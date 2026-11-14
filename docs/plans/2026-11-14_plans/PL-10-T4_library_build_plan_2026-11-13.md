# PL-10 T4: library build plan (2026-11-13)

Thread T4 of PL-10 (knowledge base and connections). Worktree branch `claude/elastic-poitras-dd574e`, based on `recon-2026-09-11` at `cd777c1`. Nothing below is built yet. Status: waiting for the owner's yes.

## Owner decisions already given (2026-11-13)

- Gemini embeddings: yes, folder by folder, each folder with its own consent record the owner can revoke.
- Folders: every folder in `~/Documents`.
- Gmail: later, after folders work.
- Evaluation: Claude drafts 50 reworded questions tied to the exact answering passage; the owner spot-checks.

## Facts checked today

- Gemini key works for embeddings. Models offered: `gemini-embedding-001` (2,048 input tokens) and `gemini-embedding-2` (8,192 input tokens, 3,072 dimensions by default, 768 on request). One probe call each, test string only, no owner text.
- Google's API terms (effective 2026-03-23): on the free tier Google may use sent text to improve its products and human reviewers may read it; with billing on it does not. The key looks free tier.
- `privacy.json` today: `consent.files = deny`, `consent.memory = allow`, no provider ticked as checked, Private mode off. `set_consent` only accepts the fixed source list, so per-folder consent needs a new record.
- `~/Documents`, skipping `.git`, `node_modules` and virtual environments: 6,506 files. Indexable now: 745 PDF (4.7 GB), 150 text, 62 Markdown, 49 DOCX, 83 PPTX, 30 RTF, about 600 JPG and PNG images (OCR). Also 2,798 Python files, mostly the `DOURMOUSE-CODE` backup and the Kiva projects.
- Old stores on disk: `workspace/memory/atlas_memory.db`, 592 facts from 88 sources, 0 embeddings. No `global_memory.sqlite3` found. The desktop vault is gone with the desktop.
- Callers: `memory_store` about 40 modules and tests; `desktop_rag` used by one tool in `general_roster.py` (around line 7078) and `model_context.py`; `shared_rag` by one tool (around line 1842) and `code_backends.py`; `memory_embed` (local Ollama, against the cloud-only policy) by `semantic_graph.py`, `general_roster.py` and `webui.py`.
- Available without new dependencies: SQLite 3.53 with FTS5, numpy 2.5, pypdf, pyobjc. Apple Vision text recognition loads through `objc.loadBundle` (checked: `VNRecognizeTextRequest` and `VNImageRequestHandler` load). No sqlite-vec and no python-docx are needed.

## What changes, in order, with a merge point after each stage

### Stage 1: library API and store (merge 1, unblocks T1, T3, T5)
- New package `dourmouse/library/`:
  - `add(source, text, meta)` returns the document id and passage ids. `meta` carries title, path or URL, location, date, privacy tier, folder, namespace and an external id for updates.
  - `search(query, filters)` with filters for source, namespace, folder, date range and highest tier.
  - `cite(passage_id)` returns the verbatim passage with title, path, page or section, date and source.
  - `verify_quote(passage_id, quote)` reuses the verbatim check in `research_pipeline/answer_critic.py` (its normaliser becomes a shared public function instead of a copy).
- One SQLite file, `workspace/library/library.db`: documents, passages, an FTS5 index, a vectors table (768 float32 values per passage), an embedding queue and per-file scan state. WAL and busy timeout as in `memory_store.py`.
- Chunking: about 400 words per passage with a small overlap, keeping page numbers and headings.
- `privacy.py`: per-folder consent (`folder_consent`, `set_folder_consent`), stored in `privacy.json` beside the existing consent, plus routes under `/api/os/settings/privacy/folders`. The existing `files` consent is left as it is.
- Tests for each function.

### Stage 2: embeddings, retrieval, connectors, connections (merge 2)
- Embedder: `gemini-embedding-2` at 768 dimensions, vectors normalised, document and query task types, built on `gemini_backend`'s key and transport. Throttled, backs off on 429, resumable. Keyword search works before any vector exists.
- Privacy gate before every Gemini call: the folder has an allow record; Private mode is off; passages that hold a secret (`t3_reason`, `.env`, keys, tokens) stay keyword-only; one `record_egress` row per request with size and hash, never the text. In Private mode search runs keyword-only and says so.
- Hybrid retrieval: FTS5 BM25 and numpy cosine, each top 50, fused by reciprocal rank, a small recency boost, filters applied to both.
- Folder connector: incremental scan of consented folders (size and modification time, the librarian's pattern). PDF through the existing `extract.extract_pdf_text`; DOCX and PPTX through the standard zip and XML libraries; RTF through macOS `textutil`; text and Markdown directly. Skips code, archives, binaries, logs and hidden or dependency folders.
- On-device OCR: Apple Vision for images and for PDF pages without a text layer (pages rendered by Quartz). No text leaves the Mac for OCR.
- Paper cards (T1) and saved pages (T5) arrive through `add()` with sources `paper` and `page`; no separate connector needed.
- Chat history and the notes vault: indexed keyword-only until the owner says yes to sending them to Gemini.
- Agent tools: `library_search`, `library_cite`, `library_add`.
- Connections API: `connections.py` grows into one list (Google, Chrome, Spotify, brokers, keys, library folders), each with what it can read, what it can do, when it was last used (from the egress ledger and usage records) and how to disconnect. Routes `GET /api/os/connections` and `POST /api/os/connections/<id>/disconnect`. No screen is built here; the UI thread sets the look and the owner sees the API output first.
- Then the real index of `~/Documents` runs in the background on an isolated test instance's workspace first, then on the owner's workspace.

### Stage 3: evaluation (merge 3)
- `scripts/library_eval.py`: recall at 5 and mean reciprocal rank, results written to `EVIDENCE/` with a history file so every change is compared. A unit test runs it on a small fixture corpus.
- 50 reworded questions drafted from the indexed documents, each with its answering passage, saved locally for the owner's spot-check. Target recall at 5 of 0.85.

### Stage 4: migrate and retire (merge 4)
- Copy the 592 facts of `atlas_memory.db` into the library (source `memory`), then point `recall` and `memory_search_semantic` at the library under the same tool names.
- Delete `desktop_rag.py` and its tool and tests; remove the desktop branch of `shared_rag.py` (its tool searches the library); delete `memory_embed.py` and move `semantic_graph.py` to library vectors.
- `memory_store.py` becomes a thin front over the library only after its callers' tests pass against it; until then it stays.

## At every merge point

Rebase on the latest `recon-2026-09-11`, check no other thread is running the suite, run the full suite and `scripts/lint_ratchet.py`, commit with the counts, add the numbered finding to `docs/ENGINEERING_AUDIT.md` and `docs/GODSPEED_ROADMAP.md`, a ledger row in `FIX_LEDGER_2026-10-06.md`, evidence in `EVIDENCE/`, then merge. No push without the owner's yes.

## Limits to say plainly

- Free-tier Gemini rate limits for `gemini-embedding-2` are not published per key; the first real run measures them. Embedding all of `~/Documents` may take many hours in the background.
- OCR quality on handwriting and photos is unmeasured until the evaluation runs.
- Testing through the real UI uses the chat box ("search my library for ...") on an isolated instance, never the owner's running app.

## Open questions for the owner

1. Approve this plan, including the deletions in stage 4 and four full-suite runs (about 24 minutes each, one per merge).
2. Chat history and memory facts: keyword-only, or also sent to Gemini?
3. Python code files (2,798, mostly code backups): skip, as planned, or index?
