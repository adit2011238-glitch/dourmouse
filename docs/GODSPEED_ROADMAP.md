# Godspeed Roadmap — Commercial-Grade Dourmouse

Source of truth for the initiative kicked off 2026-09-16 from `~/Documents/claude prompt .pdf`
(121 pages: agent-architecture translation, autonomous-runtime spec with 20 acceptance tests,
75-item engineering audit, 54-item defensive-cybersecurity spec) plus an inline UI/UX redesign
brief (45 sections). Full context also mirrored in `~/Documents/dourmouse_universe.md`.

Standing rule from the brief, reaffirmed here: fix things for real, root cause not patches, no
fake/stub UI or security theater, no work skipped, full pytest suite passes before every commit,
never break what already works. Work phase by phase, checked off below as it lands. This file is
the resumable checklist across sessions — update it at every phase boundary.

## Phase 0 — Recon and baseline (in progress)

- [x] Read the full 121-page spec.
- [x] Repo recon: 337 files / ~66,800 lines in `dourmouse/`, 190 test files, branch `recon-2026-09-11`.
- [x] Confirmed no persistent Goal/Task autonomous runtime exists yet (grepped, nothing found).
- [x] Confirmed no defensive-cybersecurity subsystem exists yet (`world_pulse.py` is an unrelated
      geopolitical-news feature).
- [x] Backend architecture map done → `docs/ARCHITECTURE.md`. Confirmed central gap: no persistent
      worker survives process restart; everything backgrounded today is a daemon thread inside one
      process. Real primitives to reuse: `DispatchRegistry`/`ToolSpec`/`Permission`, `_execute_tool`,
      `governance.py` (Budget/DLP/RBAC), `delegate_task`/`delegate_parallel`+`DispatchContext`,
      `net_errors.py`, `DesktopNotifier`/`ProactiveSurfacer`, `state_store.StateStore`'s SQLite/WAL
      pattern, `SchedulerRunner`'s daemon-thread-with-tick shape.
- [x] Frontend architecture audit done → `docs/UI_SOURCE_MAP.md`. Key findings: `/workspace` (not
      console.html) is the actual boot screen in both shells; 14 screens not 9, 8 themes not 4 (two
      stale in-code comments found); 5 overlapping "home" surfaces still routed; shared design
      system (`dourmouse-ui.css`) exists but is used by almost nothing; no syntax highlighting or
      diff rendering anywhere despite the tools existing; no command palette on the real primary
      surface; unbounded `liveEvents` array; real dead files identified for removal.
- [x] Full pytest baseline: **4702 passed, 10 skipped, 362s** — clean, matches known baseline.

## Phase 1 — Engineering audit (75-item checklist from the spec)

Not a full mechanical reorg of a 67k-line live package with a running pinned app — that risk
outweighs the tidiness gain. Instead: real inspection pass, fix everything CRITICAL/HIGH found
(security, concurrency, unhandled failures, dead code, hard-coded secrets/paths), document what's
LOW/cosmetic and deliberately deferred. Tracked findings go in `docs/ENGINEERING_AUDIT.md` with
severity, root cause, fix, files changed, tests added — never "fixed" without a real regression
test proving it.

- [x] Confirmed real gated-tool count is **34** (across 6 files), not the ~23 previously tracked — registry has grown. `_run_shell`'s `shell=True` (system_access.py:406) is the intentional, already-gated Bash-equivalent tool, not a bug.
- [x] Dead-file removal: re-verifying first caught a real false positive — `hub.html`/`graveyard.html`/`product.html`/`agent_chat.html`/`decision_cards.json` belong to a separate, real, tested ATLAS-hub sub-app (`tools/serve_hub.py`), not dead. Removed only the 2 confirmed-dead files (`ui/DOURMOUSE_DESKTOP_MOCKUPS.html`, root `quill-onboarding.html`) plus the unused Lucide bundle.
- [x] Silent `except Exception: pass` audit: 15 sites across 7 files. 14 justified (matching comment added), 1 real bug fixed (agent-inbox endpoint faked an empty inbox on bus failure, now surfaces `inbox_error`). Committed `cc72ba7`.
- [x] Prompt-injection boundary for tool output / external content: `rnd` (web_search/fetch_url) and
      `browser` agents had none, unlike `mail`/`docs`. Fixed (commit `bc91e6b`).
- [x] Real static analysis set up (`ruff`, curated config in `pyproject.toml`) and run for the first
      time — 598 real findings after curation (vs. 1,354 under ruff's noisy defaults). Fixed: 3 live
      undefined-name bugs (one a real `NameError` on a genuine production code path, caught only
      because a test called the real function instead of monkeypatching it away), an SSRF guard for
      `fetch_url` (host validation was missing; scheme validation already existed), an XXE/entity-
      expansion fix for the two modules parsing real external XML feeds, 3 dead-code removals. Every
      security-shaped finding (SQL construction, hardcoded-secret-shaped names, `shell=True`,
      bind-all-interfaces) reviewed individually — 4 real false-positive classes documented inline,
      1 real-but-low-urgency finding deferred with a full written reason. Plus a safe, mechanical
      autofix pass (unused imports, import sorting) across 92 files, spot-verified against 9 real
      test cases on the one transformation that touched actual boolean logic. Full detail:
      `docs/ENGINEERING_AUDIT.md` findings 010-015. Committed `27476ff`.
- [ ] Security pass beyond the above: path traversal, credential handling in the ~34 gated tools.
- [x] Concurrency pass, first real finding: `ActivityTracker`/`AttentionQueue`/`dispatch.JobTracker`
      spot-checked and all three already have real locks; `webui.py`'s per-tab session/gate/lock
      creation correctly holds `tab_state_lock` around its whole check-then-create sequence, no
      TOCTOU gap. Real bug found and fixed in the NEW code from this same initiative:
      `GoalRuntime._run_task` could silently overwrite an already-`cancel_goal`'d task back to
      `COMPLETED` if that task's dispatch call was still in flight when the cancel happened — fixed
      with a re-check immediately after the call returns, before writing any terminal status. See
      `docs/ENGINEERING_AUDIT.md` finding #016. Second, higher-severity real bug found in the same
      pass: `global_memory.py`'s singleton store used a `check_same_thread=True` connection (the
      stdlib default) while being reached from every top-level chat turn on `webui.py`'s
      `ThreadingHTTPServer` (one thread per request) — silently no-opped on any thread but whichever
      one built it first, since both dispatch.py call sites swallow the resulting
      `sqlite3.ProgrammingError`. Confirmed live against the pre-fix code (16/16 threads failed).
      Fixed with `check_same_thread=False` plus a real lock (matching `google_auth.py`/
      `memory_store.py`'s existing pattern) and double-checked-locking on the singleton itself
      (matching `goals.get_goal_store()`). See finding #017. Every own-write-path SQLite store now
      verified clean by direct reading: `cache.py`, `google_auth.py`, `memory_store.py` (already
      WAL/busy_timeout-hardened from a prior real incident), `supabase_sync.py`. The five read-only
      external-database readers (Claude Code/Codex history, project files) are a different risk
      category, not yet evaluated.
- [x] Git-history secret mining: installed `gitleaks` (industry-standard, real tool, not previously
      present), ran it against the full history of every branch (295 commits, ~166MB scanned). 168
      raw matches, every one individually triaged: 158 are archived third-party academic web pages
      under `jarvis/research_mesh/` (CMS cache-bust tokens plus one already-public Google Maps-style
      key belonging to `umd.edu`, not Dourmouse), 10 are deliberately fake placeholder secrets in
      `dourmouse/tests/` (env-loading and governance/redaction test fixtures). Zero real credentials
      anywhere in history. See `docs/ENGINEERING_AUDIT.md` finding #018.
- [x] First-ever `mypy` pass: 341 raw errors on a previously-unannotated codebase. Every rare,
      higher-signal category individually read (not sampled): found and fixed one real, previously
      invisible bug (Gemini delegation's `on_usage` cost tracking was a silent, total no-op since
      `call_gemini` never actually implemented the parameter `model_delegation.py` called it with —
      zero test coverage on either side of the gap, now closed with real tests) plus two trivial
      cosmetic cleanups. The bulk (attr-defined/union-attr/arg-type/etc., ~334 occurrences) is
      documented, deliberately deferred backlog, not silently assumed clean. See
      `docs/ENGINEERING_AUDIT.md` finding #019.
- [ ] Remaining `ruff` backlog (documented, not fixed): a full `S110`/`SIM105` try-except-pass sweep
      beyond the 15 already reviewed (~193 sites), a full dependency audit.
- [ ] Remaining `mypy` backlog (documented, not fixed): ~334 lower-signal occurrences across
      attr-defined/union-attr/arg-type/misc/assignment/return-value/operator/index/var-annotated,
      expected to be dominated by inference noise on this newly-annotated-nowhere codebase but not
      individually confirmed at this volume.
- [ ] Dead code / duplicate utility sweep beyond the UI dead-file pass and the F841 findings already fixed.
- [x] `docs/TESTING.md` written: real pytest/ruff/mypy/gitleaks invocation commands, the honest
      10-skips breakdown (all real environmental preconditions, never a silenced flake), and a full
      explanation of `conftest.py`'s autouse hermetic-isolation fixtures and the recurring
      real-.env-leaking-into-tests bug class they exist to prevent (with an explicit instruction for
      the next person adding a `DOURMOUSE_*` env var).
- [ ] `docs/ARCHITECTURE.md` (done), `docs/SOURCE_MAP.md`, `docs/DEVELOPMENT.md`,
      `docs/TEST_MATRIX.md` — still not written.

## Phase 2 — Autonomous agent runtime (the core new system)

Direct translation of the spec's Claude-Code-derived architecture onto Dourmouse's existing
dispatch/agent system (additive, not a rewrite — reuse `dispatch.py`'s model-call plumbing,
`planner.py`/`agent_router_model.py` routing, the existing tool registry and permission gate).

- [x] `Goal` and `Task` persistent objects + durable store (`dourmouse/goals.py`, SQLite/WAL,
      mirrors `state_store.StateStore`'s shape exactly). 30 tests, including real restart-survival
      tests (close the store, reopen the same file, state and the dependency graph are intact).
- [x] Real background worker (`dourmouse/goal_runtime.py`) — a daemon thread ticking over every
      active goal, pulling ready tasks (dependency-graph aware, promotes PENDING→READY as
      dependencies clear), executing each via a real `chat.ChatSession`/`dispatch.run_dispatch_messages`
      call (the exact same path a normal chat turn already uses — no second execution path),
      persisting a checkpoint after every single status transition (free, since each write is its
      own SQLite transaction). 17 tests. This closes the confirmed central gap from
      docs/ARCHITECTURE.md.
- [x] Crash recovery: a task found RUNNING at worker startup is never assumed still running — it's
      routed through the normal retry/fail path with a logged `recovery_attempted` event, never
      silently re-run.
- [x] Failure classification + bounded retry (`max_attempts` per task) → goal `BLOCKED` with an
      honest reason once a task permanently fails. A real bug caught by the tests themselves:
      completion/permanent-failure was only checked at the TOP of each tick, so a goal's last task
      finishing mid-tick didn't flip the goal to COMPLETED/BLOCKED until the NEXT tick — fixed to
      re-resolve immediately after running a batch.
- [x] Approval-gate integration, **honestly scoped**: a gated tool inside an autonomous task uses
      the existing `DOURMOUSE_AUTO_APPROVE` toggle (on → proceeds like an interactive yes; off →
      declines, task goes `WAITING_FOR_APPROVAL` with a real reason, real notification fires). A
      resumable **per-task approval ticket** (continue just that one task after a later human
      approval, without flipping the global toggle) is real, separate follow-on work — not built
      yet, tracked here honestly rather than faked.
- [x] Notifications reuse the existing, real mechanism exactly (no new channel): `bus.post(...)`
      for the COMMS panel, `state_store.add_alert(...)` + an `events_broadcast` SSE fan-out for a
      real native macOS notification via the already-shipped `DesktopNotifier`.
- [x] New `goals` subagent (`dourmouse/goal_tools.py`, kept OUT of the already-oversized
      `general_roster.py`, mirroring `system_access.build_system_subagent()`'s own file-per-subagent
      pattern): `create_goal` (the calling model decomposes the objective into a task graph itself,
      exactly like `delegate_parallel`'s branches — no separate hidden planner LLM), `add_tasks`
      (real replanning), `get_goal_status`, `list_goals`, `cancel_goal`. 24 tests.
- [x] `GET /api/goals` (list, `?status=`) and `GET /api/goals?id=` (full snapshot: tasks + real
      event history) — read-only for now; write endpoints (pause/cancel/approve buttons) are real
      Phase 3 UI work, not built speculatively ahead of that UI. 9 tests.
- [x] Wired into `webui.run_server` behind its own dedicated opt-in gate,
      **`DOURMOUSE_GOAL_RUNTIME=1`** — deliberately separate from `live_polling`/`live_enabled()`:
      a worker that can act with a user's own reach is a materially bigger default-behavior change
      than a news/markets poll. **Off by default. The user needs to opt in once satisfied with
      testing.**
- [ ] Scheduler for time/event-based routine creation (a routine auto-creating a `Goal` on a
      schedule or a filesystem/email event) — not built yet, real follow-on.
- [ ] Multi-agent delegation building on the existing `delegate_task` primitive — a task's own
      turn can already call `delegate_task`/`delegate_parallel` normally (nothing blocks it), but
      the runtime doesn't yet have its own dedicated multi-agent orchestration beyond that.
- [ ] Real independent verification (currently: "the task's turn completed without raising and
      wasn't declined" — self-reported, not independently checked against `success_criteria`).
- [ ] **Cross-device control** (explicit user requirement, 2026-09-16): Dourmouse should be able to
      act on every device on the Tailscale network — every app, every browser — with the same
      reach a user has at the keyboard, gated by the same approval layer as any other high-risk
      action (not a separate permission model). Builds on the existing node/remote-job
      architecture referenced in the spec (Mac/Windows/Dell nodes) rather than a new one.
      2026-09-16: the user's real desktop machine is now up. Real Tailscale network confirmed live
      (`tailscale status`): `adits-macbook-air` (this Mac), `desktop-4u4t12k` (Windows, real
      traffic counters, idle), `dourmouseserver` (Windows, a SECOND node literally named this —
      not yet investigated, treat with the same caution), `iphone173`. Tried a safe, read-only
      `tailscale ssh desktop-4u4t12k "echo ..."` — reached the host key verification stage (so the
      machine and an SSH service are genuinely reachable) but did not complete; not investigated
      further since guessing at credentials/flags for a real remote login isn't something to do
      unattended. **Real blocker, needs the user**: how they want authentication handled for this
      node (an SSH key already provisioned? Tailscale SSH enabled on that node specifically?).
      2026-09-16, same session: user powered the desktop off to save energy — this sub-thread is
      parked (not abandoned) until it's back up and reachable again.
      **Caution (standing, from memory):** a separate forex-engine/ATLAS dourmouse deployment
      already runs on that same desktop and can be console-killed by mistake — it's live user
      work, never touch or restart anything there beyond what's explicitly being built here. Same
      caution extended to `dourmouseserver` until its actual role is understood.
- [ ] Live progress model + notifications through the existing notification mechanism.
- [ ] Run the spec's 20 acceptance tests for real against the implementation.

## Phase 3 — UI/UX redesign (Claude Desktop / Claude Code interaction quality)

- [x] Real syntax highlighting for fenced code blocks in `console.html`
      (the #1 gap identified in `docs/UI_SOURCE_MAP.md` §5 — the language
      identifier was parsed and discarded; code rendered as unstyled `<pre>`).
      Hand-rolled tokenizer (matching `md()`'s own hand-rolled-regex
      approach, no library) for python/javascript/bash/json, wired into
      `md()`'s existing code-fence branch, styled with the real existing
      `--amber`/`--ok`/`--blue-dim` tokens (no new colors introduced), plus
      a language label. Verified three ways: an isolated Node harness (11
      assertions: keywords/strings/comments per language, the "a `#`
      inside a string is not a comment" edge case, multi-line block
      comments, and an explicit XSS-safety check), a live extraction-and-
      execution of the actual served file's `md()` function against a
      tagged fence (confirmed correct `data-lang` + `tok-*` spans), and a
      real rendered screenshot in the Browser pane with computed CSS
      colors confirmed. 11 new pytest regression tests
      (`test_console_code_highlighting.py`), matching this codebase's own
      established source-level (no headless browser) convention for
      `console.html` tests.

Waits on the frontend audit so the design system replaces real, identified debt rather than
guessing. Must also surface Phase 2's goals/tasks (chat vs. work distinction from the spec) once
that exists, so this phase runs after Phase 2 has at least its data model in place.

- [ ] `docs/UI_DESIGN_REFERENCES.md`, `docs/DESIGN_SYSTEM.md`, `docs/UI_SOURCE_MAP.md`.
- [ ] Design tokens (spacing/color/type/icon) replacing scattered magic numbers.
- [ ] Tool-activity / code-diff / terminal-output components.
- [ ] Command palette, right-side context panel, global status bar, live activity feed.
- [ ] Real data only — every widget has loading/empty/stale/error states, nothing fabricated.

## Phase 4 — Defensive cybersecurity subsystem

Authorized-defensive scope only (host + the user's own network + Tailscale). No credential theft,
no attacks on third parties, no surveillance tooling — matches the spec's own explicit boundary.

- [x] `SecurityPlatformAdapter` foundation (`dourmouse/security/platform_adapter.py`, macOS first,
      matches current deployment): real interfaces, default gateway, DNS resolvers, ARP neighbors,
      listening ports with exposure classification (LOOPBACK_ONLY / LOCAL_NETWORK / TAILSCALE /
      ALL_INTERFACES / UNKNOWN), Application Firewall state. Every operation returns
      `{"available": False, "reason": ...}` honestly rather than fabricating a value; every
      subprocess call is argument-list-only with a real timeout (no `shell=True`). 35 tests using
      REAL command output captured live from this machine 2026-09-17 (not synthetic samples) —
      including the real "en0 (LAN) vs. utun4 (Tailscale) must not be conflated" case a naive grep
      actually hit earlier this same session, and the real current finding that this machine's own
      Application Firewall is disabled and a Python process listens on `*:8793` (ALL_INTERFACES).
- [x] New `security` subagent (`dourmouse/security/tools.py`): `security_status` (a real evidence-
      backed summary), `list_exposed_services` (filterable by exposure). Read-only — no
      remediation tool exists yet, deliberately (the spec's own default policy is "ask before
      changing anything"; a real remediation tool is separate, later work needing
      REQUIRES_CONFIRMATION when built). Classified LOCAL_ONLY in `model_delegation.py` (same
      reasoning as `system`/`admin_ops`: this host's own network details are private data).
      9 tool-layer tests.
- [x] `GET /api/security` — read-only real-time snapshot, same shape as `platform_adapter`'s own
      `get_system_security_state()`. 1 endpoint test (against the real adapter, not mocked —
      the parser-level tests already prove honesty on failure).
- [ ] Windows/Linux platform adapters (macOS only so far — this machine's real platform).
- [ ] Network diagnostics engine (latency/jitter/packet-loss/DNS-reachability probes), WiFi
      security details (the classic `airport` binary is confirmed REMOVED on modern macOS during
      this pass's own investigation — `system_profiler SPAirPortDataType` is the real replacement,
      not yet wired), Tailscale-specific status beyond what generic interface/DNS data already
      shows.
- [ ] Baseline engine + security event schema + local AI sentries (evidence-backed, never fabricated).
- [ ] Security dashboard UI (the concept mockup exists; no real implementation yet — waits on the
      Phase 3 design-system work this same telemetry layer now makes buildable).
- [ ] Threat model doc + security test suite (including prompt-injection-via-network-data tests) +
      packet capture + any remediation actions (all real, separate, later work).

## Completion bar (standing, from the user, 2026-09-16)

Self-evaluate against the full 121-page spec at every milestone, not just once at
the end — and hold the result to the standard of a real, publicly-known engineering
report a professional would actually publish (a real security audit, a real
architecture review, the kind a CEO/CTO would read and trust), not an
AI-assistant-shaped summary. Section 75 of the spec already asks for exactly this
("judge the repository as if handed to a senior engineering team + security
reviewer + QA team + DevOps engineer + a new developer") — this raises that bar
explicitly rather than replacing it. As of this note: **12-15% of the full spec**,
honestly assessed (recon/docs done; runtime core real and tested; audit, UI
implementation, and the cybersecurity subsystem are the large remaining pieces).

## UI direction (standing, from the user, 2026-09-16)

Concrete visual direction on top of `docs/DESIGN_SYSTEM.md`/`UI_DESIGN_REFERENCES.md`:

- **Never show the user an em dash or other odd special characters in ANY
  product-facing text** — UI copy, labels, and (as far as we control it) model
  output rendered in the UI. This is broader than this session's own writing-style
  rule (which governs code/commits/chat) — it is a real UI-copy requirement for
  what Dourmouse itself outputs. Already fixed two real violations found in the
  concept mockups (Security/Research canvases) the same day this was said.
- No emoji, no generic/standard gradients, as already established.
- Fonts stay distinctive, not a generic system stack — already satisfied by the
  real existing `--dm-font-sans`/`--dm-font-mono` choices (Departure Mono,
  Monaspace Neon, etc.) — nothing to change here, just don't regress it.
- Reference feel: Claude Desktop / Grok / Codex — clean, sleek, professional.
  Concretely: Hermes Desktop's inline "Thinking" / tool-call rows and minimal
  composer bar are a good fidelity target for `ToolActivity` (see
  `docs/UI_DESIGN_REFERENCES.md`'s Hermes section).
- **Custom icon per major section**, Claude-Desktop-sidebar style (simple line
  icons: New agent/Skills/Messaging/Artifacts in their reference) — Phase 3's
  icon-system gap (`docs/DESIGN_SYSTEM.md` gap 3) should land as one distinct
  icon per nav destination (HOME/RESEARCH/CODE/SECURITY/etc.), not a generic
  shared icon reused everywhere.
- For the Security/Network center specifically: the user likes an orbital/sphere
  visualization concept for the network topology (referencing a glowing
  hex-sphere image) but wants it **practical, not literal** — keep the orbital/
  radial metaphor, drop the glow/particle/hex-grid sci-fi treatment, which
  directly conflicts with the design system's own already-established "no
  decorative glow" principle. A real, calm, evidence-backed radial layout
  (nodes arranged on a ring or sphere-projection around the host), not a
  decoration.

## Notes / decisions log

- 2026-09-16: Chose audit → runtime → UI → security ordering. Runtime is the highest product
  value and most novel piece; UI redesign should visualize it once it exists rather than being
  redone twice; security is the largest standalone vertical, done last.
- 2026-09-16: Declined the spec's literal `src/core/agents/tools/...` directory reorg as a
  big-bang move — too risky against a live, daily-used app. Reorganizing incrementally per
  subsystem as each is touched instead.
