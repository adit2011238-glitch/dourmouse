# Dourmouse — Supabase backend

Project ref `hpmruavpiloegvhdvcib`, region `ap-northeast-2`, Postgres 17.6.

This exists to sync the things that genuinely need to be the same on more
than one machine — the memory store and the usage ledger — between the Mac
and the Windows desktop. Everything else in Dourmouse stays local SQLite.

Built to the project's Tier 1/2/3 architecture rules. Where the
implementation departs from those rules it says so, here and in the
migration comment, with the reason.

---

## Schema

| Table | Purpose | Notes |
|---|---|---|
| `public.profiles` | Public mirror of `auth.users` | Every FK points here, never at `auth.users` |
| `public.devices` | One row per syncing machine | `unique(user_id, name)` |
| `public.facts` | Cloud mirror of the local memory store | `unique(user_id, source, title)` |
| `public.usage_events` | Per-turn Claude/Ollama accounting | Append-only from a client |
| `public.documents` | Chunked RAG corpus, pgvector | `embedding vector(768)`, one space only — see below |

`public.facts` mirrors the live local schema in `dourmouse/memory_store.py`
(`facts(id, source, title, body, created_at, updated_at)` with
`UNIQUE(source, title)`). The cloud form scopes that uniqueness per user, so
an upsert arriving from either machine converges on one row instead of
duplicating.

### `public.documents` — the embedding-dimension decision

Dourmouse embeds locally with Ollama's `nomic-embed-text` at **768** dims
(`dourmouse/global_memory.py`). The user's Windows desktop separately hosts a
~1M-row vault embedded with `sentence-transformers/all-MiniLM-L6-v2` at
**384** dims (`dourmouse/desktop_rag.py`). These are genuinely incompatible
vector spaces — cosine similarity across them is meaningless, not merely
lower-quality, which is exactly the reasoning `shared_rag.py`'s own
`EMBEDDING_MISMATCH` guard already documents for the local case.

Rather than a nullable dual-width column or a second table, `embedding` is
declared `vector(768)` — ONE space, chosen because it is the one Dourmouse's
own default embedder produces, so writing to this table needs no extra step
for the common case. Postgres' `vector` typmod rejects a wrong length
outright at insert time, so a 384-dim vector cannot silently land here. An
`embedding_model` column (default `'nomic-embed-text'`) is kept anyway, not
because a second width is welcome, but so a future second space can be added
as a real migration with its own column, without this one lying about what
it holds in the meantime. The desktop vault's 384-dim content stays where it
already works — queried live over SSH by `desktop_rag.py` — rather than being
force-fit into this table.

Indexed with an **HNSW** index (`vector_cosine_ops`) rather than IVFFlat:
HNSW needs no training step and no `lists` count tuned against a row count
that does not exist yet on a freshly created table, and its recall/latency
tradeoff is the better default for a corpus that will grow incrementally
rather than being bulk-loaded once.

`match_documents(query_embedding, match_count, filter, match_model)` is
**SECURITY INVOKER**, matching `sync_facts`'s own precedent: a SECURITY
DEFINER version would run as the function owner and bypass RLS entirely,
handing any authenticated caller every user's private corpus. There is
deliberately no `user_id` argument — RLS is what scopes the result, the same
way it scopes a plain `SELECT`.

Verified live in a rolled-back transaction: two users each with one document
sharing the *same* embedding vector (the adversarial case — nothing about
the vector itself distinguishes them); `match_documents` called as user one
returned exactly `mine`, never `theirs`.

### Applied migrations

```
20260903193941  tier1_profiles_auth_isolation_and_verified_email_helper
20260903194030  tier1_2_dourmouse_sync_tables_devices_facts_usage
20260903194048  tier1_3_harden_definer_grants_and_isolate_realtime
20260903194129  move_email_verified_helper_out_of_rest_exposed_schema
20260903194213  tier3_atomic_fact_sync_rpc
20260903194310  grant_dml_to_authenticated_and_service_role
20260904142641  tier2_pgvector_documents_rag
```

Each migration carries its full rationale as SQL comments. To pull the exact
DDL into `supabase/migrations/` locally:

```bash
supabase link --project-ref hpmruavpiloegvhdvcib && supabase db pull
```

That needs a `SUPABASE_ACCESS_TOKEN`, which is why the files are not already
checked in — the token was not available in the environment that built this.

---

## Two things worth knowing before you edit any policy

### 1. The spec's verified-email check does not work, and fails silently

Tier 1 rule 3 proposes this predicate:

```sql
AND auth.jwt()->>'email_confirmed_at' IS NOT NULL
```

There is no `email_confirmed_at` claim in a Supabase JWT. Per Supabase's own
JWT Claims Reference the complete set is
`iss/aud/exp/iat/sub/role/aal/session_id/email/phone/is_anonymous`, plus
optional `jti/nbf/app_metadata/user_metadata/amr`. So that expression is
`NULL` for every real user, `NULL IS NOT NULL` is false, and the policy
denies **everyone, permanently** — a silent total lockout rather than a
visible error.

The obvious substitute, `user_metadata->>'email_verified'`, is worse: a user
can set their own `user_metadata` via `auth.updateUser({ data: ... })`, so
any unverified account could assert its own verification and walk through.

What is actually used instead: `private.is_email_verified()`, a `STABLE`
`SECURITY DEFINER` function reading the authoritative, non-user-writable
`auth.users.email_confirmed_at`. It lives in the `private` schema
specifically so PostgREST does not expose it at `/rest/v1/rpc/`.

**Bootstrap consequence:** an account that has not confirmed its email cannot
register a device or sync anything. That is the intended reading of rule 3 —
verify first, then sync — and it fails visibly at the first sync attempt.

### 2. RLS is only half of access control

Found by a real negative test rather than by reading the schema: after the
tables were created, **no role held `SELECT`/`INSERT`/`UPDATE`/`DELETE` on any
of them** — only `REFERENCES`/`TRIGGER`/`TRUNCATE`. The policies were correct
but unreachable; `authenticated` would have been refused at the grant layer
with a `42501` before a policy ever ran, and the app would have shipped
completely non-functional.

The Supabase model needs both halves: a broad table grant to the role, and
RLS doing the per-row filtering. If you add a table, you must add its grants
too. The `rls_auto_enable` event trigger on this project turns RLS **on** for
new tables automatically; it does not grant anything.

`anon` holds no privilege on any of these four tables, deliberately. Every
row is one specific user's private data; there is no anonymous read path.

---

## Verified behaviour

Run as real transactions against the live database and rolled back:

| Scenario | Result |
|---|---|
| Insert into `auth.users` | Profile row auto-created by `on_auth_user_created` |
| Verified user, 2 users' facts present | Sees exactly their own 1 row |
| **Unverified user who owns a fact** | **Sees 0 rows** |
| `anon` reading `public.facts` | Hard-denied at the grant layer (`42501`) |
| `sync_facts`, first call | `synced=2 skipped=0` |
| `sync_facts`, stale replay (older `updated_at`) | `synced=0 skipped=1`, stored body unchanged |
| `sync_facts`, newer write | `synced=1`, body replaced |
| `sync_facts` with another user's `device_id` | Rejected — device not visible under RLS |
| Two users, identical embedding vector | `match_documents` as user one returns only `mine` |
| `anon` on `public.documents` | No grant at all — same as every other table here |

Security advisors: **0 findings**.

Performance advisors report `unused_index` INFO lints, one per index on
`documents` included. Those are expected on a database that has never served
a query — the indexes exist for the FK, RLS-predicate and ANN-search columns
rule 8 requires. Re-check once there is real traffic before removing any of
them.

---

## Local session persistence (Electron)

`supabase/secure-storage.ts` + `supabase/secure-storage.main.ts` implement
the storage-adapter pattern so a signed-in session survives an app restart
without landing in `window.localStorage`'s plaintext-on-disk default —
`electron-claude-bridge/supabase/client.ts` wires it into `auth.storage`.

The short version: `safeStorage` is main-process-only, so a renderer with
`contextIsolation` reaches it over IPC (`registerSecureStorageIpc()` in
`.main.ts`, exposed by `exposeSecureStorageBridge()` in the preload). When
`safeStorage.isEncryptionAvailable()` is true the value is OS-encrypted
(Keychain / DPAPI / libsecret); when it is not, `secure-storage.main.ts`
falls back to `electron-store`, unencrypted, and flags the record (`enc:
false`) rather than silently claiming a protection it does not have.

Read the file's own header before trusting this further — it states plainly
what OS encryption does and does not protect against: it stops a different
OS user or a raw-disk/backup read from recovering the token; it does **not**
stop code already running as the same OS user, and on Linux with no keyring
present, Chromium's own fallback is obfuscation, not encryption.

## Local-first cache and sync

`dourmouse/supabase_sync.py` pushes/pulls between the local SQLite memory
store and `public.facts` via `sync_facts`. Offline is treated as a normal
outcome, not an error: writes queue in a local outbox
(`SyncOutbox`, its own SQLite file, deliberately separate from
`memory_store.py`'s) and drain on the next successful sync. A 5xx or a
transport-level failure (DNS, TLS, socket) is treated the same as being
offline — the work stays queued and is retried, rather than being discarded
over a transient outage; a genuine 4xx is reported as a real failure.

The module never raises into a chat turn (42 tests cover this explicitly,
including a store that raises on every call and a transport that raises an
arbitrary exception) — this is the specific bug the codebase has already hit
twice: `RemoteMemoryStore`, despite calling itself "a drop-in" in its own
docstring, genuinely raises where local `MemoryStore` never does, and one
such exception already escaped into a live request and dropped a connection
outright. A cloud-sync module is strictly more exposed than that, so the
exception boundary is drawn once, here, around every `self._store` call.

A real, previously-live timezone bug is fixed here too:
`MemoryStore.remember()` stamps rows with a naive local timestamp (no UTC
offset), and Postgres casts a naive string to `timestamptz` under the
session's UTC `TimeZone` — so a fact written at UTC+4 arrived stamped four
hours into the future. `to_utc_iso()` normalises every timestamp this module
touches before it is compared or sent.

---

## Keys

`supabase/client.ts` reads `NEXT_PUBLIC_SUPABASE_URL` and
`NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` and **throws at construction** if
handed a `sb_secret_...` key or a JWT whose payload says
`"role":"service_role"`. The service_role key bypasses every policy above, so
one leak into a client bundle hands full read/write on every row to anyone
who unzips it. It is server-side only, and it is not in this repository.

The publishable key is safe to ship in a client and is the one this app uses.

---

## Realtime

Only `public.facts` is in the `supabase_realtime` publication, and
`subscribeToOwnFacts()` filters it further to `user_id=eq.<uid>` before the
socket. `usage_events` is deliberately **not** published: it is append-only
and high-frequency, and the usage bar polls a rolled-up total instead, so
row-level push would burn bandwidth for no feature.
