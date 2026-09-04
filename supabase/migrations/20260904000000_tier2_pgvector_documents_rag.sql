-- ===========================================================================
-- pgvector RAG storage for Dourmouse.
--
-- Adds public.documents (chunked text + embedding), an ANN index, and a
-- match_documents() similarity RPC. Written to the same rules the six
-- existing migrations follow; where it departs from them it says why.
--
-- ---------------------------------------------------------------------------
-- WHY THE EXTENSION LIVES IN `extensions`, NOT `public`
-- ---------------------------------------------------------------------------
-- Supabase's security advisor raises `extension_in_public` for anything
-- installed into `public`, and pgcrypto/uuid-ossp on this project are already
-- in `extensions`. Consistency plus a clean advisor run, so: `with schema
-- extensions`. The consequence is that `vector`, the `<=>` operator and the
-- `vector_cosine_ops` opclass are NOT on the search_path of a function that
-- sets `search_path = ''` (which every function here does, to satisfy
-- `function_search_path_mutable`). They are therefore schema-qualified below,
-- including the operator, via the explicit `OPERATOR(extensions.<=>)` form.
--
-- ---------------------------------------------------------------------------
-- THE DIMENSION DECISION: vector(768), plus a model discriminator. Both.
-- ---------------------------------------------------------------------------
-- Two embedding spaces exist in this project and they are not compatible:
--
--   * Dourmouse itself embeds with `nomic-embed-text` at 768 dims
--     (dourmouse/global_memory.py: EMBED_MODEL / EMBED_DIM = 768).
--   * The user's desktop vault uses all-MiniLM-L6-v2 at 384 dims
--     (bulletproof_vault.py, a separate system with its own store).
--
-- dourmouse/shared_rag.py already refuses to mix them (`EMBEDDING_MISMATCH`),
-- and global_memory.validate_corpus_entry() states the reason exactly:
-- cosine similarity across two different spaces is *meaningless, not merely
-- lower quality*. This table takes the same position rather than softening it.
--
-- Chosen: ONE table, ONE fixed-width column `extensions.vector(768)`, keyed to
-- Dourmouse's own model, plus a NOT NULL `embedding_model` discriminator.
--
-- Rejected alternatives, and why:
--
--   * A dimensionless `vector` column holding both spaces. pgvector cannot
--     build an HNSW or IVFFlat index on a column with no typmod ("column does
--     not have dimensions"), so this buys mixing at the cost of the ANN index
--     -- i.e. it trades away the entire point of the table.
--   * Two tables, documents_768 / documents_384. Real cost (every query, RLS
--     policy, grant and RPC duplicated) for a second space that has no
--     ingestion path here today. Revisit only if the vault genuinely starts
--     pushing rows; until then it is speculative duplication.
--
-- WHY THE DISCRIMINATOR IS STILL NEEDED WHEN THE COLUMN IS ALREADY FIXED at
-- 768 -- this is the part that is easy to get wrong. The typmod stops a
-- 384-dim MiniLM vector at the door (Postgres rejects the wrong length
-- outright, which is what makes 384 a hard error here rather than a silent
-- corruption). But *equal dimensionality is not the same as an equal space*:
-- all-mpnet-base-v2, e5-base and nomic-embed-text are all 768-dim and all
-- mutually meaningless under cosine. Dimension alone therefore cannot be the
-- guard. `embedding_model` is what actually pins the space, match_documents()
-- filters on it, and the CHECK below keeps the column from drifting into a
-- free-text field nobody validates.
--
-- Consequence, stated plainly: the 384-dim desktop vault CANNOT be uploaded
-- here as-is. It must be re-embedded with nomic-embed-text first. That is
-- deliberate -- the same refusal shared_rag.py already makes -- not an
-- oversight.
--
-- ---------------------------------------------------------------------------
-- THE INDEX: HNSW, not IVFFlat
-- ---------------------------------------------------------------------------
-- IVFFlat is a trained index: it clusters existing rows into lists, so it
-- must be built AFTER representative data is present, and an IVFFlat built on
-- an empty table has near-zero recall until it is REINDEXed. This table
-- starts empty and fills incrementally from a desktop client, so an IVFFlat
-- here would ship broken and stay broken until someone remembered to rebuild
-- it. HNSW is a graph index built incrementally, needs no training set, is
-- correct from the first row, and has strictly better recall-vs-latency at
-- this scale. Its costs are real but acceptable: slower inserts and a larger
-- memory footprint. At personal-RAG scale (thousands to low millions of
-- chunks) that is the right trade.
--
-- `vector_cosine_ops`, because every similarity path in this codebase is
-- cosine (global_memory.cosine_similarity, memory_embed.cosine_similarity).
-- 768 is far inside pgvector's 2000-dim indexing ceiling.
-- ===========================================================================

create extension if not exists vector with schema extensions;

create table if not exists public.documents (
  id          uuid primary key default gen_random_uuid(),

  -- Rule: FKs point at public.profiles, never at auth.users. Deleting the
  -- user takes their documents with them.
  user_id     uuid not null references public.profiles (id) on delete cascade,

  -- The originating machine is provenance, not ownership. Retiring a device
  -- must not destroy the chunks it happened to upload, so SET NULL.
  device_id   uuid references public.devices (id) on delete set null,

  source      text not null,
  title       text not null,
  chunk_text  text not null,

  embedding   extensions.vector(768) not null,

  -- Which space `embedding` lives in. See the long note above: the 768 typmod
  -- rejects a wrong-length vector, but only this column distinguishes two
  -- *different* 768-dim models, which are just as incomparable.
  embedding_model text not null default 'nomic-embed-text',

  metadata    jsonb not null default '{}'::jsonb,
  created_at  timestamptz not null default now(),

  constraint documents_source_not_blank     check (length(btrim(source)) > 0),
  constraint documents_chunk_text_not_blank check (length(btrim(chunk_text)) > 0),

  -- An allowlist, not free text. A typo'd or unknown model name would put
  -- rows into the table that no query could ever correctly compare against;
  -- better to reject the insert. Extend this list deliberately, and only
  -- with another genuinely 768-dim model.
  constraint documents_embedding_model_known
    check (embedding_model in ('nomic-embed-text')),

  -- metadata is filtered with jsonb containment (@>) by match_documents;
  -- a scalar or array there would make that predicate meaningless.
  constraint documents_metadata_is_object
    check (jsonb_typeof(metadata) = 'object')
);

comment on table public.documents is
  'Chunked RAG corpus with 768-dim nomic-embed-text embeddings. One vector '
  'space only -- see the migration comment: 384-dim vault content must be '
  're-embedded before it can be stored here.';

comment on column public.documents.embedding_model is
  'Pins the vector SPACE, not just the width. Two different 768-dim models '
  'are mutually meaningless under cosine, so the typmod alone is not a '
  'sufficient guard.';

-- --- indexes ---------------------------------------------------------------

-- The ANN index. See the HNSW-vs-IVFFlat reasoning above.
create index if not exists documents_embedding_hnsw_idx
  on public.documents
  using hnsw (embedding extensions.vector_cosine_ops);

-- FK / RLS-predicate columns. `user_id` is in the USING clause of every
-- policy on this table, so it is read on every single row access.
create index if not exists documents_user_id_idx  on public.documents (user_id);
create index if not exists documents_device_id_idx on public.documents (device_id);

-- Supports the containment filter in match_documents().
create index if not exists documents_metadata_gin_idx
  on public.documents using gin (metadata jsonb_path_ops);

-- --- RLS -------------------------------------------------------------------
-- The project's `rls_auto_enable` event trigger already turns this on for new
-- public tables; stated explicitly anyway so the file is self-contained and
-- does not depend on a trigger firing.

alter table public.documents enable row level security;

-- Same shape as the existing "own facts" / "own devices" policies, including
-- the same two deliberate details:
--   * private.is_email_verified() rather than auth.jwt()->>'email_confirmed_at'
--     -- there is no such JWT claim, so that predicate denies everyone
--     silently. See supabase/README.md.
--   * both auth.uid() and the helper wrapped in (select ...), so Postgres
--     evaluates each once as an InitPlan instead of once per row. This is
--     what keeps the `auth_rls_initplan` performance lint clean.
create policy "own documents"
  on public.documents
  for all
  using (
    user_id = (select auth.uid())
    and (select private.is_email_verified())
  )
  with check (
    user_id = (select auth.uid())
    and (select private.is_email_verified())
  );

-- --- grants ----------------------------------------------------------------
-- The lesson from migration 20260903194310, which this table would otherwise
-- repeat: CREATE TABLE grants no DML to anybody. RLS without a grant is a
-- 42501 at the privilege layer before a policy is ever consulted -- correct
-- policies, completely non-functional app. Both halves are required.

grant select, insert, update, delete on public.documents to authenticated;
grant select, insert, update, delete on public.documents to service_role;
-- `anon` deliberately gets nothing. Every row is one user's private corpus;
-- there is no anonymous read path.

-- --- the similarity RPC ----------------------------------------------------

create or replace function public.match_documents(
  query_embedding extensions.vector(768),
  match_count     integer default 10,
  filter          jsonb   default '{}'::jsonb,
  -- 4th parameter with a default, so the three-argument call in the spec and
  -- from PostgREST still resolves. Exists so a future second 768-dim model
  -- can be queried without a schema change -- and so that the space being
  -- searched is always explicit at the call site.
  match_model     text    default 'nomic-embed-text'
)
returns table (
  id         uuid,
  source     text,
  title      text,
  chunk_text text,
  metadata   jsonb,
  similarity double precision
)
language sql
stable
security invoker
set search_path = ''
as $function$
  select
    d.id,
    d.source,
    d.title,
    d.chunk_text,
    d.metadata,
    -- pgvector's <=> is cosine DISTANCE in [0,2]; callers want similarity in
    -- [-1,1] where higher is better, matching global_memory.cosine_similarity.
    1 - (d.embedding operator(extensions.<=>) query_embedding) as similarity
  from public.documents d
  where d.embedding_model = match_model
    and d.metadata @> coalesce(filter, '{}'::jsonb)
  order by d.embedding operator(extensions.<=>) query_embedding
  limit greatest(1, least(coalesce(match_count, 10), 200));
$function$;

comment on function public.match_documents(extensions.vector, integer, jsonb, text) is
  'Cosine ANN search over the caller''s own documents. SECURITY INVOKER by '
  'design: a SECURITY DEFINER function here would run as the owner and '
  'bypass RLS entirely, letting any authenticated caller read every user''s '
  'private corpus. public.sync_facts is invoker for the same reason.';

-- EXECUTE on a new function is granted to PUBLIC by default, which would
-- expose it at /rest/v1/rpc/ to `anon`. It would still return zero rows --
-- RLS holds, and anon has no grant on the table -- but an anonymous caller
-- should not be able to probe the endpoint at all.
revoke all on function public.match_documents(extensions.vector, integer, jsonb, text) from public;
revoke all on function public.match_documents(extensions.vector, integer, jsonb, text) from anon;
grant execute on function public.match_documents(extensions.vector, integer, jsonb, text) to authenticated;
grant execute on function public.match_documents(extensions.vector, integer, jsonb, text) to service_role;
