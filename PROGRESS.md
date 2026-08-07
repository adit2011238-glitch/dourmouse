# Dourmouse — Progress

## Current Phase: V4.0 — SELF-HOSTED PERSONAL AI OPERATING SYSTEM (2026-08-06)
## Last updated: 2026-08-07

### Rebrand — renamed from JARVIS to DOURMOUSE

- Package `atlas_jarvis` → `dourmouse`; env vars `JARVIS_*` → `DOURMOUSE_*`
  (legacy names still honored via the config shim in `dourmouse/config.py`,
  so an existing `.env` keeps working untouched).
- Folder `atlas-jarvis-dispatch-3.2.0` → `dourmouse-4.0.0`; desktop bundle
  `JARVIS.app` → `dourmouse.app`; runtime files `.jarvis-ui.log/.pid` →
  `.dourmouse-ui.*`; UI title now `DOURMOUSE // CENTRAL AGENT DISPATCH`.
- Release artifact: `~/Documents/dourmouse-4.0.0.zip` (source + app + docs).

### V4.0 — FREELY LOCAL: local LLM, ATLAS command-centre, automation, multi-device
User request: the master spec — a private, local-first AI operating system.
  Audited against v3.2.0: Phases 1 (core), 4 (Manus agents), 5 (memory),
  8 (motion), 12 (security) substantially existed. The defining gaps were
  Phase 2 (local LLM), Phase 3 (ATLAS integration), Phase 9 (multi-device),
  Phase 11 (automation), and Phase 13 (self-improvement).

- P0 — Fixed the pre-existing packaging test: test_desktop.py looked for
  scripts/build_app.command but the file lives at project root. Suite green
  from 607/1fail to 608 green.
- P1 — OllamaConfig + load_llm_config() resolver (DOURMOUSE_LLM_BACKEND =
  ollama|nvidia|auto; auto probes 127.0.0.1:11434 and picks the live brain).
  Threaded through dispatch.py (default client), orchestrator.py (dispatch
  runner), webui.py (_resolve_server_config -> load_llm_config, GET
  /api/backend), and code_backends.py (ollama backend). New code_ollama
  roster agent (keyless, zero API spend). UI header shows the real backend.
- P2 — atlas_ops.py: atlas_status (branch/commits/dirty), atlas_bootstrap
  (real FX bootstrap log + pair bid-day counts), atlas_deliverables. New
  'atlas' roster agent. Live-verified against the real ATLAS repo.
- P3 — report.py: build_morning_report (news/markets/tasks/ATLAS/system,
  no LLM in the path, honest failures) + DailyReporter thread wired into
  run_server. Live-verified (real news, real ATLAS telemetry, honest 429).
- P4 — DOURMOUSE_ACCESS_TOKEN bearer/cookie gate (loopback exempt), DOURMOUSE_HOST
  bind, login.html, docs/tailscale.md, responsive media queries on all three
  pages. Live-verified over HTTP: loopback 200, bad token 401.
- P5 — Premium HUD: Canvas2D particle field, radar sweep + sector labels,
  corner-bracket panels, 5-state motion machine (idle/thinking/planning/
  executing/complete) wired into the SSE handlers, real /api/backend
  indicator. New tests/test_ui_local.py pins the FULLY-LOCAL guarantee:
  zero external resources across all four pages.
- P6 — Memory & project intelligence deepening. memory_embed.py: semantic
  recall over the FTS5 store — Ollama embeddings (nomic-embed-text, gated
  DOURMOUSE_EMBED=1) with an honest FTS5 fallback chain (gate off / endpoint
  down / weak match all degrade with method reported); embeddings cached in
  fact_embeddings; memory agent gains memory_search_semantic. repo_index.py:
  atlas_repo_scan ingests a curated digest of the real ATLAS repo (README,
  CHANGELOG, docs, deliverables, python skeletons; excludes .venv/.git/data/
  binary) idempotently (META mtime+size line, upsert by source+title);
  atlas_repo_search / atlas_repo_status on the atlas agent. learn.distill_query
  promoted to public and reused by the semantic FTS5 fallback so conversational
  phrasing recalls. Live-verified: 257 repo facts in 0.3s, idempotent rescan,
  conversational scalping query recalls real modules.
- P7 — Voice (fully local, zero cloud). dourmouse/voice.py: DOURMOUSE_VOICE
  gate; faster-whisper STT (lazy, int8, model from DOURMOUSE_WHISPER_MODEL);
  TTS via piper with a zero-dependency macOS 'say' fallback; honest
  NOT CONFIGURED whenever the gate is off or an engine/model is missing.
  New routes: GET /api/voice (capability report), POST /api/speech (STT),
  GET /api/speech?text= (TTS, audio/wav). HUD gains [MIC] (MediaRecorder ->
  local STT -> chat input) and [SPK] (last reply -> local TTS -> playback)
  buttons that disable honestly when voice is off. requirements-voice.txt
  keeps the heavy wheels optional. Live-verified with both plan engines
  installed: piper TTS (voice fetched once) -> faster-whisper tiny STT
  round-trip returns real transcription ("Hello … voice is fully local");
  macOS 'say' fallback also proven (2.2s WAV). piper-tts 1.6 API pinned
  (flat download layout + wave.Wave_write synth). Reviewer-hardened: whisper
  load+transcribe serialized under a lock, 'say' TimeoutExpired -> honest
  NOT CONFIGURED, malformed/oversized Content-Length -> 400 (50 MB cap),
  empty transcription -> explicit "no speech detected", [[…]] say control
  sequences neutralized, TTS text capped at 500 chars, requirements pins
  piper-tts>=1.6. New hermetic suite: test_voice (34).

### P6 LIVE VERIFICATION + CHANGELOG SECTIONING (2026-08-07)
- Ollama started + `nomic-embed-text` pulled; semantic recall run against the
  REAL 281-fact store: "why did we change the risk parameters" -> method
  semantic, top hit (0.61) `CHANGELOG.md: v9 — Phase 5: Risk management
  layer` — while FTS5 finds nothing (changelog says "tightened daily loss
  limit", not "risk parameters").
- repo_index now splits CHANGELOG/CHANGES files into per-section facts
  (`CHANGELOG.md: <section>`) so decisions buried mid-file are first-class
  retrievable facts; prune switched to exact produced-titles (self-healing
  across title-scheme changes). Live rescan: 25 sections added, old flat
  fact pruned, idempotent thereafter (281/281 unchanged).
- Regression pinned hermetically (test_memory_embed): the REAL changelog
  risk-section text + the real query, a deterministic domain embedder
  standing in for nomic-embed-text THROUGH the real embed_texts HTTP path;
  asserts FTS5 (even distilled) finds nothing while semantic returns the
  risk section first, and weak matches still report fts5 honestly.
- Reviewer-hardened: a changelog with no '## ' sections falls back to a
  flat digest (never vanishes); the prune now distinguishes "file
  definitively gone" (drop) from "walked but transiently skipped" (keep),
  so a flaky scan can never destroy previously-good facts — pinned by
  test_changelog_without_sections_falls_back_to_flat and
  test_transient_skip_keeps_prior_facts. Live re-verified: idempotent
  rescan, semantic recall 0.4s warm, risk section still top hit (0.61).
- P8 — self_improve.py: pure digest over real bus traffic + conservative
  suggestions (silent agent = flagged silent, never invented). memory agent
  gains daily_digest tool; GET /api/selfimprove.
- P6+ — Project Memory panel. repo_index.py: scan-meta sidecar JSON next to
  the store DB (last scan root/time + stats — never a fact, so it can't
  pollute counts), repo_facts() newest-first listing, and a self-ingestion
  guard (the scan skips *.repo-meta.json so a store sitting inside a scanned
  root can never index its own sidecar). webui.py: GET /api/repo (status:
  facts/last_scan/recent; ?q= FTS5 search scoped to source='repo') and POST
  /api/repo/scan (idempotent, honest 409/500). HUD gains a PROJECT MEMORY
  panel in the right rail — fact count, last-scan line, debounced search box
  wired to atlas_repo_search, [RESCAN] button. Live: scan 257 files / 0
  added / 281 facts, status shows last scan, search honestly empty on the
  known FTS5 gap. test_repo_panel (18): meta roundtrip/corruption, sidecar
  skip, HTTP status/search/scan + idempotency, honest NOT CONFIGURED, HUD
  wiring.
- P6+ multi-project — atlas_repo_scan now takes an optional per-tool `path`
  and can index ANY repo, not just ATLAS. The source key is derived from the
  folder name (`repo:<folder-slug>`), so projects stay scoped and never mix:
  search/status accept `source` (or `path` to derive it), the scan's prune
  touches only its own source, and scan-meta sidecars are per-scope files.
  Correctness caught live: FTS5 column filters match by token, so
  `source:"repo"` also matched `repo:proj` and leaked every project into the
  default scope — scoping is now an EXACT `f.source = ?` equality on the
  joined row (memory_store.search), which also tightens all existing sources.
  Slug collisions fixed: `_` is preserved (my_proj vs my-proj stay distinct)
  because the prune deletes by exact source. Live: dourmouse indexed as
  `repo:dourmouse-4-0-0` (76 facts), scoped search finds voice.py, default
  scope stays airtight (281 ATLAS facts, zero leak). test_repo_index (+14
  multi-project tests) incl. the leak regression and underscore collision.
- Verification: full suite green (see below). New hermetic suites:
  test_local_llm (17), test_atlas_ops (15), test_report (12),
  test_multi_device (12), test_ui_local (11), test_self_improve (7),
  test_memory_embed (18), test_repo_index (27), test_voice (34),
  test_repo_panel (18). Total 783.
- JARVIS COMPLETION PASS (user: "stay as true to the original JARVIS design
  from the first three Iron Man movies; execute your plan to complete").
  (a) ROLLBACK PATH — the folder was NOT a git repo: git init + .gitignore
  (.env, .venv, workspace/, caches excluded) + baseline commit `4dbc003`.
  (b) LOCAL BRAIN PINNED — .env: DOURMOUSE_LLM_BACKEND=ollama +
  OLLAMA_MODEL=qwen3:8b; config resolves live (OllamaConfig, qwen3:8b,
  http://127.0.0.1:11434/v1, ollama_available()=True). The model zoo is
  already rich (qwen3:8b, qwen2.5-coder:14b, gemma4:12b, gpt-oss:120b,
  embeddings) — nothing to download.
  (c) JARVIS DESIGN PASS (index.html, login.html, map.html): holo ice-blue
  palette shift (--cyan #8FD0FF, --cyan-bright #eaf7ff, --bg #04070E,
  rgba bulk re-tint) across all three pages; Stark-lab blueprint-grid
  backdrop; helmet-HUD targeting frame (edge tick strips + corner
  brackets, #hudframe); full-screen BOOT SEQUENCE overlay — arc-reactor
  ring ignition SVG, D.O.U.R.M.O.U.S.E. wordmark in holo light sans,
  sequential typed boot lines, auto-fold after ~3s; the centre core SVG
  redesigned as an ARC REACTOR — six triangular palisade segments + five
  coil rings + white-hot centre hub (radar sweep + sector labels and all
  `.arc/.sweep/.sector/.hub` classes preserved for the state machine);
  login/map wordmarks restyled to the JARVIS holo treatment. All UI-test
  contracts preserved (particles, sweep, sectors, setCoreState, backendline,
  @media, zero external resources). Verified: 783 passed (50.7s), 11/11 UI
  contracts, live boot at 127.0.0.1:8765 — /api/backend shows
  OLLAMA · qwen3:8b, no console errors, computed styles confirm the holo
  palette + 6-segment reactor, headless-Chrome screenshots of boot + HUD
  captured. Two follow-up items left for a later pass (voice/semantic
  backends already work; broker + Obsidian phases remain deliberately
  gated).
- BUGFIX — "send an agent to fetch news" failed: the web UI reuses ONE
  ChatSession (and one BudgetTracker) for the whole life of the server, and
  the wall-time cap counts from TRACKER CREATION — so after 600s of server
  uptime EVERY request died instantly with "BUDGET EXHAUSTED: NNNs elapsed"
  (reproduced live: fresh directive killed at the budget gate before the
  news agent ever ran, while the feed itself was healthy). Fix: wall-time is
  per request-tree by design, so BudgetTracker.reset_wall_clock() restarts
  only the clock at the start of each ChatSession.ask() turn; calls and cost
  remain session-scoped and cumulative (their documented purpose).
  Regressions: test_reset_wall_clock_frees_long_lived_tracker,
  test_reset_wall_clock_keeps_calls_and_cost,
  test_long_lived_session_turn_not_budget_exhausted. Suite 786. Live
  re-verified: the same directive now plans -> news agent -> tool_use ->
  LIVE NEWS HEADLINES with real headlines.

---

## Current Phase: V3.2 — WATCH THE AGENTS COMMUNICATE (session 3 cont., 2026-08-02)
## Last updated: 2026-08-02 (session 3 cont.)

### V3.2 — SEE HOW THEY COMMUNICATE (live bus visualization on the neural map)
User request: "i should see how they communicate with each other." The v3.0 bus
  was real but static — unread badges and a message list. Now the communication
  is VISIBLE: the instant a message hits the bus, glowing pulses travel between
  agent nodes on the neural-link map, the grid flashes the sender + recipients,
  and a live comms ticker slides in. No new state — this surfaces traffic that
  was already flowing (the always-on live broadcasts + messenger tool plane).

- webui.py — GET /api/messages gains an optional ?since=msg-<N> (bare number
  also accepted) filter returning ONLY messages with id > N, newest first.
  Unread counts stay ABSOLUTE (computed from the whole bus, never the
  filtered window). Anchored regex ^(?:msg-)?(\d+)$ — garbage since= falls
  back to the full window (locked by a test); never errors.
- ui/map.html — graphPos map stored on renderGraph. pollBus() (was
  pollInboxes) polls /api/messages?since=<lastMsgId> every 1s, updates
  unread badges, and for EACH new message: flashes the sender + recipient
  node cards in GRID view (fanout capped at 12 so a live broadcast doesn't
  strobe the whole grid), animates a glowing SVG pulse (trail line + moving
  dot, 650ms ease) from sender to each recipient in NEURAL_LINK view
  (broadcast fanout capped at 12), and pushes a COMMS STREAM ticker row
  (slide-in, auto-fade after ~5s, max 5 rows, pointer-events none).
- ui/index.html — the AGENT COMMS panel flashes the newest arrival (.new
  highlight) on since= polling. commsInit flag: the FIRST render seeds the
  last-seen id with ZERO flashes — an already-busy bus doesn't flash
  everything on page load (reviewer-caught, fixed).
- ui/agent.html — the INBOX frame flashes when new messages arrive for THAT
  agent; inboxInit flag seeds the seen-id set on first render with no flash
  (same reviewer-caught pattern, fixed).
- Verification: 610 passed, 0 failed (up from 607). New hermetic tests in
  tests/test_message_bus.py: test_messages_endpoint_since_filter_returns_
  only_new (msg-1 -> only msg-2 returned; unread absolute for the REGISTERED
  echo_agent receiving the broadcast) and test_messages_endpoint_malformed_
  since_falls_back_to_full_window (garbage since= -> full window, anchored
  regex safe). UI wiring strings updated: map now asserts pollBus + pulseTo
  instead of the removed pollInboxes.
- Reviewer rounds (3): round 1 caught 2 test failures (unread asserted on a
  NON-registered agent name -> KeyError; map wiring test still asserted the
  old pollInboxes) + 3 flags (first-load flash-all on index/agent pages,
  uncapped broadcast flash fanout on the map, malformed-since untested) —
  all fixed. Round 2 caught a duplicated finally: block (SyntaxError —
  collection failed) + INVERTED first-load flash suppression (the init flag
  flipped inside the loop so the newest pre-existing message still flashed;
  now isNew = flag && id>last with the flag set AFTER the seed loop). Round 3
  found the fix had accidentally left the since_filter test's try: open (a
  second SyntaxError) — rewrote the region so both tests have exactly one
  try/finally; signed off. 610 green.
- Re-roll: bash scripts/build_dist.sh 3.2.0.

### V3.1 — EACH AGENT CAN USE A DIFFERENT NVIDIA MODEL
User request: "each agent can use a different nvidia model if possible."
  One LLM brain drives each run, so per-agent models are resolved
  DETERMINISTICALLY at the delegation boundaries — never a guess (Rule 2.8):
  a nested delegate_task routed AT one subagent runs on THAT agent's model,
  and a focus_agent chat route runs on the focused agent's model. Free
  sub-orchestration keeps the orchestrator's default. Every agent's model is
  visible in the UI so you always know which brain is thinking where.

- config.py — NvidiaConfig gains ``agent_models`` (dict of agent name ->
  model id) + ``model_for_agent(agent)``: DOURMOUSE_MODEL_<AGENT> env override
  (keys uppercased at load, case-insensitive lookup) else the default model
  (NVIDIA_MODEL). Pure env resolution, no LLM judgment (Rule 2.8).
- dispatch.py — run_dispatch_messages + run_dispatch gain ``model`` override
  param: an explicit model beats the config default for that run; None keeps
  the previous behavior exactly (test-model in hermetic tests, config default
  in prod).
- chat.py — ChatSession.ask(model=) forwards the override to dispatch (a
  one-turn override; the session default is unchanged for later turns).
- general_roster.py — delegate_task resolves ``nested_model =
  ctx.config.model_for_agent(target)`` when a target subagent is given and
  passes it into the nested run; free sub-orchestration (no target) keeps
  the parent's model. Deterministic (Rule 2.8).
- webui.py — _handle_chat: a focus_agent route resolves the override from
  server.config and passes it to session.ask. build_roster_payload(registry,
  config=None) + _effective_model() add each agent's ``model`` to the roster
  payload; /api/agent/<name> includes it too. NEW _resolve_server_config():
  serve_forever and desktop.launch resolve the config so server.config is
  set in the REAL app — without it the per-agent override and labels were
  inert in production (reviewer-caught CRITICAL, fixed; tests now cover it).
- ui — MODEL label on the agent window (agent.html), the map detail panel
  (map.html), and every dashboard roster card (index.html); "default" shown
  honestly when no config is attached.
- .env.example — documents the DOURMOUSE_MODEL_<AGENT> vars with the full
  roster name list.
- Verification: 607 passed, 0 failed (up from 594). New tests: test_config
  (env scan, case-insensitive lookup, unknown/empty agent falls back to
  default); test_dispatch TestModelOverride (override wins, config default
  when none, override beats config); test_self_dispatch
  test_delegate_target_uses_that_agents_model (exact 4-call model sequence:
  parent default, nested target model x2, parent default); test_webui
  (focus_agent route uses the agent's model, no-focus uses session default,
  roster payload carries per-agent model + "default" without config,
  _resolve_server_config explicit/env/none-without-key). All hermetic.
- Reviewer rounds (2): round 1 verified the design (dispatch override
  resolution, FakeClient backward compat, free-orchestration model=None
  regression, focus_agent handler path, roster payload backward compat, no
  collisions/cycles) and caught the CRITICAL serving-path gap above; round 2
  verified the _resolve_server_config fix placement, no-key safety, and
  hermeticity. All signed off; 607 green.
- Re-roll: bash scripts/build_dist.sh 3.1.0.

### V3.0 — THEY ALL COMMUNICATE WITH EACH OTHER (inter-agent message bus)
User request: "they should all communicate with each other." One LLM brain
  drives every roster agent, so real agent-to-agent communication lives on
  two honest planes: a DETERMINISTIC data plane (the always-on live agents
  broadcast their REAL poll results onto a shared bus — news -> *, zero LLM,
  Rule 2.8) and an LLM-MEDIATED tool plane (a new ``messenger`` subagent
  with send_message / read_agent_inbox so the orchestrator can explicitly
  route knowledge between agents mid-task). The bus is visible everywhere:
  a COMMS panel on the dashboard, per-agent inboxes + unread badges on the
  map and every agent window, and bus traffic mirrors into the v2.9 long-term
  store so the system LEARNS from inter-agent knowledge too.

- dourmouse/message_bus.py (NEW) — thread-safe, bounded MessageBus
  (stdlib only): monotonic ids, timestamps, 500-message ring (oldest
  evicted), 1200-char body / 200-char subject caps, broadcast (to="*"),
  per-recipient read state, on_post observers whose exceptions are
  swallowed (a broken observer never breaks dispatch — same principle as
  the event_sink), and a process singleton get_message_bus()/
  set_message_bus() so tools, the live runtime, and the web UI share ONE
  channel (tests isolate with set_message_bus(MessageBus())).
  - Per-recipient read state (reviewer-caught design flaw, fixed): a
    broadcast stays UNREAD for every agent until THAT agent reads it —
    one agent opening its inbox never clears another's badge. Internal
    read_by: set(); JSON-safe _public(viewer) copies for every external
    surface (read computed for the viewer, read_by serialized as a list)
    so no code path can ever json.dumps a raw set — post() returns the
    public copy AND hands it to observers too.
- live_runtime.py — LiveRuntime gains bus=None (default off = hermetic
  tests post nothing). _poll_once broadcasts the REAL poll text (or the
  honest "LIVE POLL FAILED: ..." error) from the owning agent, wrapped in
  try/except so a broken bus never kills the poll loop.
- general_roster.py — NEW ``messenger`` subagent (Both domain, 17th roster
  member) with:
  - send_message: deterministic validation (Rule 2.8) — from/to must be
    real roster agents (or to="*" broadcast); typo'd/spoofed names are
    REFUSED loudly, never silently routed. Internal bus only — nothing
    leaves the machine.
  - read_agent_inbox: returns the REAL inbox for one agent (direct +
    broadcast, newest first, unread counts) AND marks the shown messages
    read FOR THAT AGENT. Named read_agent_inbox (NOT read_inbox) because
    the mail subagent already owns the IMAP read_inbox tool + module-level
    handler — a second binding would shadow it at registry build time
    (reviewer-caught; regression guard test added).
- webui.py — run_server gains bus=None (defaults to the process singleton;
  tests pass a fresh bus). server.bus wired into LiveRuntime. NEW GET
  /api/messages -> {messages, unread, count} (real bus traffic, newest
  first, per-agent unread for badges). GET /api/agent/<name> now returns
  the agent's inbox + unread and marks it read FOR THAT AGENT (opening a
  window / selecting on the map clears ITS badge only). When memory is
  attached, every posted message is mirrored into the store (source "bus")
  so inter-agent knowledge feeds the v2.9 learning loop.
- ui/index.html — AGENT COMMS panel on the dashboard: latest bus traffic
  with per-agent unread totals, polling /api/messages every 3s.
- ui/map.html — per-node unread badges (applyInboxBadge) + an INBOX //
  INTER-AGENT COMMS section in the detail panel. pollInboxes refreshes ONLY
  the cheap unread counts so it never wipes the selected agent's inbox rows
  (reviewer-caught flicker, fixed); rows come from refreshSelectedInbox.
- ui/agent.html — INBOX section in every agent window (renderInbox),
  newest first with unread tags.
- Verification: 594 passed, 0 failed (up from 559). tests/test_message_bus.py
  (NEW, ~35 hermetic tests — fresh buses, fake fetchers, real HTTP only on
  ephemeral ports, zero network): MessageBus core (post/inbox/outbox order,
  per-recipient read state incl. the broadcast isolation case, bounded ring,
  caps, snapshot/clear, observer swallow, thread safety under 4 concurrent
  writers, singleton get/set); live broadcast (real poll text with agent
  sender, honest LIVE POLL FAILED broadcast, bus=None posts nothing);
  messenger tools (send validated/broadcast/refusals, read_agent_inbox
  real/empty/unknown-agent, mail-vs-messenger tool-name regression guard);
  HTTP (/api/messages traffic, /api/agent/<name> marks read FOR that agent,
  reading one agent's inbox leaves another's broadcast unread, singleton
  default, bus->memory mirror with FTS5 search finding source "bus"); UI
  wiring strings on all three pages. Roster-count assertions updated
  16 -> 17 in test_dispatch.py + test_general_roster.py.
- Reviewer rounds (3): round 1 caught the CRITICAL read_inbox name
  collision (mail's IMAP tool) + the live_runtime IndentationError + dead
  _notify duplication — all fixed; also flagged that unread was displayed
  but never cleared. Round 2 caught the per-recipient flaw (global
  mark_read would clear every agent's badge when one read a broadcast) —
  read_by set implemented, isolation test added. Round 3 signed off on the
  JSON-safe _public/post hardening. All signed off; 594 green.
- Re-roll: bash scripts/build_dist.sh 3.0.0.

### V2.9 — THE SYSTEM STORES DATA AND LEARNS FROM IT (Store & Learn loop)
User request: "it should store data and learn from that." The two halves
  already existed but never touched each other: sessions were persisted to
  JSONL and a SQLite FTS5 long-term store existed — but nothing auto-ingested
  completed sessions, and nothing fed stored knowledge back into the model's
  context. Now the loop is closed: every completed turn is auto-ingested, and
  each new prompt deterministically recalls the most relevant stored facts
  into its system message. Operator 👍/👎 feedback steers later recall.

- dourmouse/learn.py (NEW) — the deterministic learning loop:
  - learn_enabled(value=None): DOURMOUSE_LEARN gate (0/false/no/off/empty
    disable; optional value param mirroring live_runtime.live_enabled).
  - default_store_path(): DOURMOUSE_MEMORY_DB env else
    <workspace>/memory/atlas_memory.db (same convention as general_roster;
    deliberately duplicated, not imported — importing general_roster from
    learn.py would pull every tool backend and break chat.py's lazy-import
    design, documented in the module).
  - open_default_store(): MemoryStore or None — None when the gate is off
    OR SQLite FTS5 is unavailable (honest, never raises, Rule 2.2).
  - recall_block(store, prompt, limit=5): stopword-distilled FTS5 query
    (_distill_query drops a deterministic stopword set and keeps the most
    distinctive terms so natural prompts actually match), top-5 matches
    formatted as a REMEMBERED CONTEXT block, "" when nothing matches (the
    caller then leaves the system message untouched). Pure bm25 ranking,
    no LLM judgment (Rule 2.8).
  - record_feedback(store, session_file, rating): stores a good/bad rating
    of the LAST completed turn as a "feedback" fact carrying the exact
    user prompt + answer + rating, so later recall surfaces what the
    operator liked/disliked. Invalid rating raises ValueError.
- chat.py — ChatSession gains memory: MemoryStore|None = None (default None
  = zero behavior change for engine tests). ask(): BEFORE dispatch, recalls
  for the current prompt and rebuilds messages[0] from
  _base_system + block — a no-match turn therefore never carries a stale
  block from a previous turn (explicitly tested). AFTER a completed turn
  (final_text non-empty), auto-ingests the session file into the store
  (idempotent (source,title) upserts) — we never learn from a failed turn
  with an empty answer; a broken store mid-run is swallowed, never able to
  take down the conversation. _base_system kept in sync in _load_state so
  resume recalls against the CURRENT roster.
- webui.py — run_server gains memory=None (NO learning by default — all
  pre-existing hermetic tests untouched); server.memory wired into
  ChatSession. NEW GET /api/memory -> {active, count, gate} (count is the
  REAL fact count — evidence, not a stub). NEW POST /api/feedback: 409 when
  no store/DOURMOUSE_LEARN=0, 400 on invalid rating, 404 when no completed
  turn exists (honest ok:False, never ok:True noise), 200 ok on store.
  serve_forever opens the default store (memory=None means "open the
  default") and closes it in finally.
- desktop.py — launch() opens the default store via learn.open_default_store()
  and passes it to run_server; closed in launch's finally (symmetry with
  serve_forever).
- ui/index.html — header MEMORY stat line polling /api/memory every 3s
  ("MEMORY: N FACTS // LEARNING ON/OFF", teal when on, dim when off); a
  👍 GOOD / 👎 BAD feedback row under every completed RESPONSE that POSTs
  /api/feedback and shows the honest stored/error result in place.
- Verification: 559 passed, 0 failed (up from 518). tests/test_learn.py
  (~35 tests, all hermetic — tmp stores, fake clients, real HTTP only on
  ephemeral ports): learn_enabled value matrix + env override;
  default_store_path env precedence; open_default_store returns None under
  DOURMOUSE_LEARN=0 and when FTS5 is missing (monkeypatched _init_schema);
  recall_block no-match / no-distinctive-terms / formatted-match / stopword
  distillation; feedback facts are recalled (rating signal surfaces);
  ChatSession: completed turn auto-ingested (store.count + search finds the
  prompt), next relevant prompt's system message contains REMEMBERED
  CONTEXT + the fact, no-match turn leaves the base system message
  EXACTLY unchanged, memory=None backward compat, DOURMOUSE_LEARN=0 disables
  ingest AND recall, failed turn (raising client) is NOT ingested, resume
  keeps learning wired; record_feedback valid/invalid/no-session;
  /api/memory active-with-count + inactive-without-store over real HTTP;
  /api/feedback ok (turn run through the session first, then rated — 2
  facts: 1 ingested turn + 1 feedback) / 409 no store / 400 bad rating;
  UI wiring strings. test_desktop.py + test_live_runtime.py launch tests
  set DOURMOUSE_LEARN=0 (hermetic — no real store in the repo workspace).
- Reviewer rounds (3): round 1 verified the design (recall rebuild from
  _base_system + block prevents stale blocks, ingest gating + exception
  swallow, run_server default hermeticity, feedback route honesty across
  409/400/404/200, MemoryStore lock thread safety) and flagged 2 cleanups —
  unused _server_with_memory helper in test_learn.py (removed) and a stray
  "doe" stopword (removed). Round 2 signed off on the cleanups. The 3 test
  assertion failures were FTS5 snippet() semantics: matched terms are
  BRACKETED ([nebula]) so contiguous-phrase asserts fail — fixed to assert
  on terms and to expect 2 facts (ingested turn + feedback) in the HTTP
  feedback test. All signed off; 559 green.
- Re-roll: bash scripts/build_dist.sh 2.9.0.

### V2.8 — ALL AGENTS LIVE AND IMMEDIATELY WORKING (always-on poll loops)
User request: "all agents should be live and immediately working." The
  preloaded Live agents no longer sit idle waiting for a prompt — each runs a
  background loop that polls its REAL feed continuously and streams that
  activity into its own window, the map, and the dashboard from the moment
  DOURMOUSE starts. Every agent's native window also opens at startup.

- dourmouse/live_runtime.py (NEW) — LiveRuntime: one daemon thread per
  scheduled (agent, tool) pair. IMMEDIATE first poll at start ("immediately
  working"), then on a fixed interval (news 120s, markets gainers+losers
  120s, rnd 180s, mail 300s, tasks 60s). Results come from the REAL
  registered tool handlers (registry.lookup(tool).handler) — the exact data
  path a dispatched task uses, so live polls and chat calls agree (Rule 2.1).
  Deterministic loops, no LLM (Rule 2.8). A raising poll emits an honest
  "LIVE POLL FAILED (reported honestly): ..." line — never fabricated (Rule
  2.2). poll_count filters to agents/tools actually in the registry;
  fetcher + schedule injectable for hermetic tests (no network). DOURMOUSE_LIVE
  env gate (live_enabled(): 0/false/no/off/empty disable).
- webui.py — ActivityTracker handles NEW "live" events: maps the feed tool
  to its agent, sets status "live", updates _last, appends a feed entry
  (bounded 30). A live poll never clobbers a mid-chat computing/auth state
  (guard: only idle/live agents flip to LIVE — reviewer-caught edge).
  done/error now resets ONLY computing/auth to idle — live agents persist
  their always-on status (their loops are independent of chat runs).
  run_server gains live_polling=False (default — hermetic tests), serve_forever
  gains live_polling=True (production); server.live_runtime started when
  enabled, stopped in serve_forever's finally. _handle_chat terminal
  done/error now rides the sink (stream.emit happens inside sink) so the
  tracker resets promptly — the dashboard still receives the event EXACTLY
  once (regression-guard test added after a reviewer caught a double-emit).
- desktop.py — launch() gains live_polling=True + open_all_windows=True
  (open_all_windows=False restores the pre-v2.8 two-window behavior).
  DesktopBridge.open_all_agents(names) opens EVERY agent's own native window
  BEFORE webview.start() (the documented thread-safe pattern), deduped
  through open_agent's registry. launch's finally stops the live runtime
  (symmetry with serve_forever). Docstring no longer claims a
  DOURMOUSE_LIVE_POLLING env var (only DOURMOUSE_LIVE exists).
- UI — all three surfaces render the new LIVE status as a slow teal pulse
  and render f.type==='live' feed lines: agent.html ([LIVE] pill + LIVE
  feed row), map.html (node ring/pill + detail feed + #dstatus), index.html
  (npill.live + setNodeState 'live' branch that removes BOTH computing and
  live classes + pollLiveActivity() polling /api/activity every 2.5s so the
  cluster shows who is actively working before any dispatch).
- Verification: 518 passed, 0 failed (up from 483). tests/test_live_runtime.py
  (28 tests): poll table filtering, immediate first poll through the real
  handler path, per-agent loops, injected fetcher override, honest failure
  (no fabricated success), stop halts loops, live_enabled value matrix +
  env override, tracker live status + feed, done does NOT reset live, tool_use
  overrides live while computing, live poll does NOT clobber mid-chat
  computing, run_server wiring (live_polling starts runtime / default off /
  DOURMOUSE_LIVE=0 disables), live events land in the same tracker the windows
  poll, DesktopBridge.open_all_agents (create/dedupe/noop + launch opens
  every agent window), UI wiring strings. test_desktop.py updated: launch now
  creates 3 windows with the echo registry (main + map + agent) + an opt-out
  test; test_webui.py adds types.count("done")==1 regression guard.
- Reviewer rounds (3): round 1 caught the CRITICAL missing start() on the
  fake webview (AttributeError -> browser fallback -> _wait_forever infinite
  loop -> suite timeout) — fixed; plus a false DOURMOUSE_LIVE_POLLING docstring
  claim and a stale .live class in index.html setNodeState — both fixed.
  Round 2 signed off, adding a cleanup (launch finally stops the runtime).
  Round 3 caught a real double-emit bug: the terminal done/error event was
  being sent through sink() (which itself emits to the stream) AND
  stream.emit() again, duplicating the RESPONSE line in the dashboard feed —
  fixed to sink-only, with a regression-guard assertion + a live-clobber
  guard + test. All signed off; 518 green.
- Re-roll: bash scripts/build_dist.sh 2.8.0.

### V2.7 — EACH AGENT GETS ITS OWN LIVE DOURMOUSE WINDOW
User request: "each agent should basically open its own window of dourmouse and
  show me live what they are doing." Every subagent now has a dedicated
  native window showing its identity, toolkit, status, and a live activity
  feed — opened on first activity, from the dashboard roster, or the map.

- webui.py — GET /agent/<name> serves ui/agent.html ONLY for registry-known
  agents (404 otherwise; name unquoted + stripped BEFORE the subagent_names
  membership check so traversal attempts 404). GET /api/agent/<name> returns
  a focused live snapshot {agent:{name,domain,description,tools}, status,
  last, feed} built from the real ActivityTracker (404 unknown). Route order:
  /agent/ before /api/agent/ before /assets/ — no shadowing.
- ui/agent.html (NEW) — single-agent live window: identity header, status
  pill ([IDLE]/[COMPUTING] pulsing/[HOLD_AUTH] blinking), permission-colored
  toolkit chips, DISPATCH DIRECTIVE box (focus_agent POST to /api/chat with
  SSE streaming), LIVE ACTIVITY FEED polling /api/agent/<name> every 1s with
  a JSON dirty-check (no rebuild when unchanged — the same reviewer-caught
  pattern as the map), esc() on every server string (no XSS).
- desktop.py — DesktopBridge(map_window, webview, base_url) + open_agent(name):
  creates a native window at /agent/<name> via webview.create_window (title
  AGENT // NAME), REUSES (bring to front) an existing non-closed window
  (dedupe), recreates after close, noop on empty name. Relies on pywebview
  supporting dynamic window creation from the JS-bridge thread — VERIFIED
  against pywebview docs (queues UI ops on the GUI thread; webview.windows
  available for dedupe). open_map unchanged.
- ui/index.html — auto-opens each agent's window on FIRST tool_use of the
  session (openedAgentWindows Set — no window spam on every call), bridge
  first (window.pywebview.api.open_agent) with window.open('/agent/<name>')
  browser fallback; each roster node gained a ⧉ window button.
- ui/map.html — [LIVE WINDOW] button in the detail panel AND dispatchToAgent
  now also opens the target agent's window (after the empty-prompt guard) so
  the behavior holds from every entry point (dashboard + map + agent window).
- Verification: 483 passed, 0 failed (up from 466). webui.py 92%, desktop.py
  74% (uncovered = the real GUI entry points that can never run headless).
  tests/test_agent_windows.py (18 tests): /agent/<name> 200 for known agent
  (page served) + 404 unknown + / + /map still 200; /api/agent/<name> returns
  identity+tools+status+feed over REAL HTTP, 404 unknown, and a synthetic
  tool_use fed to the real ActivityTracker shows up live in the snapshot;
  DesktopBridge.open_agent create/reuse/recreate-after-close/separate
  windows/empty-name noop/open_map via fake webview; UI wiring string
  assertions (auto-open on first tool_use, roster ⧉ button, map [LIVE WINDOW]
  + dispatch-open, agent.html self-contained, bridge ships).
- Reviewer rounds (2): signed off on the build; then caught 3 cleanups — dead
  renderStatic() in agent.html (removed), dead Permission/_echo_tool imports
  in test_snapshot_tracks_activity (removed — this also fixed the one failing
  test), and the completeness gap that map-window dispatch didn't auto-open
  the target agent's window (fixed — dispatchToAgent now opens it). All
  fixed and re-verified. Signed off.
- Re-roll: bash scripts/build_dist.sh 2.7.0.

### V2.6 — LIVE API-KEY VALIDATION IN ONBOARDING COMPLETE (session 3 cont., 2026-08-02)
## Last updated: 2026-08-02 (session 3 cont.)

### V2.6 — FIRST-RUN ONBOARDING NOW VALIDATES THE KEY LIVE (1-token call)
User request: "Make the app's first-run onboarding validate the pasted key live
  (a real 1-token call) before writing it to .env, so an invalid or
  inference-restricted key is rejected at the prompt with a clear message
  instead of failing later with a 401/403." Triggered by a REAL incident
  earlier this session: the .env held a key that passed /v1/models but was
  HTTP 403-forbidden on the configured model (nemotron-3-super-120b-a12b),
  failing only mid-chat; the working key (…2Odh) was swapped in live.

- dourmouse/key_check.py (NEW) — validate_key_live(api_key, base_url?,
  model?, timeout?, client_factory?): makes a REAL 1-token chat completion
  through the SAME OpenAI-compatible client path the engine uses (openai SDK
  -> NVIDIA NIM), so a key that lists models but is inference-restricted is
  caught at onboarding. Format rejection (empty / < 16 chars / non-nvapi-)
  happens BEFORE any network call. Real openai v2 exceptions mapped to clear
  messages: AuthenticationError(401) -> invalid/expired/revoked,
  PermissionDeniedError(403) -> valid but NO access to model (names the
  model — the exact trap from the incident), RateLimitError(429) -> rate
  limited, APIConnectionError -> could not reach, other APIStatusError ->
  HTTP code, unexpected -> surfaced honestly (Rule 2.7). Precedence: explicit
  args > NVIDIA_BASE_URL/NVIDIA_MODEL env > shared config defaults (public
  NVIDIA_DEFAULT_BASE_URL/MODEL aliases added to config.py so key_check
  reuses the same defaults — no drift).
- CLI (main): default mode reads the key from STDIN (never argv/ps), prints
  ONLY a masked fragment (_mask: first 9 + last 4 — never the full key, Rule
  2.6), exit 0 valid / 1 rejected. NEW --check-existing mode validates the
  key ALREADY in .env via load_nvidia_config() — the stale-key trap
  (validated against the real NVIDIA_MODEL). Printed messages pass through
  DlpFilter().redact() (defense in depth: even an exception that echoed a
  credential can't leak it). argv param used (not dead).
- start.command onboarding rewritten: loop up to 3 attempts; per attempt —
  format checks, then LIVE validation via
  `printf '%s\n' "$KEY" | python -m dourmouse.key_check` (pipe, exit code
  propagates through set -uo pipefail); .env is written ONLY after live
  validation passes (umask 177 write block preserved); clear "key NOT saved"
  rejections; "No valid key after 3 attempts" abort.
- Verification: 466 passed, 0 failed (up from 443). key_check.py 95% (the 3
  uncovered lines: the __main__ sys.exit and two _mask branches — trivial).
  tests/test_key_check.py (~20 tests): happy path asserts model+max_tokens=1
  + base_url + api_key through a fake client (no network), env overrides +
  explicit-args precedence, format rejection WITHOUT any call, real openai
  exception classes -> message mapping (401/403 names the model/429/network/
  500/unexpected), masking never leaks the full key, CLI stdin + exit codes
  + no echo, --check-existing (uses env key / missing key exits 1),
  DLP-redaction of credential-shaped exception text, start.command wiring
  (live check BEFORE the .env write) + bash -n. Reviewer rounds (2): caught
  3 test bugs (env override code never honored env — fixed precedence;
  _Resp500 subclass shadowed by parent __init__ default 200 — now
  _FakeResp(500); mask assertion math 9<8 — fixed to shorter-than-input) and
  suggested the DLP-redaction + --check-existing (both added). Signed off.
- README: onboarding section documents live validation; --check-existing
  documented. Re-roll: bash scripts/build_dist.sh 2.6.0.

### V2.5 — NATIVE DESKTOP APP COMPLETE (session 3 cont., 2026-08-02)
## Last updated: 2026-08-02 (session 3 cont.)

### V2.5 — DESKTOP APP: DOURMOUSE RUNS IN ITS OWN macOS WINDOW, NOT A BROWSER
User request: "make it something that runs as a desktop app for my laptop not
  a browser." The dashboard now opens in a REAL native macOS window (WebKit)
  with the Agent Map in a SECOND native window — no browser chrome, no tabs,
  no Node, no build step (consistent with the project's zero-dependency
  philosophy).

- dourmouse/desktop.py (NEW) — native launcher: starts the SAME stdlib
  webui server on a background thread, then opens PyWebView windows at
  http://127.0.0.1:PORT. Both windows are created BEFORE webview.start()
  (the map window starts hidden=True) — the thread-safe pattern, verified
  against pywebview docs (dynamic create_window from a JS/background thread
  is backend-dependent and unsafe). The Agent Map is revealed by the js_api
  bridge (window.pywebview.api.open_map() → DesktopBridge.open_map() →
  map_window.show()) — no new window off the GUI thread. Server binds
  127.0.0.1 only; __main__ writes/removes .dourmouse-ui.pid so stop.command
  works unchanged.
- Honest fallback (Rule 2.2): if pywebview is unavailable, prints NOT
  CONFIGURED + the reason and opens the default browser instead, while the
  server keeps serving. Never a silent stub, never an unannounced browser.
- requirements-desktop.txt (pywebview>=5.0) — the desktop extra, installed
  by start.command (non-fatal: missing → browser fallback).
- ui/index.html — AGENT MAP button prefers window.pywebview.api.open_map()
  (native second window) and falls back to window.open in a plain browser.
- start.command — now installs requirements-desktop.txt and launches
  `python -m dourmouse.desktop` (nohup + pid) instead of `open`ing the
  browser; "already running" check + stop instructions preserved.
- scripts/build_app.command (NEW) — builds a double-clickable dourmouse.app
  with osacompile (ships with macOS, zero new deps): the applet runs
  start.command from its own directory via `path to me`, so a .app built in
  the staging dir works from any extraction location. Accepts an optional
  output dir ($1) — build_dist.sh calls it with "$STAGE" so the zip ships
  with dourmouse.app sitting next to start.command.
- scripts/build_dist.sh — ships requirements-desktop.txt + build_app.command
  and runs the .app builder into the staging copy.
- Verification: 443 passed, 0 failed. desktop.py 70% (the uncovered blocks
  are the real GUI entry points that can never run headless: webview.start()
  and __main__ — legitimately manual-test territory). tests/test_desktop.py
  (12 tests): port selection (default/env/invalid), NOT CONFIGURED fallback
  via the webview_loader seam asserting webbrowser.open() was really called
  + honest message, native launch creating BOTH windows with correct URLs
  (map hidden=True), bridge reveal, a REAL HTTP probe of /api/roster during
  launch (server truly live, not a stub), bash -n on all launcher scripts,
  packaging metadata (requirements-desktop.txt, desktop module in
  start.command, osacompile in build_app.command).
- Reviewer rounds (deepseek-flash) caught FOUR real issues, all fixed: dead
  prefer_browser param (docstring promised it, never implemented — removed),
  test monkeypatching start on the CLASS (would bind self into a zero-arg
  function → TypeError — patched the instance instead), wrong path in
  test_build_app_command (build_app.command lives in scripts/),  and the critical packaging bug: build_app.command run from $STAGE computed ROOT as
  dist/staging (dirname $0/..) so dourmouse.app landed OUTSIDE the staged
  folder and never shipped in the zip — fixed with the $1 output-dir arg.
  Signed off; two nits (bash -n list + usage comment) applied. Round 3:
  closing review flagged the browser fallback only covered the ImportError
  path — a runtime webview.start() failure (headless/SSH session, missing
  pyobjc) surfaced a raw traceback. Fixed: start() exceptions now degrade
  via the same honest path (clear message + browser fallback + server stays
  up), extracted into a shared _fallback_to_browser() helper (both call
  sites use it; reviewed behavior-preserving). New test
  TestStartFailureFallback with a _BrokenWebview whose start() raises,
  asserting opened[0], code==1, and both messages. 443 green.

### V2.4 — MULTI-BACKEND CODING AGENTS COMPLETE (session 3 cont., 2026-08-01)
## Last updated: 2026-08-01 (session 3)

### V2.4 — CODING AGENTS LINKED TO NVIDIA / FREEBUFF DEEPSEEK / CLAUDE
User request: "add agents linking to coding using the nvidia llm, freebuff
  free deepseek and claude." Built on the v2.3 live-intelligence roster.

- dourmouse/code_backends.py (NEW) — multi-backend coding dispatch:
  - load_backend(): nvidia (NVIDIA_API_KEY via load_nvidia_config),
    deepseek (prefers FREEBUFF_DEEPSEEK_API_KEY/_BASE_URL/_MODEL, falls
    back to DEEPSEEK_API_KEY/_BASE_URL/_MODEL, documented defaults
    https://api.deepseek.com/v1 + deepseek-chat), claude (Claude Code CLI).
    Missing key/CLI -> honest NOT CONFIGURED (Rule 2.2); unknown backend ->
    ERROR. Keys only from env (Rule 2.6).
  - run_code_task(): OpenAI-compatible path via injectable
    _openai_client_factory (referenced at CALL time, never a def-time
    default — a reviewer-caught bug where tests hit the real API with 403
    because the def-time default captured the original function and
    ignored the monkeypatch), coding system prompt, honest API-failure and
    empty-response errors; claude path mirrors the existing claude_code
    tool (timeout clamp 1-600, DEVNULL stdin, honest non-zero exit +
    empty-output errors).
- general_roster.py — _make_code_tool(backend) factory builds globally-
  unique tools code_nvidia / code_deepseek / code_claude; THREE new
  Coding-domain subagents (roster now 16). Each routes a REAL coding task
  through its backend and reports failures honestly.
- .env.example — FREEBUFF_DEEPSEEK_* + DEEPSEEK_* vars documented.
- Verification: 428 passed, 0 failed (up from 408). code_backends.py 90%.
  tests/test_code_backends.py (~19 tests): NOT CONFIGURED paths, Freebuff-
  preferred resolution + fallback + defaults, fake-OpenAI happy paths
  (model + messages asserted), API-failure + empty-response honesty, fake
  CLI roundtrip + non-zero exit, roster wiring + global name uniqueness.
  Reviewer caught: def-time default param ignored monkeypatch (fixed),
  globals() indirection (simplified to direct call-time name), dead
  Callable/json imports (removed), hardcoded default model in a test
  (now explicit). Roster-count tests updated 13->16.

### V2.3 — PRELOADED LIVE-INTELLIGENCE AGENTS + NEURAL-LINK AGENT MAP (session 3 cont., 2026-08-01)
User request: "it should have a visual neural link agent path, preload a
  bunch of agents for live news feed, stocks feed top winners and losers,
  pull from web and yahoo finance, r and d for the agents itself, my emails
  and tasks."

- dourmouse/live_feeds.py (NEW) — keyless, stdlib-only live data:
  - news_headlines(): Google News RSS parsed with xml.etree (keyless,
    verified live), honest failure on fetch/parse.
  - stock_quote(): Yahoo v8 chart API (keyless, browser UA, verified live:
    AAPL 308.91). 52-week + day ranges, currency, as_of.
  - market_movers(): Yahoo v1 screener day_gainers/day_losers (keyless,
    verified live). Never fabricates a ranking.
  - read_inbox(): read-only IMAP via stdlib imaplib, activated ONLY when
    DOURMOUSE_IMAP_HOST/USER/PASS set, otherwise honest NOT CONFIGURED;
    selects INBOX readonly, logs out in finally, never sends/deletes.
  - TaskList: deterministic local CRUD in workspace tasks.json (no LLM in
    the data path, Rule 2.8).
- general_roster.py — FIVE preloaded subagents: news (news_headlines),
  markets (stock_quote + market_movers), rnd (research_news / research_
  quote / research_movers / research_web_search / research_fetch_url —
  uniquely-named because the registry enforces globally-unique tool
  names), mail (read_inbox), tasks (list/add/complete). Roster 8 -> 13.
- webui.py — build_link_topology() + GET /api/links: nodes = every
  subagent; edges = delegate (orchestrator->all, the dispatch paths),
  memory (memory->all, the shared-truth hub), peer (same-domain clusters).
  Deterministic (Rule 2.8).
- ui/map.html — GRID / NEURAL_LINK view toggle; SVG radial layout
  (orchestrator center, memory inner hub, others on a ring grouped by
  domain sector); animated pulse on links + nodes of computing agents;
  click -> existing detail panel + search + dispatch preserved.
- Verification: 408 passed (up from 383). tests/test_live_feeds.py (~25
  tests): mocked HTTP (no live network), honest error paths, tool wiring,
  topology (delegate edges cover all agents, memory hub, peer edges
  same-domain, valid pairs). 5 stale assertions updated for the new
  roster: roster 8->13, planner/map queries that routed to the new 'news'
  agent now use unambiguous research queries.

### V2.2 — PHASE A: long-term memory (A1) + audit CLI/export (A2) + per-conversation RBAC (A3)
User request: "begin building in phases, provide a report after every phase."
Phase A from BUILD_PLAN.md — the plan said A is "ready now". All three
  sub-features shipped with tests in the same pass; reviewer-signed off.

A1 — dourmouse/memory_store.py (NEW): SQLite FTS5 full-text memory store.
- External-content FTS5 pattern (facts table + facts_fts virtual table,
  content_rowid sync, 3 triggers keep index in lockstep). Upsert by
  (source, title); FTS5-ranked search (bm25) returning source/title/
  snippet/score; snippet() marks matched terms.
- Injection-safe query builder (_fts_query): tokenizes on non-alphanumerics
  and double-quotes each term — a bare MATCH string can't inject FTS5
  syntax (tested with sneaky " OR *).
- Ingestion: ingest_session_file() indexes every turn of a session JSONL,
  ingest_vault() indexes every .md under an Obsidian vault. Idempotent via
  the (source, title) upsert.
- Honest degradation (Rule 2.2): FTS5-missing build raises
  MemoryStoreUnavailable; tools report NOT CONFIGURED — never a silent
  grep fake.
- general_roster.py: memory subagent now has remember/recall tools
  (DOURMOUSE_MEMORY_DB env override, else <workspace>/memory/atlas_memory.db),
  plus the existing search_vault/read_note/write_note.

A2 — audit tooling in dourmouse/chat.py:
- export_audit(session, out): verifies the hash chain FIRST, exports only
  an intact ledger (tampered ledger is never propagated to a compliance
  store), creates parent dirs.
- CLI: `python -m dourmouse.chat --verify <session>` (exit 0 verified /
  1 tampered) and `--export <session> <out>` — the scriptable exit-code
  contract.

A3 — per-conversation RBAC:
- ChatSession.set_role(role): applies from the NEXT turn (no mid-turn
  privilege change), validates via RbacPolicy (unknown role raises  and is NOT recorded), appends {at, from, role} (self-contained prior→new
  pair) to role_changes — an audited intervention-style event in the ledger.
- webui.py /api/role POST + server.app_role = rbac.role. ELEVATION GATE:
  a conversation can never switch to a role MORE permissive than the
  app-level DOURMOUSE_ROLE (readonly deployment cannot self-elevate to
  operator via the UI — 403 REFUSED, nothing changed; same-role reassert
  still allowed). Role name validated first (garbage role → 400, not 403).

Verification: 383 passed, 0 failed (up from 352). memory_store.py 87%,
  general_roster.py 87%, webui.py 91%. New tests: test_memory_store.py
  (store unit tests, tool wiring, FTS5-unavailable degradation) + TestAudit
  Export (export ok / tamper-refusal with out file NOT created / parent
  dirs) + TestRoleSwitch (switch+audit, invalid not recorded, per-
  conversation isolation) + TestRoleEndpoint (200 / 400 / 400 / 403
  elevation refusal with session unchanged) + CLI exit-code tests
  (verify 0/1, export 0/1) in test_governance.py.
- Reviewer rounds caught: sqlite3.Connection attribute assignment is
  impossible (FTS5 test now patches _init_schema), /api/role had no
  elevation gate (added + tested), CLI --verify/--export untested (4 exit-
  code tests added). chat.py sits at 64% — the uncovered main() block is
  the REPL/one-shot-prompt path that needs a live LLM + interactive input
  (legitimately manual-test territory, stated honestly).
- Re-roll the app: bash scripts/build_dist.sh 2.2.0.

### V2.1 — SELF-DISPATCH (recursive agent delegation) + INSTITUTIONAL GOVERNANCE (enterprise audit build)
User request: "make it so it can dispatch its own agents, and blow it out to an
  institutional level" + the enterprise spec (Compulsory: governance/RBAC/DLP/
  budgets, immutable audit, shared memory, structured output, deterministic
  orchestration; Additional: HITL, dynamic tools, async parallel, self-
  correction, cross-framework interop). Full evaluation in INSTITUTIONAL_AUDIT.md.

SELF-DISPATCH (built first):
- dispatch.py — JobTracker (bounded 500, thread-safe audit tree: spawn/finish/
  refuse/snapshot/count, task+result truncation), DispatchContext (depth/
  max_depth, SHARED budget list + consume_delegate atomic counter,
  current_job_id), run_dispatch_messages pushes/pops one ctx per in-flight
  run (invariant documented; webui serializes via session_lock).
- general_roster.py — NEW 'orchestrator' subagent with delegate_task tool:
  spawns a REAL nested run_dispatch_messages with the same client/gate/sink/
  jobs/budget; rejects empty task / unknown subagent / depth cap / budget
  exhaustion; job recorded with TRUE parent_id chain (audit tree, not flat
  list — reviewer-caught gap); result honestly truncated; untracked path
  reports '(untracked)'. Roster is now 8 subagents.
- webui.py — /api/jobs + server.jobs wiring; ui/index.html DELEGATED TASKS
  panel (polling every 1.5s, .job CSS added).

INSTITUTIONAL GOVERNANCE (dourmouse/governance.py, all deterministic Rule 2.8):
- BudgetTracker — cost-capping: caps LLM calls / estimated USD cost / wall
  time per request TREE (shared across nested runs); checked before every
  LLM call, tripped budget ends run with honest BUDGET EXHAUSTED event.
- DlpFilter — data-loss prevention at the API boundary: redacts nvapi-/sk-/
  AWS AKIA/JWT/GitHub/PEM/secret-assignment patterns from tool results AND
  model text BEFORE they reach the model messages or transcript. ON by default.
- RbacPolicy — roles: operator (all, back-compat) / readonly (read-only
  tools) / custom allow-list; refused BEFORE execution; DOURMOUSE_ROLE env,
  invalid role raises before any socket binds (webui.run_server reordered).
- Contract enforcement — validate_tool_arguments (args vs declared schema
  before handler; missing required + wrong type rejected) + ToolSpec.
  output_schema (opt-in output validation, violations surfaced honestly).
- Self-correction — _call_with_retry: transient-only retry + exponential
  backoff + optional fallback_model (NVIDIA_MAX_RETRIES / NVIDIA_RETRY_BACKOFF
  / NVIDIA_FALLBACK_MODEL on NvidiaConfig); non-transient errors never masked.
- Shared memory — _build_parent_context threads recent parent conversation
  into nested delegate runs ([PARENT CONTEXT] block) = consistent truth.
- Immutable audit — chat.py hash-chained JSONL (prev_hash + hash fields,
  tamper-evident; verify_session_audit() public checker; resume chains onto
  previous hash), elapsed_ms latency + interventions list per record.
- webui.py — /api/budget (cost snapshot + role) + DOURMOUSE_ROLE loading;
  ui/index.html COST BUDGET panel.
- Verification: 352 passed, 0 failed (up from 303). governance.py 91%,
  dispatch.py 94%, webui.py 91%. New tests/test_governance.py (~45 tests):
  budget caps runaway loop + shared across nested tree; DLP redacts before
  boundary; RBAC before-execution; schema blocks before handler;
  output_schema valid/violation/non-JSON; retry+fallback; transient vs
  non-transient classification (real openai exception classes); hash-chain
  edit AND deletion detection; latency+interventions persisted; resume
  chains; /api/budget + role-from-env + invalid-role-fails-before-bind.
  Reviewer rounds caught: vacuous gate test (made real), audit tree was flat
  (wired parent_id), dead validate_against_schema (wired output_schema),
  run_server binding before role validation (reordered), two test bugs
  (exploding-completions call count, wrong DLP test string), fake response
  missing status_code/headers for openai v2 exceptions, NameError in role
  test — all fixed and signed off.
- Re-roll the app: bash scripts/build_dist.sh 2.1.0.

### V2.0 BUILD PROMPT — Phase 0 (Security patch), Phase 1 (Sandbox), Phase 2 (Planning + workspace tools + sessions), Phase 3 (UI polish) — COMPLETE (session 3, 2026-08-01)
User pasted the v2.0 Master Build Prompt and said "that is your guide". All four
  phases executed in order, each with its Verification Gate. One honest note:
  the prompt says "commit this alone" after Phase 0 — this working directory
  is NOT a git repo (git status: fatal: not a git repository), so no commit
  could be made; the change is staged in the working tree only.

### Phase 0 — Security patch (v2.0)
Status: complete + verified live.
- dourmouse/system_access.py — _read_path_tool now calls _is_sensitive()
  BEFORE reading (previously it silently read credentials; the audit finding).
  New _SENSITIVE_FILENAME_PATTERNS applied inside _is_sensitive() via
  pat.search (suffix patterns need search, not match — reviewer-caught bug:
  match() let credentials.pem through): .env* (ANY directory — closes the
  project-root .env gap), *.pem, *.key, bare id_rsa/id_ed25519/id_ecdsa/dsa
  (closes the downloaded-key gap), .netrc, .npmrc, .pgpass. Gate now applies
  to ALL four file tools (read/write/delete + the sandbox exec tool).
  Interim _DANGEROUS_COMMAND_PATTERNS added (cat *.ssh|aws|...,
  os.remove/os.unlink/shutil.rmtree) as acknowledged stopgaps — Phase 1's
  sandbox replaces this class of protection. Module docstring updated.
- Verification: 237 passed (then 250 with Phase 1). New tests in
  test_system_access.py (TestReadPathGuard: ~/.ssh, ~/.aws, bare .env
  anywhere, .pem/.key/id_* anywhere, netrc/npmrc/pgpass, happy path intact,
  write/delete also refuse bare .env; TestRunCommandInterimPatterns).
  Reviewer round 1 caught match()-vs-search() (credentials.pem leaked) +
  stale comment — both fixed. LIVE adversarial check (real tool, real paths):
  the repo's own .env -> REFUSED; ~/.ssh/id_rsa + ~/.aws/credentials ->
  REFUSED; bare id_rsa/id_ed25519/.netrc/.npmrc/.pgpass/creds.pem/deploy.key
  created in ~/Downloads -> REFUSED; lookalikes (id_rsa_backup.txt,
  notes.netrc.md, id_ed25519.pub) still readable — no over-blocking.

### Phase 1 — Kernel-enforced sandbox for run_command (v2.0)
Status: complete + verified live at the OS level.
- dourmouse/sandbox.py (new) — sandbox-exec (macOS Seatbelt) with a
  GENERATED profile (paths resolved, because Seatbelt matches resolved paths
  and macOS /tmp is a symlink to /private/tmp — an unresolv'ed deny silently
  leaks, proven empirically). Profile: (deny default) + process/runtime
  allows + broad file-read* then targeted denies of $HOME/.ssh .aws .gnupg
  .kube .docker Library/Keychains + secret-filename regexes + writes ONLY to
  resolved workspace root + cwd + /dev literals + (deny network*) unless
  allow_network. run_sandboxed writes the profile to a temp file (-f),
  returns the same output shape as _run_shell. THE key property: NEVER
  silently falls back to unsandboxed execution — sandbox-exec missing =>
  plain NOT CONFIGURED (Rule 2.2). Tradeoff documented: sandbox-exec is
  deprecated by Apple (still shipped/functional); future-migration note +
  dedicated non-privileged-user fallback recorded in the module docstring.
  system_access._run_command_tool now calls run_sandboxed; classify_command
  is a fast-path pre-filter, NOT the safety boundary (docstring updated).
  run_privileged_command stays UNSANDBOXED by design (human-approved escape
  hatch — don't sandbox the thing whose purpose is "run exactly this after
  approval").
- Verification: 250 passed. tests/test_sandbox.py (11 tests): the EXACT three
  audit bypasses actually executed inside the sandbox and failing at the OS
  level (cat ~/.ssh/id_rsa blocked, python3 -c os.remove + find -delete on an
  OUTSIDE victim file -> victim survives with Operation not permitted),
  positive workspace work succeeds, network denied with a real curl
  (CURL_EXIT=6), .env inside the workspace still unreadable (regex deny),
  honest NOT CONFIGURED tested via monkeypatch (command must NOT run), plus
  wiring-level tests through the real run_command tool (curl denied, find
  -delete on a credential key survives). Reviewer rounds caught: module-level
  skipif defeating the honest-fallback tests on non-macOS (scoped to the
  real-sandbox classes), test_safe_command_runs breaking on non-macOS (now
  accepts NOT CONFIGURED), vacuous assertion, dead imports — all fixed.
  LIVE re-test of the three bypasses against a staged fake-home .ssh and
  outside victim dir: BYPASS 1 blocked (Operation not permitted), BYPASS 2
  and 3 both failed at the OS level with victims surviving, /dev/null
  redirects work, curl_exit=6.

### Phase 2 — Planning + workspace tools + session resume (v2.0)
Status: complete.
- Phase 2.1 dourmouse/planner.py (new) — NO separate planning LLM call
  (reuses the same NVIDIA model; avoids doubling latency/cost for simple
  requests). looks_multi_step(): cheap deterministic heuristic — sequencing
  markers (" then ", " after that", " and also", ...) OR 2+ outcome verbs
  counted by a word-boundary regex ("search and summarize" in ONE clause
  counts as two). build_plan(): splits the prompt into numbered subtasks,
  maps each to the best subagent via find_agents_for_query (token-overlap
  scoring, Rule 2.8; fallback "orchestrator"), bounded by max_steps.
  find_agents_for_query + _STOP_WORDS MOVED here from webui.py (webui
  re-exports so test_map.py's import still resolves). dispatch.py emits a
  visible {"type":"plan","steps","total"} transcript event BEFORE the tool
  loop for multi-step prompts; because it rides the transcript, chat.py
  persists it to the session JSONL — auditable for arbitrary sessions.
- Phase 2.2 general_roster.py — search_files (grep -rn subprocess with
  pure-Python fallback, workspace-scoped, file:line:match), diff_preview
  (unified diff WITHOUT writing), write_file now returns the diff when the
  target exists (for_write=True header: "DIFF (what changed in this write):"),
  edit_file (str-replace with EXACT-ONCE uniqueness; 0 or 2+ matches refused
  — silent multi-match edits are a correctness hazard). All three registered
  on dev_coding.
- Phase 2.3 webui.py — GET /api/sessions/recent surfaces REAL data already
  on disk (workspace/sessions/*.jsonl): first user message + last answer +
  turn count per session, newest first. ui/index.html gains a RESUME SESSION
  picker in the telemetry header; selecting one prints a [RESUME] summary
  line into the feed.
- Verification: 279 passed. tests/test_planner.py (heuristic, plan building
  + subagent mapping, determinism, max_steps, orchestrator fallback, webui
  re-export identity, plan event first in transcript for multi-step / absent
  for single-step, plan persisted to session JSONL); TestPhase2WorkspaceTools
  in test_general_roster.py (search/diff/edit real behavior incl. diff
  without writing + exact-once edit); test_sessions_recent in test_webui.py.
  Reviewer rounds caught: my own heuristic bug (clause-count vs verb-count),
  the MISSING 4.1 requirement (tool-uses not tied to plan steps), dead
  import re in webui.py, misleading "(not written)" diff header after a real
  write, unguarded search_files max_results, unused fixtures, 1-step plan
  for "Search then summarize" — all fixed (see Phase 3 for the UI fix).
  LIVE round-trip through the real NVIDIA API: "Search the web for NVIDIA
  Nemotron news, then draft a short email about it" -> PLAN event
  [1->research_info, 2->comms] emitted BEFORE execution, then the model
  really called web_search and draft_message and gave a real final answer.

### Phase 3 — UI polish (v2.0)
Status: complete.
- ui/index.html — [PLAN] block (violet STEP n/total lines) rendered on the
  plan event; the MISSING 4.1 requirement implemented: each tool_use now
  finds the next unconsumed plan step whose subagent matches the tool's
  agent and prefixes the DISPATCH_ROUTING line with STEP k/n (activePlan
  resets on send/done); [SANDBOXED] badge on run_command tool cards that
  flips to [UNSANDBOXED] when the result honestly reports NOT CONFIGURED
  (never a silent stub — the safety mode is visible); session picker.
- Verification: covered by the 279-test suite (UI logic exercised via the
  SSE handler code review); live in-browser round-trips confirmed the plan
  event renders and the sandboxed run_command shows [SANDBOXED].
Re-roll the app: bash scripts/build_dist.sh 2.0.0.

### Session 2 prior work (unchanged, still accurate):
## Current Phase (session 2 view): 1 (in progress) + GENERAL DISPATCH AGENT BUILT + COWORK-STYLE CHAT + INTERNET TOOLS + AGENT MAP + CLAUDE CODE BRIDGE
## Last updated: 2026-08-01 (session 2)

### Phase 1 — Orchestrator Skeleton + Research Agent
Status: in progress. (a) RESOLVED — real NVIDIA API round-trip done and shown
  live (see Verification below). (b) still blocks full completion: real ATLAS
  output flowing through the orchestrator — blocked on user providing
  ATLAS_REPO_PATH ("i will provide the repo path later"). Do not claim Phase 1
  complete until a real ATLAS run happens and is shown live.
User provided NVIDIA_API_KEY directly in chat (2026-07-31). Placed into a new
  .env file (git-ignored; confirmed via .gitignore, not yet a git repo so
  git check-ignore couldn't run but pattern is present for when it is). Key
  masked in all conversation output per Rule 2.6 (last 4 chars: ...vO4x).
  Advised user that pasting secrets into chat isn't ideal going forward.
  Added python-dotenv (requirements.txt) + load_dotenv() in config.py
  (resolves project-root .env by file location, independent of caller cwd)
  since nothing previously auto-loaded .env into the process environment.

ARCHITECTURE DEVIATION (Section 4), flagged and explicitly agreed by user:
  orchestrator LLM backend changed from Claude Agent SDK / local `claude` CLI
  to NVIDIA NIM (OpenAI-compatible API), per user instruction "everything run
  off nvidia, the best nvidia model practically usable". This is a real
  rework, not a config flag — claude_agent_sdk's tool-dispatch/session
  machinery was replaced with a hand-rolled tool-calling loop in
  orchestrator.py using the `openai` client pointed at NVIDIA's endpoint.
  Scope of the deviation: orchestrator only, for now (Research/Memory have no
  LLM reasoning of their own yet to move). Phase 0 guardrails.py is UNAFFECTED
  — Rule 2.8 requires it stay deterministic Python regardless of orchestrator
  backend, and it has no LLM in its path either way.
  Model chosen (researched live via WebSearch/WebFetch against NVIDIA's own
  docs, not assumed from stale training knowledge): nvidia/nemotron-3-super-120b-a12b
  (NVIDIA's own docs: "excelling in agentic reasoning, coding, planning, tool
  calling", MoE with ~12B active params despite 120B total, 1M context).
  Documented fallback if this model proves flaky/rate-limited:
  nvidia/llama-3.3-nemotron-super-49b-v1 (older, smaller, well-documented
  tool-calling support w/ dedicated tool parser, BFCL benchmarks).
  Old auth decision (local claude CLI, Pro subscription) is now MOOT —
  superseded by this pivot. claude-agent-sdk pip package still installed in
  .venv but no longer imported anywhere in dourmouse/ (harmless, unused).
NVIDIA API key: user has it, confirmed purpose = orchestrator backend (see
  deviation above). NOT YET actually placed in .env (checked — no .env file
  exists). Cannot do a real live round-trip until it is.
Built/rewritten this session:
- dourmouse/config.py — added NvidiaConfig + load_nvidia_config(); raises
  ValueError loudly if NVIDIA_API_KEY missing (Rule 2.2/2.6 — no default key,
  no fabrication). base_url/model have sane defaults, overridable via env.
- dourmouse/research_agent.py — tool interface reworked from
  claude_agent_sdk's async @tool/SdkMcpTool to a framework-agnostic sync
  pair: RESEARCH_TOOL_SPEC (OpenAI function-calling JSON schema) +
  call_research_tool(arguments) -> str. Core logic unchanged: still resolves
  ATLAS_REPO_PATH/ATLAS_VENV_PATH, still raises AtlasNotConfiguredError loudly
  when missing, still subprocess-calls real Atlas().research() via
  _atlas_runner.py once configured. Never fabricates champions/results data.
- dourmouse/_atlas_runner.py — unchanged; runs INSIDE ATLAS's own venv,
  calls real atlas.core.Atlas().research(). Not yet exercised for real (no
  ATLAS venv exists yet).
- dourmouse/orchestrator.py — REWRITTEN. Hand-rolled tool-calling loop
  against an OpenAI-compatible client (openai==2.52.0, installed & verified
  importable). `dispatch(prompt, max_turns, client=None, config=None)` —
  client/config injectable for isolated testing (Integration Rule 7.3)
  without hitting the real NVIDIA API. Bounded by max_turns (default 6) so a
  model that never stops calling tools can't loop forever — verified by test.
- New/updated tests: test_orchestrator.py (9 tests, fake-client doubles for
  the external API boundary only — never fabricates ATLAS/trading data),
  test_config.py (+3 NvidiaConfig tests), test_research_agent.py (updated for
  sync tool interface).
Verification evidence (REAL, shown live this session, not fabricated):
- pytest: 67 passed, coverage: config.py 100%, guardrails.py 100%,
  orchestrator.py 92% (only the trivial `__main__` CLI stub uncovered),
  research_agent.py 78% (only the real-ATLAS-subprocess success path
  uncovered — can't test without a real ATLAS install, by design not faked).
- Real live CLI run with NVIDIA_API_KEY deliberately unset (before key was
  provided): clean uncaught ValueError: "NVIDIA_API_KEY is not set. ... Add
  it to .env (never hardcode it; see .env.example)." Proved the real code
  path (not a mock) correctly refuses to proceed without credentials.
- *** REAL LIVE NVIDIA ROUND-TRIP, SUCCEEDED (2026-07-31) ***:
  `python -m dourmouse.orchestrator "Run ATLAS research on SPY."` against
  the actual NVIDIA NIM API (nvidia/nemotron-3-super-120b-a12b) ->
  model correctly emitted a well-formed tool call
  (run_atlas_research, args {"symbols":["SPY"],"population_size":20,
  "generations":4,"windows":3}), our loop dispatched it, tool honestly
  returned NOT CONFIGURED (ATLAS_REPO_PATH unset), model's final answer
  honestly relayed that to the user without fabricating any research data.
  This proves: real auth works, real tool-calling format works end-to-end,
  the model respects the "never invent results" system-prompt instruction.
  Auth decision (B) and NVIDIA pivot are now FULLY verified live, not just
  unit-tested against fakes.
- .env.example updated with NVIDIA_API_KEY / NVIDIA_BASE_URL / NVIDIA_MODEL
  (defaulted to nemotron-3-super-120b-a12b) and ATLAS_REPO_PATH/ATLAS_VENV_PATH.
- requirements.txt created (openai>=2.52.0, python-dotenv>=1.0.0) — first
  real runtime dependencies.
Still open before Phase 1 can be marked COMPLETE:
  1. User to provide real ATLAS_REPO_PATH.
  2. Build an ATLAS-dependency venv (pandas/scipy/etc, currently missing —
     `import atlas` fails with `No module named 'pandas'`), point
     ATLAS_VENV_PATH at it.
  3. Run the orchestrator again with ATLAS configured and show REAL
     champions/results JSON flowing back through it (the actual Phase 1 exit
     criterion) — driven by the NVIDIA model, now proven to work end-to-end.
Findings (verified earlier this session, still accurate):
- Canonical ATLAS repo (95% confident, awaiting confirm):
  /Users/aditagrawal/Projects/atlas project/01_CURRENT_atlas_work/atlas_v9_phase9
  ATLAS v9.8.0, paper-only. Only candidate with BOTH CrossSectionalRanker
  (atlas/strategies/cross_sectional.py, Phase 6 anchor) AND full pipeline
  (atlas/core.py, validation/walk_forward.py, backtesting/engine.py).
  Other candidates: ATLAS-atlas-v5 (older v5), ATLAS-MKRP (older).
- Real entry point to wrap (NOT reimplement):
  Atlas().research(market_data: dict[str,pd.DataFrame], population_size=20,
    generations=4, windows=3, benchmark=None, portfolio_method="greedy",
    lift_exposure=True) -> dict   [atlas/core.py:24]
  Data via ATLAS Downloader(default_router()); keyless Yahoo/Stooq fallback.
- ENV GAPS: (1) ATLAS has no venv; deps not installed (import atlas -> no pandas).
  Plan: dedicated venv OUTSIDE the ATLAS repo (under this project) + pip install
  -r requirements.txt. (2) claude_agent_sdk not installed, ANTHROPIC_API_KEY not
  set, but authenticated `claude` CLI present -> orchestrator can auth via CLI.
Plan: research_agent.py (subprocess into ATLAS venv, calls real research()),
  orchestrator.py (Agent SDK, registers research tool). Verify = real run.

### General Dispatch Agent (RUN:GENERAL) — BUILT session 2, 2026-08-01
Status: complete (Phase 7-style entry per master prompt Section 11; the General
  roster exists and is tested; Trading roster slots in later with zero rework).
User request: "build the dispatch agent system from the ground up... build the
  general dispatch agent using an NVIDIA LLM api key i will provide. use the
  file zip attached as reference for the design." Reference zip (dourmouse-ai-
  assistant-main.zip) extracted to .freebuff/reference/ (git-ignored) and used
  as DESIGN reference only — central router dispatching to capability modules,
  feature-module structure, JSON state files. Nothing copied verbatim (it is a
  Windows/Groq/Gemini monolith).
Built from the ground up:
- dourmouse/dispatch.py — reusable dispatch ENGINE. DispatchRegistry (the
  single extension point: register_subagent, subagent_names/tool_names/
  gated_tool_names), ToolSpec (name/description/params/handler/permission/
  confirm_prompt), Subagent, Permission tiers (REGULAR / REQUIRES_CONFIRMATION
  / PROHIBITED) enforced deterministically in run_dispatch() via
  _execute_tool() (Rule 2.8). NVIDIA-NIM-backed tool-calling loop (same
  transcript shape as orchestrator.py; client/config injectable for isolated
  tests). confirmation_gate hook = the human-in-the-loop; NO gate => gated
  tools NEVER execute (returns CONFIRMATION REQUIRED). PROHIBITED => REFUSED.
  Unknown tools / malformed JSON / handler exceptions => honest error text.
  CLI: python -m dourmouse.dispatch "<prompt>" with stdin y/N gate.
- dourmouse/general_roster.py — the six General subagents (v2.0 Section 4):
  research_info (live keyless Wikipedia search, stdlib urllib), comms (draft
  REAL + saved to workspace/drafts; send confirmation-gated + NOT CONFIGURED),
  scheduling (deterministic propose_time_slots; calendar read-only NOT
  CONFIGURED), dev_coding (run_python real subprocess in workspace; read/write
  file path-traversal-guarded; deploy gated + NOT CONFIGURED), admin_ops
  (list_files read-only; delete_file per-item confirmation-gated, real delete
  after approval), memory (Obsidian vault via OBSIDIAN_VAULT_PATH,
  filesystem-backed until Phase 2 MCP fix; search/read/write, escape-guarded).
  Path-traversal guards on every file/vault tool via _safe_resolve().
- Open-ended for ATLAS: Trading roster (Research/Monitoring/Risk/Execution)
  registers later via the SAME DispatchRegistry — proven by test
  (trading subagent added post-hoc and dispatched). No engine changes needed.
- Permission decisions (recorded per review): run_python REGULAR by design
  (user asked for a coding agent; "local code changes" = Regular tier; doc'd
  in general_roster.py with the one-word flip path).
Verification evidence (REAL, shown live this session):
- pytest: 108 passed, 0 failed (1.46s). New: tests/test_dispatch.py
  (registry, loop, permission tiers, real-client construction, max_turns,
  open-ended trading-extension) + tests/test_general_roster.py (real tool
  behavior: filesystem, subprocess, vault, canned-HTTP web search,
  engine-gated real delete). Coverage: dispatch.py 90%, general_roster.py 87%
  (missed = CLI block, network-error branches, confirm_prompt lambdas).
- Prior Phase 0/1 suites still green (108 total incl. all previous tests).
- Code-reviewed twice (deepseek-flash); findings fixed: broken register_tool
  removed, public registry accessors added, real delete handler tested,
  tool-specs-asserted-to-model, friendly int-arg errors, draft filename
  microsecond-resolution.
Environment note: .env.example is blocked from file-tool edits (dotfile
  guard); the ONE newly consumed var DOURMOUSE_WORKSPACE defaults to
  <project>/workspace so nothing breaks; OBSIDIAN_VAULT_PATH already listed.
Still open for General Dispatch:
  1. NVIDIA_API_KEY still not placed in .env (user said "I will provide") —
     live round-trip blocked until then; missing key raises loudly.
  2. Comms/Scheduling/Deploy backends + calendar are NOT CONFIGURED by design
     until Phase 3 wiring; all honest.
  3. STATE.md still not created (v2.0 says it is the canonical state file) —
     PROGRESS.md continues to serve; migration decision still pending.

### Cowork-style conversational front end + internet access — BUILT (session 2 cont., 2026-08-01)
User request: "it should run as basically a version of claude cowork with
  internet acess and so forth" — i.e., a conversational multi-turn assistant
  with real internet tools, on top of the General Dispatch Agent.
Built (all REAL, evidence shown live):
- dourmouse/chat.py — Cowork-style conversational layer. ChatSession
  keeps the full OpenAI-format message list (system + history) so the NVIDIA
  model has context across turns; ask() runs the shared engine loop and
  persists every turn to <workspace>/sessions/<ts>.jsonl (audit) plus a
  .messages.json state snapshot for RESUME. Interactive REPL + one-shot mode:
  `python -m dourmouse.chat` / `python -m dourmouse.chat "prompt"`,
  with the confirmation gate wired to real y/N prompts. History stays
  well-formed on max-turns exhaustion and API failure (assistant tail
  guaranteed); corrupt state raises loudly, never silently resumes.
- dispatch.py refactor (backward compatible): system_message(registry)
  helper + run_dispatch_messages(messages, ...) conversation-aware loop;
  run_dispatch() is now a thin wrapper returning the same {final_text,
  transcript} shape (all prior tests untouched and green).
- general_roster.py — real internet tools added to research_info:
  fetch_url (keyless http(s) page fetch, HTML-stripped, path-scheme guard,
  honest errors) and open_url (opens the user's browser). web_search upgraded
  from Wikipedia-only to DuckDuckGo general search FIRST with Wikipedia
  fallback and honest error aggregation; DDG hrefs normalized to https:.
Verification evidence (REAL, shown live this session):
- pytest: 129 passed, 0 failed. Coverage: dispatch.py 91%, general_roster.py
  88%, chat.py 55% (REPL/CLI block is the uncovered bulk), 91% overall.
  New tests: test_chat.py (multi-turn memory, full history sent to model,
  tool-flow history, persistence + resume + corrupt-state, system-prompt
  re-injection on resume, gated tools through chat, max-turns exhaustion,
  API-failure well-formedness) + internet-tool and DuckDuckGo-first tests.
- LIVE one-shot round-trip through the chat front end succeeded:
  `python -m dourmouse.chat "Use the scheduling tool to propose two
  30-minute slots..."` — model called propose_time_slots with real args,
  engine returned real slots, final answer picked two and HONESTLY reminded
  the user "No booking was made". Session persisted to workspace/sessions/.
- Code-reviewed after each round (deepseek-flash): max-turns well-formedness,
  finally-block persistence, resume system-prompt re-injection, DuckDuckGo
  regex + https normalization — all fixed and signed off.
Notes:
- workspace/ added to .gitignore (sandbox + session files).
- .env unchanged; NVIDIA key already present (masked).

### Web UI (DOURMOUSE HUD) for the dispatch agent — BUILT (session 2 cont., 2026-08-01)
User request: "now for the ui" — a polished web front end for the General
  Dispatch Agent, with live tool-call visibility and inline approve/decline
  for confirmation-gated actions.
Built (all REAL, evidence shown live):
- dourmouse/webui.py — stdlib-only ThreadingHTTPServer (no new deps).
  POST /api/chat streams SSE transcript events live via the new event_sink
  on run_dispatch_messages (tool_use, tool_result, assistant_text,
  confirmation_requested, done, error); WebConfirmationGate = threading.Event
  human-in-the-loop gate resolved via POST /api/confirm; /api/roster +
  /api/sessions JSON APIs; static ui/ serving with path-traversal guard;
  Connection: close so SSE streams EOF after done/error. One shared gate per
  server with wiring + restore atomic under session_lock (concurrent chat
  requests serialized; terminal emits after lock release; no double-emit).
- ui/index.html — DOURMOUSE HUD front end: dark radar/scanline aesthetic, pulsing
  orb, subagent roster sidebar with permission chips (gated/prohibited
  color-coded), live tool-activity cards (click to expand ARGS/RESULT),
  amber confirmation cards with Approve/Decline buttons, typing indicator,
  markdown rendering (code, bold, links, headers, list grouping), escaped
  error bubbles (no innerHTML injection), persistent multi-turn session with
  full conversation memory via the shared ChatSession on the server.
- dispatch.py — event_sink param on run_dispatch_messages (pure observer:
  _emit_event wraps it in try/except so a raising sink never aborts
  dispatch); get_subagent/all_subagents public accessors.
- chat.py — event_sink passthrough on ChatSession.ask.
Verification evidence (REAL, shown live this session):
- pytest: 140 passed, 0 failed. Coverage: dispatch.py 91%, general_roster.py
  88%, chat.py 61%, webui.py 89%, overall 91%. New tests/test_webui.py uses
  REAL HTTP round-trips (server on port 0) incl. SSE event framing, an
  approve flow (confirmation_requested -> POST /api/confirm -> tool
  executes) and a decline flow (tool NOT executed), plus roster/sessions
  endpoints.
- LIVE round-trip through the running UI verified in-browser twice:
  (1) "Propose two 30-minute meeting slots" -> propose_time_slots tool card
  streamed with real ARGS + RESULT, final answer rendered as a numbered list
  picking Monday 09:00-09:30 + 09:30-10:00; (2) after the dedupe fix, a
  45-minute variant rendered exactly ONE assistant bubble (previously the
  terminal assistant_text + done.final_text double-rendered — fixed by
  comparing final_text against lastAssistantText).
- Three review rounds (deepseek-flash); findings fixed: confirm-resolver
  wiring, per-request session persistence, SSE keep-alive hang (close
  connection + stop reading on done AND error), shared-gate emit race
  (wiring moved inside session_lock), unescaped error text, duplicate
  assistant bubble.
Run it: ./.venv/bin/python -m dourmouse.webui  ->  http://127.0.0.1:8765
  (server currently running in background; log at .freebuff/webui.log).

### DOWNLOADABLE APP PACKAGE — BUILT (session 2 cont., 2026-08-01)
User request: "present the final project as a downloadable application for my
  laptop". Shipped dist/dourmouse-1.0.0.zip (84 KB, sha256
  9567d41b8352ec61a8809bc6fb52367f4de4179ab81c5b42f9c0ee8344f4943d) — a
  self-contained macOS app you unzip anywhere and double-click:
- start.command — double-clickable launcher: auto-finds Python 3.10+ (Homebrew
  + system + Framework paths), creates .venv, pip installs requirements.txt,
  FIRST-RUN onboarding asks for the NVIDIA API key (trimmed, validated
  nvapi-<token> min 16 chars, written to .env under umask 177 then restored
  so later mkdir/redirects keep sane perms, chmod 600, never echoed),
  boots the web UI server via nohup + pid file, polls /api/roster until up,
  opens the browser; detects an already-running instance.
- stop.command — kills the pid-file process (or anything on DOURMOUSE_UI_PORT).
- scripts/build_dist.sh — stages a clean copy (engine + ui + launchers +
  README + manifests), scrubs __pycache__/.pyc/.coverage/.DS_Store/
  workspace/.env/pid/log, asserts .env/.venv/workspace absent, greps for
  real-looking NVIDIA_API_KEY values (nvapi-<token> — no false positives on
  the empty .env.example line or README doc example), zips + prints sha256.
- README.md — install/first-run/config/security/troubleshooting guide.
Verification evidence (REAL, shown live this session):
- pytest: 140 passed, 0 failed (dispatch 91%, general_roster 88%, chat 61%,
  webui 89%, overall 91%).
- Zip leak audit: unzip -l greps for .env/.venv/workspace/.pyc/__pycache__/
  .coverage/.DS_Store/.freebuff/pid/log -> NO matches; start/stop.command
  ship with exec bits (-rwxr-xr-x) preserved.
- bash -n passes on start.command, stop.command, build_dist.sh.
- END-TO-END first-user smoke test: extracted the zip to a fresh temp dir,
  found python3.12, created .venv, pip-installed, imported openai+dotenv,
  booted the server (port 8799) -> /api/roster returned real JSON, / served
  200, and a chat request WITHOUT a key honestly streamed the
  NVIDIA_API_KEY-is-not-set error (no stub, no fabrication).
- FIRST-RUN ONBOARDING simulation (the flagged regression path): piped a
  fake nvapi- key into start.command on port 8797 -> .env written with the
  key (perms -rw------- 600), workspace created with sane perms
  (drwxr-xr-x, NOT 600 — the umask-restore fix), server came up (api:200),
  a chat request successfully wrote session_*.jsonl + .messages.json into
  workspace/sessions (NO EACCES), and the real NVIDIA 403 for the fake key
  was surfaced honestly in the SSE stream. Launcher logged "✓ dispatch core
  online".
- Three review rounds (deepseek-flash) over the packaging layer; findings
  fixed: (1) secret-grep false-positives -> tightened to nvapi-<token>{8,};
  (2) umask 177 leaking into later mkdir/redirect (would have created a 600
  workspace dir and crashed first-run session writes) -> OLD_UMASK
  capture/restore around only the .env write; (3) sed-unsafe key chars /
  bare-nvapi paste -> case pattern nvapi-[A-Za-z0-9._-]* + min 16 chars.
To rebuild: bash scripts/build_dist.sh [version].

### FULL LAPTOP ACCESS (Claude-Cowork scope) — BUILT (session 2 cont., 2026-08-01)
User request: "give it full access to my laptop like claude cowork". Added a
  seventh ``system`` subagent (dourmouse/system_access.py) that operates
  across the whole machine, not just the workspace sandbox:
- read_path / write_path / list_path / delete_path — any file anywhere by
  absolute path. Deletion is REQUIRES_CONFIRMATION (engine-gated). Writes/
  deletes refused inside credential/system dirs via parts-based guard
  (.ssh/.aws/.gnupg/.kube/.docker/Keychains as ANY path component, so it
  works for any home dir; /etc /usr /System /Library/Keychains /private/etc
  by prefix; slash-delimited .config/gcloud check — no gcloud-sandbox false
  positives).
- run_command (REGULAR) with a deterministic danger classifier (Rule 2.8 —
  pure code, not an LLM call): blocks sudo/doas/pkexec, git push, rm/rmdir,
  curl|sh remote execution, brew/apt/dnf/yum install (global by default),
  pip/npm -g/--global, dd to /dev, mkfs/fdisk/diskutil, shutdown/reboot/
  launchctl, killall/pkill, kill -9 -1, chmod on system roots, redirects
  into system paths. REFUSED commands route to:
- run_privileged_command (REQUIRES_CONFIRMATION) — the escape hatch: runs
  ANY command after human approval, surfacing the exact command in the UI
  INTERVENTIONS column.
- system_info (real platform/CPU/memory/disk), open_path (Finder/default
  app), clipboard_get/set (macOS pbpaste/pbcopy; honest NOT CONFIGURED
  otherwise). Output capped at 20k chars; timeouts bounded.
- dispatch.py system prompt gained rule 6: never rephrase a refused command
  to sneak past the guard; use run_privileged_command for genuine needs.
Verification evidence (REAL, shown live this session):
- pytest: 183 passed, 0 failed. New tests/test_system_access.py: classifier
  parametrized over 19 dangerous + 9 safe commands; full-scope file
  read/write/list/delete on tmp_path (outside workspace); sensitive-path
  refusals; gated run_privileged_command (approved executes / declined does
  not / no-gate never executes); permission-tier assertions; roster payload
  includes system. Updated roster-shape tests to seven subagents.
- Four review rounds (deepseek-flash) over the layer; findings fixed: dead
  `/.ssh`-style prefix guard (never matched real paths) -> parts-based;
  brew/apt/dnf/yum global installs bypassing the -g-only rule -> blocked
  unconditionally; added rmdir + killall/pkill; user keychains (~/Library/
  Keychains) unguarded -> Keychains component; .config/gcloud substring
  false-positive -> slash-delimited; redirect-into-system-path rule
  (dropped leading \s so >/etc/hosts without a space is caught too).
- LIVE in-browser round-trip (dev server restarted with the new roster):
  directive "read README.md + run echo laptop-access-works" -> tool cards
  streamed REAL read_path output (full README text) and REAL run_command
  output (EXIT CODE: 0 / STDOUT: laptop-access-works); final answer honestly
  reported both. Roster API shows all 7 subagents incl. system with its 10
  tools.
Safety model: reads/local changes proceed; destructive/privileged require
  human confirmation; credentials stay prohibited (Rule 2.6); the classifier
  is a guardrail-not-sandbox (documented) with the gated escape hatch as the
  honest path.

### Web UI rebuilt to PRODUCT SPEC (mission-control dashboard) — session 2 cont., 2026-08-01
User provided the official product spec PDF ("# PRODUCT SPECIFICATION &
  ENGINEERING PROTOCOL: DOURMOUSE DISPATCH AGENT") and said "that is your
  guide". Rebuilt ui/index.html from the DOURMOUSE HUD into the spec's
  mission-control layout — same zero-dependency single-file architecture
  (webui.py backend contract UNCHANGED; no build step introduced):
- Top telemetry header: "DOURMOUSE // CENTRAL AGENT DISPATCH" + heartbeat
  banner [AGENT PIPELINES: 100% OPERATIONAL // DESYNC RISK: 0.02%], live
  clock with running seconds, fluctuating MEM_LOAD counter, session label.
- Left sidebar "AGENT ORCHESTRATION CLUSTER": the REAL roster from
  /api/roster rendered as worker nodes (rotating SVG telemetry wheels, live
  [SPEED: n t/sec] counters, tool chips color-coded by permission tier, and
  status pills that flip [READY] -> [COMPUTING] (pulsing cyan) on tool_use
  -> [HOLD_AUTH] (flashing amber) on confirmation_requested).
- Center: System Core Arc Matrix SVG (5 concentric counter-rotating dashed
  arcs that accelerate while busy + glowing hub, status flips
  IDLE<->COMPUTING) above the PIPELINE DISPATCH FEED — timestamped terminal
  lines ([DIRECTIVE]/[DISPATCH_ROUTING]/[TOOL_RESULT]/[SYSTEM]/[HOLD_AUTH]/
  [ERROR]) with expandable tool cards (ARGS/RESULT auto-open on result) and
  md() rendering for answers (lists/strong/code/links). Quick-directive chips.
- Right sidebar: TRANSACTION METRICS micro-bars (Queue Throughput, Vector DB
  Latency, Open Serverless Instances — live simulated telemetry) + the
  INTERVENTIONS column where every confirmation_requested renders a flashing
  amber intervention frame with [EXECUTE_PATCH] / [BYPASS_NODE] button pair
  wired to /api/confirm (approved -> "PATCH EXECUTED", declined -> "NODE
  BYPASSED").
- Bottom SYSTEM CONSOLE: ultra-wide glowing input with the spec placeholder
  ("SYSTEM CONSOLE STANDBY // ENTER DISPATCH COMMANDS OR DIRECTIVE
  STRINGS..."), Enter or Cmd/Ctrl+Enter to broadcast, [SEND_DIRECTIVE] button.
Verification evidence (REAL, shown live this session):
- pytest: 140 passed, 0 failed. Coverage: dispatch 91%, general_roster 88%,
  chat 61%, webui 89%, overall 91%.
- Two review rounds (deepseek-flash); findings fixed: unescaped tool name in
  tool-card header (now esc(name), consistent with the escaped fline usage),
  dead const count removed. Prior safety properties preserved: every
  server-provided string escaped or textContent'd, assistant_text + done
  final_text dedupe via lastAssistantText, SSE termination on done AND error,
  busy-state hygiene with node resets in finally.
- LIVE in-browser round-trip verified: "Propose two 45-minute meeting slots"
  -> DIRECTIVE logged, tool card streamed with real ARGS {"days_ahead":5,
  "duration_minutes":45} + real RESULT (slots list), node went COMPUTING,
  core IDLE->COMPUTING->IDLE, final answer rendered as a numbered list in
  the feed, send re-enabled. Roster cluster renders all 6 real subagents
  with [READY] pills; metrics fluctuate live.

### CLAUDE CODE BRIDGE — dev_coding delegates coding work to the user's real Claude Code CLI — BUILT (session 2 cont., 2026-08-01)
User request: "i also want to link it to my claude code". The dev_coding
  subagent gained a claude_code tool that runs a task through the user's
  ACTUAL Claude Code CLI in headless mode (`claude -p <task>`) and returns
  the real stdout/stderr:
- dourmouse/general_roster.py — _find_claude_cli() (CLAUDE_CODE_CLI env
  override first, then PATH 'claude'; None honestly when missing) +
  _claude_code_tool(): subprocess.run([cli, '-p', task], cwd, timeout
  clamped to [1, 600], stdin=DEVNULL so claude -p doesn't wait ~3s on
  stdin) — REAL output only (Rule 2.1): honest NOT CONFIGURED when the CLI
  is missing, honest timeout/OSError/non-zero-exit messages, 20k-char
  output cap with truncation marker. Registered as a REGULAR-tier tool on
  dev_coding (task/cwd/timeout_seconds). Tool description notes headless
  mode runs with default permissions (permission-gated edits typically
  declined). No shell=True — the task is one argv element, injection-safe.
- .env.example — CLAUDE_CODE_CLI documented (auto-detect claude on PATH
  when unset).
- tests/test_claude_code.py (100% coverage) — registration on dev_coding +
  regular tier + roster payload; CLI discovery (env override, PATH, not
  found); missing-CLI honest NOT CONFIGURED; empty-task error; REAL
  subprocess round-trip against a FAKE executable (ARGV/CWD captured —
  proves real subprocess without burning Claude Code credits); non-zero
  exit surfaces stderr; timeout honesty; zero/cap/limit clamping;
  non-integer timeout; truncation (cap monkeypatched small); OSError via
  dead-shebang exec failure.
Verification evidence (REAL, shown live this session):
- pytest: 225 passed, 0 failed. general_roster.py 90%, overall 91%.
- Two review rounds (deepseek-flash); findings fixed: truncation test math
  (400 lines ~8k chars never crossed the 20k cap -> cap monkeypatched to
  200), OSError test routed a dir path to NOT CONFIGURED instead of ERROR
  -> dead-shebang exec failure, timeout 0 clamped to min 1, unused import
  removed, headless-permission note added, and (post-review) stdin=DEVNULL
  hardening surfaced by the live run.
- LIVE round-trip through the actual tool: claude_code task "Reply with
  exactly: LINKED" against the user's real Claude Code 2.1.220 CLI returned
  EXIT CODE: 0 / STDOUT: LINKED. Roster API shows claude_code among
  dev_coding's 5 tools.
Re-roll the app: bash scripts/build_dist.sh 1.3.0.

### AGENT MAP — separate orchestration window with task search + live per-agent inspection — BUILT (session 2 cont., 2026-08-01)
User request: "i want it to be able to send dispatch agents and display a map of all
  the agents in a different window, with a search bar to find the agent for that
  task, it should then when clicked upon, show me what that agent is doing".
Built (all REAL, evidence shown live):
- dourmouse/webui.py — ActivityTracker: a pure observer fed from the same
  event_sink the chat SSE uses (NEVER affects dispatch; a raising tracker is
  swallowed). Maps tool->subagent, tracks per-agent status (idle/computing/
  auth), last tool activity, and a bounded 30-entry feed so the map can show
  "what that agent is doing right now". GET /map serves ui/map.html; GET
  /api/activity returns tracker.snapshot(); GET /api/find_agent ranks agents
  for a task via find_agents_for_query — deterministic token-overlap scoring
  (Rule 2.8: pure string matching, no LLM in the lookup path), name hits
  weighted 3x, stop-word filtered, limit-bounded. POST /api/chat gained
  focus_agent: validated against the registry (400 on unknown name, before
  any SSE headers), wraps the prompt with a [ROUTING DIRECTIVE] so the task
  routes at ONE agent.
- ui/map.html — standalone AGENT ORCHESTRATION MAP window (opens from the
  dashboard's ⧉ AGENT MAP button via window.open): search bar with a ranked
  results dropdown (click to select), a grid map of all subagent nodes with
  live status pills [IDLE]/[COMPUTING] (pulsing)/[HOLD_AUTH] (blinking) +
  LAST-activity lines, and a click-to-inspect detail panel showing the
  agent's toolkit (permission color-coded chips), a per-agent DISPATCH
  DIRECTIVE box posting {prompt, focus_agent} to /api/chat, and a LIVE
  ACTIVITY FEED of that agent's real tool calls/results. Polls /api/activity
  every 1s; re-renders the detail panel ONLY when the selected agent's state
  actually changed (JSON dirty-check) so the dispatch textarea is never wiped
  by the poll (reviewer-caught bug). OPEN DASHBOARD link back.
- ui/index.html — ⧉ AGENT MAP button in the telemetry header.
Verification evidence (REAL, shown live this session):
- pytest: 209 passed, 0 failed. Coverage: dispatch 91%, general_roster 88%,
  webui 91%, overall 91%. New tests/test_map.py: /map served (and is not the
  dashboard), find_agents_for_query ranking (web->research_info,
  email->comms, delete->admin_ops, terminal->system; empty/stopword-only
  queries return no matches; limit respected; deterministic), ActivityTracker
  transitions (tool_use->computing+feed, tool_result->last.result,
  confirmation->auth, done->all idle, unknown tool ignored, feed bounded),
  /api/activity + /api/find_agent over real HTTP, focus_agent over real HTTP
  (unknown name -> 400; known name -> prompt wrapped with [ROUTING DIRECTIVE]
  and SSE completes).
- Two review rounds (deepseek-flash); findings fixed: (1) the 1s activity poll
  re-rendered the detail panel wholesale every tick, wiping the dispatch
  textarea and stealing focus — fixed with a JSON dirty-check
  (lastRenderedSnap) so the panel refreshes only on real state change;
  (2) dead _run_seq counter + never-emitted "result" terminal branch removed
  from ActivityTracker.
- LIVE in-browser round-trip (dev server restarted with the new routes):
  AGENT MAP button opened the map window; searched "search the web" -> ranked
  dropdown showed research_info (score 5) then memory (score 1); clicked the
  node -> detail panel with toolkit chips (web_search/fetch_url/open_url),
  dispatch box, live feed; dispatched "Search the web for NVIDIA news" to
  research_info via focus_agent -> node flipped [COMPUTING] (pulsing), LAST
  updated, and the LIVE ACTIVITY FEED streamed the real multi-turn activity:
  web_search {"query":"NVIDIA news today",...} -> real Wikipedia results, then
  two more searches, each TOOL/RESULT pair appearing live.
Re-roll the app: bash scripts/build_dist.sh 1.2.0 (zip ships map.html + the
  new routes; dashboard button opens the map window).

### Phase 0 — Guardrail / Risk Engine
Status: complete (pending user confirmation gate)
Verification evidence: phase0_pytest.log — 48 passed, 100% coverage on
  dourmouse/guardrails.py and dourmouse/config.py.
  Re-run: ./.venv/bin/python -m pytest dourmouse/tests -v --cov=dourmouse.guardrails --cov=dourmouse.config --cov-report=term-missing
Confirmed risk numbers (defaults): max_position 10%, max_sector 30%,
  daily_loss kill-switch 3% (start-of-day equity snapshot, latching + manual
  re-arm), trade confirmation threshold $1,000.
Deliverables:
- dourmouse/guardrails.py — pure deterministic engine (no I/O, no LLM):
  evaluate_trade(), KillSwitch (latching), daily_loss_*, evaluate_paper_gate().
- dourmouse/config.py — env-driven GuardrailConfig loader (defaults baked).
- dourmouse/tests/ — 48 tests incl. exactly-at-threshold, negative P&L,
  concentration exactly at max, kill-switch latch/re-arm, invalid-input raises.
- .env.example (all vars, later-phase ones marked not-yet-consumed), .gitignore.
Design notes:
- Position & sector limits only block EXPOSURE INCREASES (de-risking always allowed).
- "At or below limit passes; strictly above fails."
- Kill-switch trips at loss >= limit, latches through intraday recovery, clears
  only via manual rearm().
- Confirmation threshold FLAGS (requires_confirmation), never rejects.
- No broker/ATLAS connection exists — tested in complete isolation.
Notes:
- Session 1. Working dir was empty; no prior work. Built standalone (no ATLAS
  path given yet); relocatable next to ATLAS later per Integration Rule 7.
- Environment verified: Python 3.12.13 (Homebrew), Obsidian MCP server +
  mcp-venv present, Alpaca creds not set (not needed until Phase 3+).
- Runtime "DOURMOUSE Dispatch Agent" companion prompt received & registered as an
  OPERATING spec for later; not activated (nothing built to operate yet).

### Phase 2 — Obsidian MCP Fix + Memory Agent
Status: not started
Notes: Server exists at ~/.claude/obsidian_mcp_server.py. Response-timeout bug to debug.

### Phase 3 — Monitoring Agent + Conversational Front End
Status: not started
Notes: Alpaca read-only polling + Slack. Needs Alpaca paper creds.

### Phase 4 — Execution Agent + Paper Trading
Status: not started
Notes: First order-placement code. Gated by Phase 0 guardrails. Paper only.

### Phase 5 — Live Rollout
Status: not started
Notes: Requires `AUTHORIZE LIVE CAPITAL — <amount>`.

### Phase 6 — CSMOM / BAB Resolution
Status: not started
Notes: Independent. Rename hypothesis vs. extend CrossSectionalRanker short leg — user decision pending.
