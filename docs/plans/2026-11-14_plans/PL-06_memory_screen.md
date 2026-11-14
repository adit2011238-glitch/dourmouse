# PL-06: Memory you can see, correct and trust

Status: planned. Sources: Claude Cowork (memory can be viewed, edited and deleted), ChatGPT Atlas ("browser memories" under user control), memory-layer comparison (Mem0: extract, dedupe, retrieve facts; Zep: a temporal graph that knows when a fact stopped being true; Letta: the agent edits its own core memory; one source's argument that vector-only memory lacks audit, rollback and scoped writes). [comparison](https://www.developersdigest.tech/blog/best-ai-agent-memory-providers-2026)

## Goal
One screen answers "what does it know about me, where did it learn that, and can I fix it". Every reply that used a memory can show which one, and a wrong or outdated fact is a two-click fix.

## Today (verified)
- `dourmouse/memory_store.py`: a SQLite store with a `facts` table (`source`, `title`, `body`, `created_at`, `updated_at`, a unique pair, a content-hash dedup column), a full-text index and a `fact_embeddings` table for semantic search. Routes `/api/memory`, `/api/memory/search`, `/api/memory/remember`.
- Auto-learning from completed turns (`learn`), chat auto-save to RAG, "remember" tool. Memory is injected into prompts as REMEMBERED CONTEXT.
- **No screen shows any of it** (USE1: the memory routes have no caller). No delete or edit route in the OS shell; no provenance shown to the owner; one finding (SEC3-6) says global memory stores unmasked answers, and one (SEC2-10) says recalled memory is injected as a system-role message without a data fence.

## Design
**Facts get five new properties** (additive columns, migrated with the existing `table_info` pattern):
- `kind`: `preference | fact | person | project | instruction | event` (guessed at write time; editable).
- `origin`: `you_said | learned_from_chat | from_file | from_mail | from_research` plus the `source` already stored and a link (session id, file path hash, research record).
- `valid_from` and `valid_to` (the Zep idea, kept simple): a fact that is replaced is **closed, not deleted** (`valid_to` set, `superseded_by` pointing at the new one), so "I moved to Dubai" retires "I live in London" and the history is visible.
- `pinned` (always injected) and `hidden` (never injected, kept for the record).
- `uses`: a counter and last-used time, updated when a fact is actually put in a prompt.

**The MEMORY screen:** search box; filter chips by kind, origin, pinned, expiring, "used in the last 7 days"; a list showing text, origin, age, uses; detail drawer with the full text, history of changes, and the replies that used it (from the ledger); actions: EDIT, PIN, HIDE, FORGET (asks first, then hard-deletes the row, its index entries and its embedding), MERGE duplicates (the app proposes pairs by the existing content-hash and embedding closeness), ADD a fact by hand, EXPORT all (JSON and a readable markdown file), and a "forget everything from this source or this date range" action.
**"Why did you say that"** on any assistant message: a drawer listing the memories injected into that turn. Needs the turn to record the fact ids it used (new field on the ledger record, ids only).
**Privacy:** the screen shows which memories are eligible to go to a cloud model (tier and consent), because memory leaves the Mac inside prompts; a memory marked sensitive is masked by the gate exactly like the live message.

## Work breakdown
1. Store migration and API (`memory_store.py`, new `os_api/memory.py`): list with filters and paging, get with history and uses, update, close-and-supersede, pin, hide, forget, merge, export, forget-by-source. Atomic writes, a `.corrupt` aside copy through `state_file` on a bad open.
2. Retrieval changes: `recall` skips hidden and closed facts, always includes pinned (capped), records the ids used on the turn.
3. Injection fix: recalled memory goes in a fenced user-role block labelled data, not a system message (closes SEC2-10); recalled text passes the redactor (closes SEC3-6).
4. Chat record: store fact ids per turn (ledger record field), expose them to the UI.
5. OS shell: new screen `memory` (registry entry as a deliberate addition like COMMAND_CENTER), "why" drawer in `kit/thread-view.js`.
6. Kind and origin guessing at write time: deterministic rules first (a "remember" call is `you_said`; a research claim is `from_research`), no extra model call.

## Tests
Migration on an old file keeps every row; supersede closes rather than deletes; hidden and closed facts never appear in recall; pinned are capped; forget removes row, FTS entry and embedding (checked by query); export round-trips; "why" returns the exact ids injected; recalled text is fenced and redacted; a hostile memory ("ignore previous instructions") stays inert inside the fence; node tests for filters; screen contract test.

## Security and privacy
Editing and forgetting are owner-only routes and appear in the action ledger (names and hash only). The export is written only through a native save dialog. No memory text in notifications or logs.

## Risks
Over-eager auto-learning fills the list with noise: show the count of un-reviewed learned facts and add a "review new" queue; consider a weekly digest ("12 new things I learned: keep or forget"). Embedding cost on edit (re-embed lazily).

## Effort and dependencies
About 3 sessions. Pairs with PL-01 (fence, redaction) and PL-14 (data export). No hard dependency.

## Done when
Every learned fact is visible with origin; editing, superseding and forgetting work and are verified by retrieval tests; "why did you say that" lists the real memories; the SEC2-10 and SEC3-6 findings are closed with tests.
