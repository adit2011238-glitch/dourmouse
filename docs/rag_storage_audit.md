# RAG storage-layout audit (Cycle 1)

Scope: the six modules that make up Dourmouse's RAG/retrieval stack. Read
against the actual code, not design intent. `memory_store.py` and
`bulk_ingest.py` are owned by this cycle's fix (see "Fix landed" below);
`shared_rag.py`, `desktop_rag.py`, `memory_embed.py`, `semantic_graph.py`
are external/remote-owned and documented **read-only** this cycle.

## Storage layout, per module

| Module | Store | Granularity | Owns write path? |
|---|---|---|---|
| `memory_store.py` | local SQLite, `facts` table + `facts_fts` (FTS5 external-content) | one row per `remember()` call — a whole fact/note/session-turn/file, never split | yes (local) |
| `shared_rag.py` | reads `hybrid_vault.db` + `vector.index` (FAISS) directly, IF mounted locally | whatever the external vault stored a row as (schema probed at runtime, not assumed) | no — read-only merge layer over an external corpus |
| `desktop_rag.py` | SSH's a Python program onto the desktop that reads the same `hybrid_vault.db` (`hybrid_chunks` table, 1,023,765 rows) + `vector.index` (`IndexFlatL2`, 226,076 vectors, dim 384) **there**, over the network | one row = one `hybrid_chunks` row (`chunk_text` column) — chunking, if any, happened in `bulletproof_vault.py`, a process this codebase has never inspected | no — remote-owned store, this module only queries it |
| `bulk_ingest.py` | writes into `memory_store.MemoryStore` | one row per source file (local) or one row per Drive file — the **whole file's extracted text**, capped at `_MAX_TEXT_CHARS` (200,000 chars) with a `[TRUNCATED — N real chars...]` marker appended, never split into multiple rows | yes (calls `memory_store.remember()`) |
| `memory_embed.py` | reads/writes `memory_store`'s `fact_embeddings` table | one embedding vector per **fact row** (whole body, via `embed_texts()` against Ollama `nomic-embed-text`) — not per chunk, because nothing upstream produces chunks | no — layered cache on top of `memory_store` |
| `semantic_graph.py` | reads `memory_store.all_facts()`, writes into an in-process/on-disk Qdrant collection | one Qdrant point per fact (reuses `memory_embed`'s cached vector) | no — clustering layer, no independent store |

## Confirmed gap 1: no chunking anywhere in this codebase

Grepped all six modules for `chunk_size` / any chunk-length constant: zero
hits. `bulk_ingest.py` has one length constant, `_MAX_TEXT_CHARS = 200_000`
— a **truncation cap**, not a chunk size (text past it is dropped and
labeled `TRUNCATED`, never continued in a second row). `shared_rag.py` and
`desktop_rag.py` reference columns literally named `chunk_id`/`chunk_text`/
`hybrid_chunks`, but those are schema names on the **external** vault built
by `bulletproof_vault.py` — a process outside this codebase. Whatever
chunking policy produced those rows is not visible from here and is not
this codebase's to change.

Practical effect: every fact/file/note this codebase writes is indexed as
one atomic unit, however long. A very long local file (say, a 500KB log)
gets truncated at 200K chars and indexed as a single FTS5 document —
recall on content past the cap is impossible, and recall on content near
the cap competes for relevance against the whole rest of the document in
one bm25 score. This is a known, accepted limitation (Rule 2.2: the
truncation is stated in the stored body, never silent) — not something
this cycle's task list asked to fix, called out here for the record.

## Confirmed gap 2 (now fixed this cycle): zero content-dedup logic

Grepped all six modules for dedup logic before this cycle: the **only**
`hashlib` usage in the entire stack was `desktop_rag.py:781`, building a
cache **filename** for a position→id map (`dourmouse_desktop_rag_idmap_
<sha256>.i64`) — unrelated to content dedup, and in the remote-owned,
read-only module besides. No module hashed, fingerprinted, or otherwise
compared fact/chunk *content* to detect duplicates before insert.

`memory_store.py`'s only pre-existing duplicate guard was the
`UNIQUE(source, title)` constraint on `facts`, enforced via
`INSERT ... ON CONFLICT(source, title) DO UPDATE` — a literal-key upsert.
It catches "the same file re-ingested under the same path" but **not**
"the same content ingested under a different path/id/title" — exactly the
shape of the confirmed unresolved duplicate in the remote vault
(`dourmouse_universe.md`: "2000 in Afghanistan", two ids, in
`desktop_rag.py`'s `hybrid_chunks` table). That specific pair lives in the
remote, read-only vault and is out of scope to edit this cycle (see task
list); this fix closes the same class of gap at the two entry points this
codebase does own.

### Fix landed this cycle (`memory_store.py`, `bulk_ingest.py`)

- `memory_store.py`: `facts` gained a `content_hash` column (migration-safe
  `ALTER TABLE` + `PRAGMA table_info` guard so an on-disk DB from before
  this fix upgrades cleanly) plus an `idx_facts_source_hash` index, and a
  one-time backfill of the hash for any pre-existing row on schema init.
  `remember()` now hashes the (whitespace-normalized) body and, before
  inserting, checks for an existing row with the **same source** and
  **same content_hash** under a **different title**. If found, the insert
  is skipped and `remember()` returns a `"MEMORY DUPLICATE: ..."` result
  naming the original (source, title) instead of creating a second row.
  Dedup is scoped **per source** deliberately — the same paragraph
  legitimately appearing in two different sources (e.g. a vault note
  quoted inside a session ledger) is not a duplicate of itself.
- `bulk_ingest.py`: `ingest_local_tree()` and `ingest_drive()` now check
  each `remember()` result via the new `memory_store.is_duplicate_result()`
  helper and count it under a new `skipped_duplicate` stat instead of
  `indexed`, so a resumed/checkpointed run (or a moved/renamed file, or a
  Drive "Make a copy") never silently inflates the index with a second
  copy of content already stored, and the skip is visible in the run's
  stats/status JSON rather than hidden.
- Real dedup is exact-content, not fuzzy: normalization is limited to
  stripping surrounding whitespace and trailing per-line whitespace, so
  a genuinely different document is never wrongly collapsed into an
  existing one.

Not touched (per task scope — external/remote-owned, read-only):
`shared_rag.py`, `desktop_rag.py`, `memory_embed.py`, `semantic_graph.py`.
The known remote-vault duplicate pair therefore still exists in that data
and is not resolved by this fix; resolving it would require either a
write path into `bulletproof_vault.py`'s own store (not this codebase) or
a one-off dedup pass explicitly scoped and approved as its own task.

## Test coverage added

`dourmouse/tests/test_memory_store.py::TestContentHashDedup` (7 cases) and
`dourmouse/tests/test_bulk_ingest.py::TestIngestLocalTreeDedup` (3 cases) +
one Drive case in `TestIngestDrive` — real dedup-on-ingest, not mocked at
the store level: same content under a new title/path/id is skipped and
counted; a real content difference or a different source is not
collapsed; a pre-existing on-disk DB without `content_hash` backfills and
dedups correctly on reopen. Full pass: 47/47 (`test_memory_store.py`),
26/26 (`test_bulk_ingest.py`).
