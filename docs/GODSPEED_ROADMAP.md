# Godspeed Roadmap — Commercial-Grade Dourmouse

Source of truth for the initiative kicked off 2026-09-16 from `~/Documents/claude prompt .pdf`
(121 pages: agent-architecture translation, autonomous-runtime spec with 20 acceptance tests,
75-item engineering audit, 54-item defensive-cybersecurity spec) plus an inline UI/UX redesign
brief (45 sections). Full context also mirrored in `~/Documents/dourmouse_universe.md`.

Standing rule from the brief, reaffirmed here: fix things for real, root cause not patches, no
fake/stub UI or security theater, no work skipped, full pytest suite passes before every commit,
never break what already works. Work phase by phase, checked off below as it lands. This file is
the resumable checklist across sessions — update it at every phase boundary.

**`docs/COMMERCIAL_GRADE_MASTER_REQUIREMENTS.md`** (added 2026-09-18) is the standing,
harsh-adversarial yardstick every domain is measured against — built from the 121-page spec, a
real Hermes/Claude-Code/Codex architectural comparison, two real reference repos (ThreatSentinel,
AI-Scientist), a distilled backend-architecture guide, a distilled UI-design-principles guide, and
every direct user instruction layered on since. This roadmap tracks phase-by-phase progress; that
document defines what "done" means for each domain and holds the permanent per-domain acceptance
tests. Read it before any future "how far along are we" self-assessment.

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
- [x] Wired into `webui.run_server`. **Default ON since 2026-09-18** (`DOURMOUSE_GOAL_RUNTIME=0`
      to opt out), flipped from the original opt-in default — a real, live bug, not a cautious
      choice worth keeping: `create_goal`/`add_tasks` are registered by `general_roster.py`
      unconditionally, with no check of this flag anywhere in that path, so with the worker off
      by default the model could call `create_goal`, get back a real goal id, tell the user work
      was now happening in the background, and nothing would ever advance it — exactly the
      founding spec's own named anti-pattern ("do not create fake background execution... no real
      worker is operating"), just worse than a spinner since the tool call looked fully
      successful. No safety gate was removed by this flip: a `REQUIRES_CONFIRMATION` tool inside
      an autonomous task still pauses at `WAITING_FOR_APPROVAL` regardless of this flag. New
      direct test coverage for the wiring itself (previously untested): `TestGoalRuntimeWiring`
      in `test_webui.py` (2 tests) confirms a real worker thread starts/doesn't start; a new
      autouse `_goal_runtime_off` fixture in `conftest.py` keeps every other test hermetic (the
      same override-the-fixture convention as every other isolation fixture there — see
      `docs/TESTING.md`).
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
- [x] Cross-goal audit trail (acceptance test 15's data/API half): `GoalStore.all_events`/
      `export_events_markdown` + `GET /api/audit` (`?goal_id=`, `?since=`, `?format=markdown`).
      Investigation found the underlying event log already real and comprehensive
      (`goal_events`, auto-logged by every goal/task lifecycle method plus real tool_call/
      tool_result/recovery_attempted logging in `goal_runtime.py`) — the actual gap was narrowly
      "no cross-goal query, no human-readable export, no UI surface." First two closed; the UI
      surface (the GOALS screen) followed later this same phase -- see finding #026 below. Real
      bug caught by the new tests before shipping:
      `since` was first treated as a Unix float, but `goal_events.at` is a real ISO-8601 string
      throughout this module — fixed end to end rather than converting types at a boundary. See
      `docs/ENGINEERING_AUDIT.md` finding #022.
- [ ] Live progress model + notifications through the existing notification mechanism.
- [ ] Run the spec's 20 acceptance tests for real against the implementation. Test 8 (crash
      mid-task, resumes without duplicating side effects) genuinely drilled 2026-09-18: real
      `kill -9` against the real dev-preview server mid-dispatch, restarted, real automatic
      recovery confirmed from the live event log (`recovery_attempted` → `RETRYING` → successful
      retry with a correct real answer) — see `docs/ENGINEERING_AUDIT.md` finding #024.
- [x] Test 11 (independent verification, not self-reported): a genuine second reasoning pass over
      a tool-less `ChatSession`, shown the real tool-call evidence from the actual run, a real
      `NOT_VERIFIED` verdict routed through the normal failure/retry path exactly like any other
      failure. `TASK_STATES`' own `VERIFYING` state, defined since this module's first version and
      never once used, now real — live-verified end to end against the real dev-preview server
      (`RUNNING` → `VERIFYING` → `COMPLETED`, a real independently-reasoned verdict logged to the
      real audit trail). Real, deliberate cost tradeoff: every task now makes two real dispatch
      calls, not one. See `docs/ENGINEERING_AUDIT.md` finding #025.
- [x] Deterministic success-criteria check (finding #031): `success_criteria`, declarable on any
      goal since this module's first version but never once read back, now gets a real, goal-
      scoped independent check (`_verify_goal_criteria`) before a goal is allowed to complete --
      criterion-by-criterion, not one vague verdict. `GOAL_STATES`' own `VERIFYING` value, defined
      since the module's first version and never once used for a goal before, now real. An
      unsatisfied criterion routes the goal to `BLOCKED` with the real reasoning, never silently
      reported done -- the real, already-completed task work underneath is never thrown away.
      Live-verified with a real, unscripted model call: a task fully succeeded on its own terms
      while the goal it belonged to still correctly blocked because its own declared bar (a
      specific token that never appeared) was not met, visible in both the real audit trail and
      the real GOALS screen. Domain B is now fully closed -- all 20 acceptance tests real.
      See `docs/COMMERCIAL_GRADE_MASTER_REQUIREMENTS.md` Domain B for the full per-test status.
- [x] Test 15 UI surface (the GOALS screen, `ui/console.html`): before this, no UI file in the
      whole product referenced `/api/goals` or `/api/audit` -- confirmed by grep, not assumed -- so
      the autonomous runtime that findings #023-#025 had just hardened was invisible; a goal could
      run forever in the background with no way to see it short of calling the API by hand. Now: a
      live goal list (status, priority, blocked reason), tasks expandable per goal on demand, the
      real cross-goal audit trail, and the runtime's first real write action (CANCEL, a thin route
      over the already-tested `cancel_goal`). Polled every 4s while the screen is open, not pushed
      over the shared SSE stream ORCHESTRATION uses -- the runtime has no event-sink wiring into
      that stream today, real separate follow-on if ever needed. Live-verified against the real
      dev-preview server with real clicks: list, expand, cancel, and the resulting audit entry all
      confirmed, not just passing tests. See `docs/ENGINEERING_AUDIT.md` finding #026.
- [x] Domain D (self-extension, "the single most architecturally sensitive item" in
      `docs/COMMERCIAL_GRADE_MASTER_REQUIREMENTS.md`): `agent_smith` (`general_roster.py`) drafts
      a real tool + real test for a described capability gap; a human -- never the model, no
      approve/reject tool exists anywhere in the roster, checked by a dedicated test -- reviews
      the actual source in the new AGENT SMITH screen and approves over
      `POST /api/self_extensions/approve`; approval runs the draft's own test through a real
      pytest subprocess and forces `Permission.REQUIRES_CONFIRMATION` regardless of what the
      draft claims; an approved tool becomes callable only after a real server restart
      (`general_roster.py`'s new startup loader). Live-verified in full against the real
      dev-preview server: a real Ollama Cloud call drafted a genuinely correct
      `celsius_to_fahrenheit` tool, approval genuinely ran and passed a real pytest subprocess,
      a real restart made it live, and -- the hardest proof -- a fresh chat thread's real call to
      the new tool genuinely paused on a real confirmation event and only executed after a real
      human approval, returning the mathematically correct result. A real bug caught live (not in
      a unit test): the changelog's first design wrote into the actual tracked repo on every
      approval; fixed to be workspace-relative like every other piece of this feature's state.
      See `docs/ENGINEERING_AUDIT.md` finding #028.
- [x] Test 7 (resumable per-task approval ticket): the largest remaining named gap in Domain B,
      called out in three separate prior findings every time it came up. `GoalStore.
      resolve_task_approval` -- a human approves the ONE task they reviewed (the GOALS screen's
      own APPROVE/DECLINE on a waiting task), not every gated action on every task everywhere via
      the global `DOURMOUSE_AUTO_APPROVE` toggle. The ticket is one-time, consumed the instant the
      task starts running again, before its own dispatch call happens. Live-verified against the
      real dev-preview server: a real gated `send_draft` call was genuinely declined, a real click
      on the real APPROVE button resumed it, and the same tool call ran for real on the next
      attempt -- no more decline. An unplanned bonus proof: the independent verifier (finding
      #025) still correctly caught that the tool's own honest "not configured, nothing sent"
      result didn't mean the task's real objective was accomplished, even after human approval
      passed the gate -- two safeguards doing their own separate jobs correctly. See
      `docs/ENGINEERING_AUDIT.md` finding #029.
- [x] Domain F harsh acceptance test run live (real fan-out, 3 specialist branches via
      `delegate_parallel` against the real dev-preview server) surfaced a real reliability gap:
      a branch that only ran out of its own `max_turns` reported `(OK, 11.39s)`, indistinguishable
      from a branch that genuinely finished -- the same self-reported-success class finding #025
      closed for goal-runtime tasks, now found on `delegate_parallel`'s own separate execution
      path. Fixed by reading a real, pre-existing, previously-unused signal: `dispatch.py`'s own
      `budget_exhausted` transcript entry, emitted unconditionally the instant `max_turns` is
      exhausted. `_format_delegate_parallel_result` now reports `succeeded`/`incomplete`/`failed`
      as three separate counts, marks an exhausted branch `INCOMPLETE` instead of `OK`, and keeps
      the branch's own real partial text visible underneath an explicit warning rather than
      discarding it. `max_turns`'s own JSON-schema description (identical in `delegate_task` and
      `delegate_parallel`) also gained real guidance on how many turns multi-step work actually
      needs, targeting the root behavior observed live (the model picked `max_turns: 1` for a job
      that needed several). Live-caught, then covered by a real regression test that genuinely
      exhausts a branch's turn budget through the real dispatch loop (`FakeClient`'s own
      repeat-last-response mechanism, the same one proven in
      `test_dispatch.py::test_max_turns_bounds_looping_model`), not a mocked shortcut. See
      `docs/ENGINEERING_AUDIT.md` finding #032.
- [x] `research_mesh` rebuilt for real (user instruction: "forget the existing one and rebuild").
      "The existing one" turned out to be a real, concrete, already-committed package sitting
      orphaned in this repo (`jarvis/research_mesh/agents/`, 2395 lines, its own real test suite) --
      a field-specialist qualification mesh across 500 real academic fields (6836 real exam PDFs),
      zero references from `dourmouse/`, no real reasoning backend, stale a month. Relocated the
      already-correct state machine into `dourmouse/research_mesh/`, added a real `RealBrain`
      backed by this codebase's own real model routing (the same tool-less `ChatSession` primitive
      `goal_runtime.py`'s verification calls already use), and made it chat-reachable for the first
      time via a new `research_mesh` subagent (`dourmouse/research_mesh_tools.py`). Live-verified
      with a real model against the real corpus: a real held-out fail, a real remediation, a real
      pass, and a real 3-strikes exclusion, plus a real chat call whose response matched the tool
      handler's own private string template character-for-character. **Scope correction, same
      day**: this is a real, valuable, standalone capability, but user correction clarified it is
      NOT the founding spec's own "distributed research network" workstream -- that is a real
      3-device network (this Mac, the Dell compute node, the DOURMOUSE desktop), tracked as real,
      separate, not-yet-started work under `docs/COMMERCIAL_GRADE_MASTER_REQUIREMENTS.md` Domain G.
      Two real, separate, out-of-scope bugs found live and flagged rather than fixed here: this
      machine's global Claude Code CLI model override (fixed directly, user's own account setting,
      outside this repository), and a Grounded Mode false-positive on a genuine tool call
      (`task_d88c3f91`), plus a separately-flagged, confirmed-hanging pre-existing test
      (`task_ade8f6a2`). See `docs/ENGINEERING_AUDIT.md` finding #033.

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

- [x] Product-facing copy cleanup (user-directed, 2026-09-17): "the user should never see special
      characters, em dashes and other such things... js sleek professional like on claude desktop,
      grok chatbots and codex." Live-verified in the browser first: the running app's own header
      read "DOURMOUSE // HOME" and the status bar showed a bare em dash as a placeholder glyph,
      directly contradicting the instruction. Full sweep of `ui/console.html` (7,143 lines, the main
      daily UI), `ui/login.html`, and `ui/setup.html` (the two first-run screens): every `<title>`,
      panel header, and sub-section header's decorative `//` separator replaced (a `·` middle dot,
      or removed outright and left to layout spacing, matching the reference apps' own minimal
      style); every `—`-as-clause-joiner sentence rewritten with natural punctuation (period, comma,
      colon, semicolon, or a parenthetical, chosen per sentence, not a blind find-replace); every
      bare `"—"` used as a "no value yet" placeholder glyph replaced with a plain ASCII `-`.
      Methodology, since a first pass using markup-adjacency heuristics and a naive multi-line-
      template-literal scan both missed real instances (documented honestly rather than silently):
      built a proper character-level state-machine tokenizer (tracks `//` line comments, `/* */`
      and `<!-- -->` block comments, and `'`/`"`/`` ` `` string/template literals) to enumerate every
      em dash genuinely inside a string or template, then read every single flagged line's real
      surrounding context by hand rather than trusting the tokenizer's own state label (which had a
      real desync bug of its own, caught by cross-checking against a plain grep for
      `textContent`/`innerHTML`/`alert(`/`placeholder=` assignments containing an em dash, which
      independently returned zero remaining hits). Six existing tests asserted the literal old text
      (the placeholder glyph itself, or an exact substring of the old copy) and were updated to
      match the new, correct strings, not reverted. Live-verified in the browser afterward: home
      screen, `/login` (both the Google and access-token flows), and the `/setup` wizard's first two
      steps all screenshot-confirmed clean.
      **Remaining, honestly not done**: 18 other UI files under `ui/` were NOT swept this pass, with
      a real raw em-dash count each (not yet triaged into real-vs-comment, so these are upper
      bounds, not confirmed defect counts): `index.html` (257, the `/index.html` HUD surface),
      `workspace.html` (116, the `/workspace` Vision floating-panel UI), `os.html` (51), `atlas_lab.html`
      (28), `hud.html` (27), `product.html` (21), `map.html` (20), `study.html` (12), `voice.html`
      (11), `mobile.html` (9), `agent.html` (7), `all_hands.html` (7), `app.html` (6), `hub.html` (4),
      `graveyard.html` (3), `design-system.html` (2), `agent_chat.html` (1), `file_preview.html` (1).
      Prioritize `workspace.html` and `index.html` next (both real, reachable daily-use surfaces per
      `docs/UI_SOURCE_MAP.md`); the rest are specialized secondary windows (STUDY, ALL_HANDS,
      per-agent windows, the ATLAS lab, a product/marketing page) of lower daily-visibility.

- [x] `index.html` swept (2026-09-18), the top-priority file from the list above -- 257 raw em
      dashes, 95 genuinely real (product HTML text/attributes and JS strings), 162 pre-existing
      code comments correctly left untouched. A hand-built tokenizer specific to this sweep (the
      prior one had its own real bug: nested template-literal `${...}` interpolation desynced a
      naive backtick-depth-1 quote tracker, both undercounting AND overcounting depending on the
      file's own structure -- caught by cross-checking against `grep -c` ground truth, which the
      tokenizer's own output had to match exactly before being trusted). Every genuine placeholder
      glyph (`'—'` used as "no value yet") replaced with a plain ASCII `-`; every clause-joining
      sentence rewritten with natural punctuation (period, comma, colon, semicolon), chosen per
      sentence; every short label-pair (`"EFFECT 1/6 — COUNT-UP"`-shaped) standardized on the
      middle dot `·` this same file already uses as its own separator convention everywhere else,
      rather than inventing a new one. Verified two ways beyond the line diff: every affected
      `<script>` block re-extracted and syntax-checked clean with `node --check` (confirms no
      broken quote balance from any of the 95 edits), and a live dev-preview render confirmed the
      real, running app's own header tooltips, textarea placeholder, and a genuine live SSE event
      (`Freebuff watch offline · app unreachable`) all render the corrected text, not just the
      static source. Zero existing tests referenced the old text. 17 files remain, `workspace.html`
      next.

- [x] `workspace.html` swept (2026-09-18), the second-priority file (the `/workspace` Vision
      floating-panel UI). 116 raw em dashes; 43 flagged real by the tokenizer, of which 4 were
      themselves a real, narrow tokenizer gap (a `<script type="module">` block -- the tokenizer
      only recognized a bare `<script>` opening tag as entering JS mode, so `//` comments inside
      the module block were never masked; caught by reading each flagged line, not by trusting the
      tool blindly, exactly the discipline the original methodology called for) -- left untouched
      as the genuine code comments they are, 39 real fixes applied. Same fix vocabulary as
      `index.html`: ASCII `-` for placeholder glyphs, `·` for short label pairs, natural
      punctuation (comma/colon/semicolon/period) chosen per sentence for clause-joiners. Verified
      the same two ways: every script block (including the `type="module"` one, checked separately
      since `node --check` needs ESM input handled differently from a classic script) syntax-clean
      after all 39 edits, and a live dev-preview render of the actual Vision workspace confirmed
      the HAND CONTROL panel's placeholder fields and its full multi-sentence explanation paragraph
      both render the corrected text for real. A real, live-caught bug: the full suite (run before
      committing, as always) failed one pre-existing test that asserted the literal old em-dash
      string inside `stopHandControl()` -- an earlier grep-based stale-reference check missed it
      because it searched a handful of representative substrings, not every one of the 39 changed
      lines individually; fixed to assert the new, correct text, not reverted, and a second,
      exhaustive per-fix grep across the whole test suite afterward confirmed no others were
      missed. 16 files remain.

Waits on the frontend audit so the design system replaces real, identified debt rather than
guessing. Must also surface Phase 2's goals/tasks (chat vs. work distinction from the spec) once
that exists, so this phase runs after Phase 2 has at least its data model in place.

- [ ] `docs/UI_DESIGN_REFERENCES.md`, `docs/DESIGN_SYSTEM.md`, `docs/UI_SOURCE_MAP.md`.
- [ ] Design tokens (spacing/color/type/icon) replacing scattered magic numbers.
- [ ] Tool-activity / code-diff / terminal-output components.
- [ ] Command palette, right-side context panel, global status bar, live activity feed.
- [x] A real audit-trail/activity UI surface over `GET /api/audit` (backend and API done, finding
      #022) -- closes acceptance test 15 ("the user can inspect what the agent actually did")
      fully, not just at the API level: the GOALS screen's LIVE AUDIT TRAIL section (finding
      #026), chronological, human-described per event type, plus a real EXPORT MARKDOWN button
      (2026-09-18) wired straight to the backend's own `?format=markdown` response (finding #022)
      -- a real Blob download, live-verified against the real dev-preview server (a genuine
      17,945-character, 61-entry real report fetched and downloaded, not a stub).
- [x] A real, user-facing scheduling/timetable UI over the existing `scheduler_runner` primitive
      (new, explicit user ask -- see `docs/COMMERCIAL_GRADE_MASTER_REQUIREMENTS.md` Domain C): the
      TIMETABLE screen (finding #027), a visible list of every scheduled routine, next-run time,
      last-run time, and pause/resume/edit/delete (edit: finding #030, `Schedules.update_spec`,
      deliberately scoped to WHEN a routine runs, never WHAT it does). Live-verified: a real chat
      message created a real schedule through the real `schedule_recurring` tool and the real,
      currently configured Ollama Cloud backend, then paused/resumed/edited/deleted over real
      browser clicks, edit confirmed by a direct backend read (Monday -> Friday, `tool`/
      `arguments` untouched). Deliberately no creation form (natural language through any chat
      composer already does this for real). Domain C is now fully closed -- all 4 acceptance
      tests real.
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

## Phase 5 -- Domains E-J, scoped 2026-09-19, not yet started

Every remaining domain now has a concrete **Build plan** subsection in
`docs/COMMERCIAL_GRADE_MASTER_REQUIREMENTS.md` (added the same day as
finding #033, so a future session can start executing directly instead of
re-deriving architecture from scratch) -- real modules named, real existing
infrastructure identified for reuse (the `ChatSession(DispatchRegistry(),
session_file=None)` tool-less primitive now has FOUR real precedents to
copy: `_verify_completion`, `_verify_goal_criteria`, `RealBrain.answer`,
and the research pipeline's own plan/extract/synthesize calls; the
one-row-JSON-body resumable SQLite store shape now has THREE:
`goals.py`, `research_mesh/store.py`, the proposed security sentry store),
and a real build sequence, not just a requirement statement:

- [x] Domain E, step 1/6 shipped 2026-09-21 -- unblocked (re-checked
      `ps aux | grep -i "claude.*Documents/dourmouse"`: no match, clear to
      proceed). `dourmouse/device_wiki/core.py`, mirroring
      `research_pipeline/core.py`'s own pure-logic-first shape: a real
      `WikiEntry` (UNSUMMARIZED/SUMMARIZED/MISSING) plus `reconcile()`,
      the function harsh acceptance test 2 depends on -- a real deletion
      is marked MISSING, never dropped; a real content change reverts to
      UNSUMMARIZED rather than keeping a stale summary; a reappearing
      MISSING file self-heals from its real preserved summary. See
      `docs/ENGINEERING_AUDIT.md` finding #056.
- [x] Domain E, steps 2-5/6 shipped 2026-09-21 -- the real SQLite store
      (`device_wiki/store.py`, one row per real file, workspace-relative
      `DEFAULT_DB` from the start), the real summarizer
      (`device_wiki/stages.py`, reusing the tool-less `ChatSession`
      primitive, harsh acceptance test 1 enforced in code -- a binary or
      unreadable file never reaches the model), the real walker
      (`device_wiki/walker.py`, explicit `DOURMOUSE_WIKI_ROOTS`
      allowlist, never a filesystem-wide fallback, harsh acceptance test
      2 proven end to end through three real temp-directory scans), and
      the chat tool (`device_wiki_tools.py`, the `device_wiki` subagent
      -- `device_wiki_scan`/`device_wiki_status`/`device_wiki_get`).
      Live-verified against two real local files (a meeting-notes text
      file, a project README): two real, accurate summaries produced end
      to end. A real duplicate-summarize-call bug caught before any test
      ran, fixed. See `docs/ENGINEERING_AUDIT.md` findings #057-#060.
- [x] Domain E, step 6/6 (final) shipped 2026-09-21 -- `GET /api/device_
      wiki` (read-only, mirroring `/api/goals`'s own shape) and a new
      WIKI screen in `ui/console.html` (SCREENS array, polled every 5s,
      SUMMARIZED/UNSUMMARIZED/MISSING grouping). Live-verified end to
      end in the browser: a real scanned file's status, path, and real
      summary rendered correctly through the console's own "MORE"
      overflow menu, confirmed by screenshot. See
      `docs/ENGINEERING_AUDIT.md` finding #061. Domain E's full build
      plan (steps 1-6) is now closed; cross-link generation (harsh
      acceptance test 4) is real, separate follow-on layered on after,
      not yet built.
- [x] Domain I dashboard UI shipped 2026-09-21 (user-directed: "more
      visual with circles task bars ... a dashboard") -- a real SECURITY
      screen over `GET /api/security_dashboard`: a real SVG circular
      risk gauge, severity/incident bars, known-device count, real
      findings list. Reads the server's own already-computed
      `SentryRuntime.last_result`, never triggers a fresh scan. Caught
      and fixed a real bug before any test ran: `var(--no)`/`var(--dim)`
      do not exist in this stylesheet, the real established colors are
      `var(--bad)`/`var(--blue-deep)`. Live-verified against this
      machine's own real risk score (21.0) and 4 real findings,
      confirmed by screenshot. See `docs/ENGINEERING_AUDIT.md` finding
      #062.
- [x] Phase 7, the agent ecosystem visual monitor, shipped 2026-09-21
      (user-directed: "pixelated office ... confirm as you work"), v1
      was a static desk grid rebuilt via innerHTML every update -- real
      user correction ("I want ... move interact discuss ... exactly
      like the GitHub repo"), fixed same day in v2: a persistent SVG
      scene, patched in place (`.style.transform`/color/text on existing
      nodes, never rebuilt) so CSS transitions genuinely animate a
      sprite walking between its desk and a real meeting-room seat, both
      driven ONLY by real `_orchFanouts`/`_orchAgents` changes, never a
      timer. Caught and fixed a real regression along the way: v1's
      `const busy = ...` tripped the pre-existing
      `test_console_per_screen_busy_state.py` guard against exactly the
      shared-global-`busy` bug class this codebase was bitten by once
      already -- gone in v2's rewrite. Live-verified: all 43 real
      subagents rendered as persistent desks (including two agents
      showing a real, distinct `LIVE` status the renderer correctly
      passed through unmodified); three real `delegate_parallel` fan-
      outs driven live through the actual chat composer all genuinely
      dispatched and completed. Honest gap: every real fan-out in this
      local environment finished in under a second, so the walk
      animation itself was code-reviewed but never caught mid-flight in
      a screenshot. See `docs/ENGINEERING_AUDIT.md` finding #063. Deeper
      3D/pixel-office asset work (real sprites, a full floor plan,
      deploy/blocked-streak-triggered flavor animations) is real,
      separate, not-yet-built follow-on.
- [x] Agent ecosystem hardening, round 1, shipped 2026-09-20 -- two real
      flaws found during a design-phase review of the (not-yet-built)
      multi-floor agent office concept, fixed against the real, already-
      shipped backend (`message_bus.py`/`send_message`), not the mockup:
      (1) `send_message`'s `from_agent` used to come straight from the
      model's own tool-call arguments, checked only against "is this a
      real roster name" -- any agent (or the untargeted top-level
      orchestrator turn) could forge a message as `security` or
      `orchestrator`. Fixed by reading the real caller identity off
      `current_dispatch_context(registry).forced_agent` (the same real
      hard-scoping `delegate_task`/`delegate_parallel` already use) and
      refusing loudly, never silently overriding, on any mismatch or on
      an untargeted caller. See `docs/ENGINEERING_AUDIT.md` finding
      #064. (2) `message_bus` had no proactive notification path -- a
      direct message sat until something explicitly called
      `read_agent_inbox`. Fixed by wiring `message_bus.on_post` to a new
      `"agent"` alert kind on the real, already-existing
      `StateStore.add_alert`/SSE `state_change` path (the same one ATLAS
      run-started alerts already use, which the desktop app's
      `DesktopNotifier` already watches) -- filtered to DIRECT messages
      only, since a broadcast (e.g. news's live feed) is routine data-
      plane traffic, never surfaced as if it were urgent. Both changes
      are additive to the real backend; no mockup/artifact code touched.
      See `docs/ENGINEERING_AUDIT.md` finding #065.
- [x] Agent ecosystem hardening, round 2, shipped 2026-09-20 -- `office_logger.py`, a new,
      workspace-relative SQLite store closing the "message_bus is in-memory-only, dies on
      restart" gap named in the same design review: append-only `messages` + `fanout_events`
      tables, wired with zero new plumbing onto the SAME `message_bus.on_post` hook and the SAME
      chat `event_sink` `ActivityTracker` already consumes, plus a new read-only
      `GET /api/office_log`. Deliberately does NOT capture per-branch reasoning/tool-call
      transcripts yet (named, separate, still-not-built: `tool_use`/`tool_result` events don't
      carry a calling-agent or call-instance id, and `thinking_delta` only fires at
      `ctx.depth == 0` today) -- this persists who messaged whom and which fan-out branch ran
      where, real and durable, not yet a full chain-of-thought replay. See
      `docs/ENGINEERING_AUDIT.md` finding #066.
- [x] Agent ecosystem hardening, round 3, shipped 2026-09-20 -- closed the one backend piece
      named as still-missing at the end of the design review: real per-agent, per-call
      reasoning/transcript tagging (the "full chain of thought ... whenever we want" ask).
      `DispatchContext` gains a real, fresh-per-run `call_id`; `_emit_event` additively tags
      `tool_use`/`tool_result`/`thinking_delta`/`assistant_delta`/`assistant_text`/`brain`
      events with the real calling agent and that `call_id` (never overwriting a
      `delegate_parallel_branch` entry's own already-correct agent). This is the exact real
      fix for flaw #4 named in round 1/#066 (`ActivityTracker`'s live status collision for two
      independent `delegate_task` calls to the same agent) at the event-identity level: two
      concurrent runs against the same agent now carry distinct `call_id`s. `office_logger.py`
      gained a third table, `agent_events`, and `GET /api/office_log?kind=events`
      (`?agent=`/`?call_id=`) -- the real, on-demand, oldest-first transcript. Still not built,
      named honestly: assembling several concurrent `call_id`s from one meeting into a single
      merged conversation view is a read-side/UI concern layered on top, not yet done. See
      `docs/ENGINEERING_AUDIT.md` finding #067.
- [x] Agent ecosystem hardening, round 4, shipped 2026-09-20 -- the real fix for flaw #4 itself
      (round 3/#067 fixed the event-identity groundwork; this uses it). `ActivityTracker._record`
      now stores each `tool_use`'s real `call_id` alongside `last`, and only applies a `tool_
      result` to that slot when it genuinely belongs to the call currently occupying it -- a
      result for a superseded call still reaches the feed (never dropped) but no longer
      silently overwrites the wrong call's snapshot. New `concurrent_call_ids(agent)` /
      `GET /api/activity`'s new `concurrent_call_ids` field surfaces real concurrent activity
      (a 60s recency heuristic, named as such). Backward compatible with untagged events.
      UI work to actually render "2 active" on the office desk is real, separate, not attempted.
      See `docs/ENGINEERING_AUDIT.md` finding #073.
- [x] Agent ecosystem hardening, round 5, shipped 2026-09-20 -- the real fix for flaw #5, the local
      backend concurrency ceiling named in round 1/#066 (a real, live-observed HTTP 400 from two
      simultaneous local Ollama calls). A real process-wide semaphore gates the one real network-
      call boundary (`_call_with_retry_inner`) whenever `backend_identity(config)` says the call is
      local -- default fully serial, every other backend unaffected, a real
      `DOURMOUSE_LOCAL_MODEL_MAX_CONCURRENT` env var raises it. Live-proved with real threads and
      real wall-clock timing: local calls never overlap and take additive time; cloud calls DO
      overlap and take roughly one call's time, unaffected. Named limitation: this bounds local
      concurrency, it does not add cloud burst capacity (the other half of the originally-named fix
      direction) -- real, separate, not attempted. This closes both agent-ecosystem flaws #4 and #5
      from the original design review; only the read-side transcript-assembly UI (merging several
      concurrent `call_id`s from one meeting into one view) remains from that list. See
      `docs/ENGINEERING_AUDIT.md` finding #074.
- [x] Domain F remainder (named specialist roles) -- shipped 2026-09-19.
      `general_roster.py`'s `_DELEGATE_ROLE_PRESETS`: `researcher`, `coder`,
      `tester`, `security_sentry` each map to a real, already-registered
      subagent (no second roster system, no new tool-allowlist layer -- a
      role routed to one real subagent already only sees that subagent's
      own fixed toolset). `reviewer` deliberately deferred: no
      currently-registered subagent has a genuinely write-free toolset that
      fits reviewing arbitrary code, and a stern prompt on a write-capable
      one would not be an enforced restriction -- named as real, separate
      follow-on (register a real, narrow, read-only subagent first), not
      silently dropped. Live-verified with a real model: a real 2-branch
      fan-out with no explicit `agent_or_task` on either branch correctly
      resolved `researcher` -> `research_info` and `security_sentry` ->
      `security`, the latter returning genuinely real host/network
      telemetry. See `docs/ENGINEERING_AUDIT.md` finding #038.
- [x] Domain F closed 2026-09-20 -- the `reviewer` role, the one gap finding
      #038 deliberately deferred. A new, genuinely write-free `reviewer`
      subagent (`review_read_file`/`review_search_files`/`review_diff_
      preview` -- prefixed to avoid a real tool-name collision with
      `dev_coding`'s own bare names, caught live the moment the registry
      first built). Live-verified: a real 2-branch fan-out (`researcher` +
      `reviewer`) correctly resolved the new role and the branch genuinely
      called its own restricted tool, never attempting a write. Domain F
      is now fully closed. See `docs/ENGINEERING_AUDIT.md` finding #048.
- [x] Domain G, piece 1 shipped 2026-09-20 -- the real data model and
      persisted store (`dourmouse/research_pipeline/`), mirroring
      `research_mesh/store.py`'s own proven shape. Real bug caught by its
      own test and fixed before commit (a source-dedup loop that missed
      duplicates within the same incoming batch). No model call yet, no
      chat reachability yet -- the plan/discover/extract/synthesize stage
      functions are next, same incremental pattern as every other domain.
      Device distribution still layered on after, once the Dell node is
      confirmed reachable (it was NOT, live-checked 2026-09-19:
      `server_url_configured()` is `False`). See §9's own Build plan and
      `docs/ENGINEERING_AUDIT.md` finding #042.
- [x] Domain G, piece 2 shipped 2026-09-20 -- real `plan()` and
      `discover_sources()` stage functions (`dourmouse/research_pipeline/
      stages.py`), live-verified with a real model and the real live web,
      zero mocks: `plan()` decomposed a real question into 5 real, distinct
      sub-questions; `discover_sources()`, run through the real production
      registry, advanced a real record to `SOURCES_DISCOVERED` with 6 real
      MCP documentation/spec URLs a real `research_info` agent actually
      found and fetched. Evidence extraction (real per-source `Claim`s with
      real citations), contradiction detection, and synthesis are next,
      same incremental pattern. See `docs/ENGINEERING_AUDIT.md` finding
      #043.
- [x] Cross-cutting infra fix 2026-09-20 -- a stale LOCAL-tagged persisted
      orchestrator model ("qwen2.5:7b") was leaking into Ollama Cloud on
      every non-escalated orchestrator turn, a real 404 on this machine
      right now, live-caught while verifying Domain G piece 3 below (not
      scoped to Domain G -- affects the main chat orchestrator too).
      `OllamaConfig.model_for_agent` now skips the persisted choice
      entirely once `is_cloud`, symmetric with the fast-dispatch pin's own
      existing guard. See `docs/ENGINEERING_AUDIT.md` finding #044.
- [x] Domain G, piece 3 shipped 2026-09-20 -- real `extract_evidence()`
      (`dourmouse/research_pipeline/stages.py`), live-verified end to end
      (`plan -> discover_sources -> extract_evidence`) with a real model,
      real web fetch, and zero mocks. The model's quoted `PASSAGE` is
      validated as a real substring of the real fetched text before a
      `Claim` is ever built -- harsh acceptance test 1 enforced in code,
      not just requested in a prompt. `document_hash` is a real
      `hashlib.sha256` of the real fetched content. Contradiction
      detection and synthesis are next, same incremental pattern. See
      `docs/ENGINEERING_AUDIT.md` finding #045.
- [x] Domain G, piece 4 shipped 2026-09-20 -- real `synthesize()`, the
      final stage the current state machine supports, built ONLY from
      `record.active_claims()` so it cannot cite anything no real `Claim`
      supports; zero active claims skips the model call entirely rather
      than inviting fabrication. Live-verifying it end to end caught a
      real, separate bug: `dispatch.py`'s own plan-reminder loop leaked a
      "[DOURMOUSE: plan step(s) not executed via tools ...]" UI-only
      caveat into the stored synthesis text (and, latently, could have
      contaminated `extract_evidence()`'s `location` field too) -- fixed
      with a new `_strip_internal_diagnostics()` helper applied at every
      point these stage functions consume model text as data. Domain G's
      core single-source loop (plan/discover/extract/synthesize) is now
      fully real and live-verified end to end; contradiction detection
      remains the one real, separate, not-yet-built piece. See
      `docs/ENGINEERING_AUDIT.md` finding #046.
- [x] Domain G, workspace + document cache closed 2026-09-20 -- two real
      gaps named explicitly in this session's own status report to the
      user, closed the same day: `ResearchStore` now has a real
      workspace-relative default (`DEFAULT_DB`, matching `sentry.py`'s own
      convention), and `extract_evidence()` now caches a fetched source's
      real text to disk before the model ever sees it, keyed by a hash of
      the URL -- a repeated source costs one real fetch, not one per call,
      and `document_hash` stops fingerprinting content that no longer
      exists anywhere. Live-verified: a real fetch against
      `modelcontextprotocol.io` wrote a real cache file; a second call
      against the same URL made zero further network calls. See
      `docs/ENGINEERING_AUDIT.md` finding #047.
- [x] Domain G, contradiction detection closed 2026-09-20 -- harsh
      acceptance test 2. Added the real `Claim.sub_question` field this
      needed (a real, necessary schema gap: nothing recorded which
      sub-question a claim answered), then `detect_contradictions()`:
      groups active claims by sub-question, one real model call per pair
      judges genuine disagreement. Live-caught bug: the first live run
      against a seeded contradiction (two different completion years for
      the Eiffel Tower) found zero -- the model said "yes" but skipped the
      literal "NOTE:" label the prompt asked for, and the strict parser
      discarded a correct verdict over the formatting miss. Fixed with a
      lenient parser (only the verdict marker is required). Re-verified
      live: the same seeded contradiction now correctly detected with a
      real note; a real compatible-claims control case correctly found
      none. See `docs/ENGINEERING_AUDIT.md` finding #049.
- [x] Domain G, chat reachability closed 2026-09-20 -- a new `evidence_
      pipeline` subagent (`research_pipeline_tools.py`), six real tools
      wrapping every already-tested stage function. Real bug caught before
      any test ran: the first-draft name `deep_research` word-matched
      "research" and stole `research_info`'s own routing on 3 existing
      `test_planner.py` tests -- renamed `evidence_pipeline`, no scorer
      change needed. Real, separate gap named honestly: plain-prose
      auto-routing to the new agent isn't yet confirmed working;
      `forced_agent="evidence_pipeline"` does. Live-verified end to end:
      `research_plan` produced 5 real sub-questions, `research_status`
      accurately reported the real persisted state, through a real
      dispatch call (Gemini was genuinely down twice during verification,
      unrelated to this code -- isolated by forcing local routing for the
      one proof run). See `docs/ENGINEERING_AUDIT.md` finding #050.
- [x] Domain G, multi-source orchestration loop closed 2026-09-21 --
      `run_full_pipeline()` walks every real sub-question in the plan,
      discovering and extracting from its own new sources (capped at 3
      per sub-question, a real cost bound), skipping a failed source
      rather than aborting the run. Exposed as a seventh chat tool,
      `research_run_pipeline`. Live-verified: a real, unforced local run
      produced 5 real sub-questions, 8 real sources, 3 real passage-
      verified claims in one call. See `docs/ENGINEERING_AUDIT.md`
      finding #051. Domain G's core loop is now fully chat-reachable and
      fully automatable; only 3-device distribution (blocked on the Dell
      node) and natural-language auto-routing to `evidence_pipeline`
      remain.
- [x] Domain I, known-device baseline shipped 2026-09-21 (Phase 2 step 1,
      harsh acceptance test 2) -- `SentryStore` gains a real `known_
      devices` table built from `platform_adapter.get_arp_neighbors()`'s
      already-real telemetry; `_detect_findings` gains a third real,
      deterministic rule (a device not in the baseline is a MED
      finding), same scoring/persistence/alert pipeline every other
      finding already uses. First scan against an empty baseline seeds
      it silently (no false-positive storm); a genuinely new device is
      an honest MED finding on the next scan. Live-verified against this
      machine's real 16-device ARP table: silent seed, then exactly one
      correct finding on a real injected 17th device. New
      `security_known_devices` chat tool lists the baseline. See
      `docs/ENGINEERING_AUDIT.md` finding #052. Remaining Phase 2 steps
      (threat-intel enrichment, incident tracking, correlation, local
      remediation) not yet built.
- [x] Domain I, threat-intelligence enrichment shipped 2026-09-21 (Phase
      2 step 2) -- `platform_adapter.get_established_connections()` (new
      real `lsof -sTCP:ESTABLISHED` telemetry) plus `security/
      reputation.py`, a real keyed AbuseIPDB lookup, honestly `NOT
      CONFIGURED` without `ABUSEIPDB_API_KEY` (same convention as
      `worldmonitor.py`'s own key), refusing private/LAN targets before
      any network call. New `security_external_peers`/
      `security_check_reputation` chat tools. Live-verified against this
      machine's real network: 15 real external peers listed correctly,
      a real private IP refused, a real public IP honestly reported
      NOT CONFIGURED (no key on this machine). See
      `docs/ENGINEERING_AUDIT.md` finding #053. Remaining Phase 2 steps
      (incident tracking, correlation, local remediation) not yet built.
- [x] Domain I, incident/case tracking shipped 2026-09-21 (Phase 2 step
      3) -- `SentryStore` gains a real `incidents` table
      (`OPEN -> INVESTIGATING -> RESOLVED/ACCEPTED_RISK`, mirroring
      `goals.py`'s own terminal-states discipline). An unknown
      fingerprint is honestly refused; a terminal incident refuses to
      change status but still accepts a note or a same-status
      re-confirmation. New `security_incident_open`/
      `security_incident_update`/`security_incidents` chat tools.
      Live-verified against this machine's own real, currently-true
      disabled-firewall finding: opened, moved to INVESTIGATING with a
      real note, closed ACCEPTED_RISK, correctly refused to reopen. See
      `docs/ENGINEERING_AUDIT.md` finding #054.
- [x] Domain I, correlation engine + specific remediation text shipped
      2026-09-21 (Phase 2 steps 4-5, closing the scale-out plan) --
      `_detect_correlations()` fires one real HIGH finding when a real
      new-device AND a real exposed-port finding both appear in the SAME
      scan (a same-window coincidence rule, never re-fires once either
      condition is already known). Every detection rule's
      `recommended_action` upgraded to real, specific, copy-pasteable
      text (the exact `socketfilterfw`/`pf` commands, the specific
      MAC/IP to block) -- text-only, nothing here executes anything.
      Live-verified: a synthetic injected new device AND exposed port in
      the same real scan correctly produced both underlying findings
      plus the HIGH correlation. See `docs/ENGINEERING_AUDIT.md` finding
      #055. Domain I's Phase 2 scale-out plan is now fully closed;
      remaining Domain I work is the dashboard UI, Windows/cross-device
      coverage, and the much larger balance of the founding spec's
      54-item cybersecurity build list.
- [x] Domain H, piece 1/7 (project-instruction file, `DOURMOUSE.md`) -- shipped
      2026-09-19. `dourmouse/project_instructions.py` reads the workspace's
      own `DOURMOUSE.md`, spliced into `dispatch.py`'s shared
      `system_message()` alongside (never instead of) the base governance
      rules -- every backend and delegation depth picks it up automatically,
      no second injection path. Live-verified with a real model: a real
      `DOURMOUSE.md` instruction survived a real chat call AND a real
      delegation hop. See `docs/ENGINEERING_AUDIT.md` finding #037.
- [x] Domain H, piece 4/7 (deterministic hooks: pre-tool/post-tool/stop/session) shipped
      2026-09-20. New `dourmouse/hooks.py`: pre-tool hooks can genuinely block (a real
      denial, short-circuiting), every other type is a pure observer matching this
      codebase's existing `on_post`/`on_event` discipline; a raising hook is always
      swallowed as "no opinion." Wired at the ONE real call sites the plan itself named:
      `_execute_tool` (pre/post-tool) and `ChatSession`'s lifecycle (`__init__`/`ask`/new
      `close()`, plus all 3 real REPL exit paths). Honest limitation: `webui.py`'s
      long-lived server-side session never calls `close()` (process exit is its real end,
      no hook point yet). See `docs/ENGINEERING_AUDIT.md` finding #068.
- [x] Domain H, piece 5/7 (session-compaction audit) closed 2026-09-20 -- audited before
      building anything, per this section's own instruction: real, structured context
      compaction already existed (`dispatch.py`'s pre-existing, already-tested
      `_bounded_context()`) alongside the real, never-lossy `ChatSession` JSONL/
      `.messages.json` persistence. No new code; closed as a documentation finding. See
      `docs/ENGINEERING_AUDIT.md` finding #069.
- [x] Domain H, piece 3/7 (Skills-as-modular-capability-packages) shipped 2026-09-20. New
      `dourmouse/skills.py`: a `dourmouse/skills/<name>/SKILL.md` convention (minimal
      frontmatter, no new dependency), loaded ONLY when a turn's own text overlaps a
      skill's declared keywords -- the same deterministic, no-LLM-judgment discipline
      `planner.find_agents_for_query` already established for subagent routing, applied
      to capability packages instead. Spliced into `ChatSession.ask()` as a trailing
      system message (never touching the immutable base prompt), same pattern the memory
      recall block already uses. Ships the infrastructure only; the skill library itself
      is real, separate, ongoing work. See `docs/ENGINEERING_AUDIT.md` finding #070.
- [x] Domain H, piece 6/7 (programmable SDK-style headless interface) shipped 2026-09-20.
      New `dourmouse/sdk.py`: a thin `Dourmouse` facade generalizing the one existing real
      instance of this shape (`research_mesh/pipeline.py`'s own CLI) rather than a new
      pattern -- `ask()` returns the real `ChatSession` report unmodified, `close()`/
      context-manager fires the real session-stop hook from piece 4. New scriptable CLI
      (`python -m dourmouse.sdk "prompt" --json`) distinct from `python -m dourmouse.chat`'s
      interactive REPL. See `docs/ENGINEERING_AUDIT.md` finding #071.
- [x] Domain H, piece 7/7 (MCP, second half: Dourmouse as an MCP client) shipped 2026-09-20 --
      DOMAIN H NOW FULLY CLOSED. Found first that the SERVER half already existed
      (`dourmouse/mcp_bridge.py`, built earlier under a different initiative, never marked
      against this checklist -- same situation piece 5 found). New `dourmouse/mcp_client.py`:
      a real stdio JSON-RPC 2.0 client, same config shape (`mcpServers`) Claude Code's own
      `.mcp.json` uses, real tools wrapped as `mcp__<server>__<tool>` under a new opt-in
      `mcp_tools` subagent (never a standing empty one). One bad server never blocks another
      or breaks `build_general_registry()`. Real end-to-end test connects a real `McpClient`
      to a real `dourmouse.mcp_bridge` subprocess -- this codebase's own client and server
      halves proven to interoperate, not two isolated mocks. See
      `docs/ENGINEERING_AUDIT.md` finding #072.
- [x] Domain I, real sentry shipped 2026-09-19 -- `dourmouse/security/sentry.py`,
      a fully deterministic scan (corrected away from the original plan's
      LLM-scored design before writing any rule -- a security finding must
      be exactly reproducible). Real, live-verified against THIS machine's
      own real, previously-known disabled firewall: correctly detected,
      scored (risk 33.0), alerted once (HIGH only), persisted, and
      correctly suppressed on a real repeat scan. `security_sentry_scan`/
      `security_sentry_dismiss` reachable from chat now. NOT built:
      new-LAN-device detection, a continuously-running background
      scheduler (needed for the domain's own "unprompted, bounded window"
      wording), the live SSE push, and the dashboard UI -- all named
      explicitly as real, separate follow-on. See
      `docs/ENGINEERING_AUDIT.md` finding #039.
- [x] Domain I, continuous sentry shipped 2026-09-20 (user-directed: "always
      running... continuous data stream") -- `SentryRuntime`, the exact
      real daemon-thread shape `GoalRuntime` already uses, wired into
      `webui.run_server` startup, default ON. Live-verified: a real server
      boot, zero chat messages, already had 6 real findings persisted and
      a real HIGH alert written before any user action. Interval floor
      60s, default 300s -- a real scan shells out to real system commands
      per tick, not a free check. Known minor limitation found live: a
      dual-stack (IPv4+IPv6) service on the same port double-counts in one
      tick's risk score -- named, not fixed. Live SSE push, new-device
      detection, and the dashboard UI remain real, separate follow-on. See
      `docs/ENGINEERING_AUDIT.md` finding #041.
- [x] Domain J, primary screen retoned 2026-09-20 -- unblocked (user supplied
      the real Hermes reference screenshot). Real pixel colors extracted
      programmatically, not guessed; `console.html`'s default "Terminal
      Core" theme + `dourmouse-ui.css`'s shared tokens now use the real
      extracted dark-green family, live-verified in the browser. Font
      family NOT changed -- unextractable with confidence from a raster
      screenshot. Real gap found, not yet fixed: `workspace.html` (and
      possibly other files among the 16 linking the shared stylesheet)
      locally overrides the canvas color instead of inheriting it --
      unaudited beyond the primary screen. See
      `docs/ENGINEERING_AUDIT.md` finding #040.
- [ ] **New, user-directed 2026-09-20 ("browser is key... replace every app")**: the embedded
      browser/PDF/media shell, audited (finding #075) but not yet built out. Real state: browser
      automation and an in-page iframe pane are real and working (with a proxy fallback for
      framing-blocked sites, no live login there); PDF has a real PDFium page-image viewer; a
      faster Electron `BrowserView` embed exists but is not on the default launch path; there is
      **no embedded audio/video player anywhere** (Spotify is remote-control only, no YouTube
      integration at all). Real gaps to close, not yet scoped into steps:
      1. Make the Electron shell (real CDP-connected `BrowserView`) the default launch path, or
         explicitly decide to keep pywebview and drop the Electron migration -- currently neither
         is true, it is just half-built.
      2. ~~A real embedded audio/video player~~ **DONE 2026-09-23, finding #076.** Real
         native `<audio>`/`<video>` in `ui/file_preview.html`, backed by a new
         `GET /api/files/media` with real HTTP byte-range support (206/416, streamed in
         256KB chunks, never `read_bytes()` on a video). `open_file_preview` now accepts
         media and refuses an undecodable container by name rather than opening an empty
         player. Live-verified: a real H.264 file decoded and SEEKED in the browser
         (`readyState: 4`, seek completed), real AAC audio with native transport, and
         byte-exact ranges confirmed against `dd` on the same offsets. A real bug the unit
         tests missed was found by that live run and fixed (an open-ended range past the end
         served a 200 instead of a 416). Honest limit: rendering inside the pane's sandboxed
         iframe is NOT verified -- this test browser blocks sandboxed-iframe navigation
         outright (`ERR_BLOCKED_BY_CLIENT`, reproduced identically on a pre-existing image
         path), so that step needs the real desktop shell. Also fixed en route: the tool used
         to hand the pane an absolute `127.0.0.1` URL for this app's own page while the
         console may be loaded as `localhost`, framing its own page cross-origin for no
         reason; it now sends a root-relative, same-origin URL.
      3. Real login/session handling for the iframe-pane's proxy fallback, or an honest, visible
         "this site can't be embedded, use `open_url` instead" UI state rather than a silently
         cookie-less proxy.
      4. Live-verify the existing browser pane, PDF viewer, and Spotify remote control against a
         real range of sites/documents (not just the one path already proven) -- brutal, no
         assumed-working claims.

## Notes / decisions log

- 2026-09-16: Chose audit → runtime → UI → security ordering. Runtime is the highest product
  value and most novel piece; UI redesign should visualize it once it exists rather than being
  redone twice; security is the largest standalone vertical, done last.
- 2026-09-16: Declined the spec's literal `src/core/agents/tools/...` directory reorg as a
  big-bang move — too risky against a live, daily-used app. Reorganizing incrementally per
  subsystem as each is touched instead.

- [x] Domain J, OS mockup redesigned and owner-approved as the reference UI 2026-09-24
  (finding #083) -- `ui/os_mockup.html`. Real OS shell (window chrome, Control Centre,
  Notification Centre, live accent theming), Research and Security drawn as flowcharts,
  a Claude-preview-style resizable Browser, two gimmick screens removed. Prototype only,
  wired to nothing; live wiring waits behind the §0 foundation (see the tracking folder).
- [x] Phase 1 X-7 + X-4 (finding #084) 2026-09-24 -- both "flaky" tests were real bugs (Atlas
  request lock held across a network git pull; test fixture doing real git), the activate test
  now accepts the OS's honest refusal, and a 112-test orphan `tests/` tree that the suite command
  never ran was consolidated into `dourmouse/tests` with its 5 failures fixed at the root.
  Same finding: every server test leaked a real security scanner (now off in tests, and stopped on
  real shutdown), six import-time store paths let the suite write into the real workspace (now
  resolved per call, AST-guarded), and one approve test timed out before the server's own budget.
  Suite 5565 passed, 0 failed.
- [ ] Phase 1 X-1 (finding #085) 2026-09-24 -- CI rewritten to actually run on the working branch
  (3-OS matrix, Python 3.14, one gated pytest run) plus a lint ratchet (ruff per-rule and mypy
  total may only go down; baseline ruff 447, mypy 352). Open until all runner jobs are green.
  Runs 1-2 found missing CI deps (fastapi, desktop/voice extras), an undeclared Pillow dependency,
  a Linux-only mypy error from a gitignored import, and the Windows defects in #087/#088.
- [x] Phase 2 X-9 / R0-SEC (finding #086) 2026-09-24 -- fetch_url SSRF: new net_guard pins each
  connection to the vetted address, re-checks every redirect hop (cap 5), checks every DNS answer
  with is_global (CGNAT and mapped IPv6 caught), ignores proxies. Old path proven to follow a 302
  to 169.254.169.254. reputation.py shares the rule.
- [x] Security sentry ARP view (finding #087) 2026-09-24 -- `arp -a` reverse-resolved 206 LAN
  names past its 5s timeout, blanking new-device detection; now `arp -an`, stored names kept.
- [x] Windows portability (finding #088) 2026-09-24 -- coding-CLI task on stdin (first Claude turn
  hit cmd.exe's 8191-char limit), UTF-8 on all 31 text subprocess calls, exclusive port bind,
  project lookups by raw per-tool path, CLI discovery with extensions and npm folder.
- [x] Phase 2 R0-6 + R0-4 + R0-5 (finding #089) 2026-09-24 -- research_pipeline/acquire.py: raw
  bytes stored content-addressed with fetch metadata, real charset and content-type handling,
  final URL and redirect chain recorded; evidence fetched directly (no LLM), document_hash is the
  hash of stored bytes; fetch_url cuts at word boundaries and says so.
- [x] Kill-switch path (finding #090) 2026-09-24 -- tray, live_feeds, world_watch_regions and
  world_pulse_history defaulted to a cwd-relative workspace; now config.workspace_dir(), guarded.
- [x] Phase 2 R0-1 (finding #091) 2026-09-24 -- stdlib main-content extraction: chrome removed,
  article picked, heading paths kept; claim location computed from the document, not guessed.
- [x] Phase 2 R0-2 (finding #092) 2026-09-24 -- headless render for JS-only pages; every request
  the page makes is served through the SSRF guard; rendered DOM stored linked to the server bytes.
- [x] Tray on Linux (finding #093) 2026-09-24 -- an em dash in the tray title crashed pystray's
  X11 backend (Latin-1); title is ASCII now, all four states tested.
- [x] Phase 2 R0-3 (finding #094) 2026-09-24 -- robots.txt honoured and per-host spacing (Crawl-delay,
  capped) for every automated fetch. R0 acquisition complete.
- [x] Phase 2 R1 + R2 (finding #095) 2026-09-24 -- research_graph: 21 typed objects (immutable
  evidence chain, versioned interpretations with as_of), typed edges, all-or-nothing sync, one-time
  non-destructive migration off the JSON blob; ResearchStore keeps the graph in step.
- [x] Phase 2 R3 (finding #096) 2026-09-24 -- the backward edge: contradiction spawns a follow-up
  task, evidence tagged with it enters after synthesis, revised synthesis kept alongside the old;
  stages forward-only; contradictions surfaced in synthesis; research_follow_up tool.
- [x] Device network (finding #097) 2026-09-24 -- stdlib node service (data + compute roles, token
  auth, Tailscale-only bind, per-job env strip + time + memory limits), Mac client and registry.
  Desktop compute node live over Tailscale; Dell pending its SSH setup.
- [x] Mac-only + job environment (finding #098) 2026-09-24 -- owner scrapped other devices; job runner
  becomes the Mac's sandbox with a real memory limit (RSS watchdog) and an environment hash per job.
- [x] Phase 3 MS-1 (finding #099) 2026-09-24 -- Mac telemetry: Wi-Fi security, host protections,
  process identity and code signatures, persistence, diagnostics.
- [x] Phase 3 MS-2 + MS-3 (finding #100) 2026-09-24 -- baseline engine (per network, learning period)
  and Mac detectors (posture and change); live scan of this Mac reports its real posture.
- [x] Phase 3 MS-4 (finding #101) 2026-09-24 -- Downloads watch and file assessment (type by bytes,
  origin, signature, Gatekeeper, decoy tricks); honest "not malware-scanned" without ClamAV.
- [x] Phase 3 MS-5 (finding #102) 2026-09-24 -- "am I being monitored?": eight indicators with
  evidence and confidence, and an explicit Unknowns list.
- [x] Phase 3 MS-13 (finding #103) 2026-09-25 -- lockdown: blocklisted apps closed on launch, websites
  blocked system-wide via a minimal, validated root helper (one sudo install), approval-gated switches.
- [x] Phase 3 MS-6 (finding #104) 2026-09-25 -- connection failure taxonomy: one named cause per
  failure (BLOCKED / DNS / ROUTING / UNREACHABLE / TIMEOUT / TLS / SERVER) with step evidence.
- [x] Phase 3 MS-7 (finding #105) 2026-09-25 -- posture in six areas (unknown never shown as good) and
  the one-button report with an Unknowns section.
- [x] Phase 3 MS-8 (finding #106) 2026-09-25 -- approval-gated response actions: kill, quarantine
  (restorable), disable a startup item, block a domain for good.
- [x] Phase 3 MS-9 (finding #107) 2026-09-25 -- AI analyst on a large cloud model: explains, must cite,
  cannot invent findings; wakes once per new set.
- [x] Phase 3 MS-10 (finding #108) 2026-09-25 -- immediate scan on network change; every scan live on
  the event hub.
- [x] Finding #109 2026-09-25 -- `/api/security/action` for the console.
- [x] Finding #110 2026-09-25 -- SECURITY screen rebuilt around posture by area and one-click tools;
  three root-cause fixes found doing it (IPv4/IPv6 duplicate findings, risk hidden as unknown,
  routing leak).
- [x] Phase 3 MS-11 (finding #111) 2026-09-25 -- browser history and typed searches, all Chromium
  profiles plus Safari and Firefox, local only; Safari's TCC gap reported with the fix.
- [x] Phase 3 MS-12 (finding #112) 2026-09-25 -- privacy mode, Dourmouse self-audit, threat model.
  Mac-only security plan (MS-1..MS-13) complete.
- [x] Finding #113 2026-09-25 -- Dell/qwen compute node retired (policy); `compute` runs sandboxed
  Python jobs on this Mac; MODEL-2 verified and pinned by a policy test.
- [x] INFRA-1 (finding #114) 2026-09-25 -- standing-agent runtime: inbox-woken loops, read/propose
  leash, visible activity log.
- [x] OS-5 (finding #115) 2026-09-25 -- always-on file librarian: incremental index, search for
  chat and other agents, tidy proposals applied only on approval, undo.
- [x] OS-3 (finding #116) 2026-09-25 -- the console's browser pane drives the real Electron
  BrowserView (real cookies, real history, sized to the pane; agent and user share it).
- [x] OS-10 (finding #117) 2026-09-25 -- every media format plays: ffmpeg remux first, transcode only
  what needs it, cached; subtitles; remembered position.
- [x] OS-6 (finding #118) 2026-09-25 -- a project's chat works inside its own folder (file and
  coding tools scoped, parallel branches included).
- [x] OS-8.1 (finding #119) 2026-09-25 -- notification center: bell, history, per-source mute; the
  security, downloads, analyst and librarian events feed it.
- [x] OS-8.3 (finding #120) 2026-09-25 -- every background switch settable from the console.
- [x] OS-8.2 + OS-9 (finding #121) 2026-09-25 -- Cmd+K launcher; app.html and os.html retired into
  the console; service worker made network-first (stale console after updates fixed).
- [x] OS-7 (finding #122) 2026-09-25 -- Calendar write scope actually requested (create_calendar_event
  used to 403 forever); sheets_append for existing Sheets.
- [x] Phase 5 A0 + A1 (finding #123) 2026-09-25 -- fan-out branches carry their run's call_id; a
  meeting reads as one conversation in the OFFICE screen.
- [x] Phase 5 A4 (finding #124) 2026-09-25 -- hostile bus broadcast: data envelope plus the approval
  gate proven to stop an obedient model.
- [x] Phase 5 A2 (finding #125) 2026-09-25 -- agent window streams thinking, tools, answer and approvals.
- [x] Phase 5 A3 (finding #126) 2026-09-25 -- office desks show concurrent runs. Phase 5 A0-A4 complete.
- [x] R5 + RES-18 (finding #127) 2026-09-25 -- experiments, runs, metrics and replication as linked graph objects.
- [x] R6 (finding #128) 2026-09-25 -- append-only event log; graph changes (commit-only) and scans; agents wake on events.
- [x] R8 (finding #129) 2026-09-25 -- the research view on the RESEARCH screen.
- [x] Finding #130 2026-09-25 -- split mode sent research agents to a tool-less Gemini client; now Ollama
  Cloud with the configured model.
- [x] R4 (finding #131) 2026-09-25 -- hypothesis generation (claim-grounded), criticism, experiment design, deterministic statistics.
- [x] UI-5 + UI-7 (finding #132) 2026-09-25 -- live-event array capped; tool chips keyboard-accessible.
- [x] R7 (finding #133) 2026-09-25 -- run policy (loop breaker, approval budget) and an action ledger at the single tool choke point.
- [x] AGENT-3 (finding #134) 2026-09-26 -- cloud burst (16 concurrent branches on cloud backends); branches never re-fan-out; branch prompts scoped to their own task; 429 backoff with Retry-After; delegate_to_models honest and inherits the caller's backend.
- [x] Finding #135 2026-09-26 -- request guard: Host/Origin/Sec-Fetch-Site checks on the local server, launch token on the wildcard-CORS file preview routes, pane bridge check (security review S01, S07, S31, S32).
- [x] Finding #136 2026-09-26 -- lockdown, quarantine and analyst hardening (review S02-S06, S08-S12, S25, S26, S28).
- [x] Finding #137 2026-09-26 -- model-written code sandboxed; code tools, schedules and goal approvals need the owner (S13-S15, S19, S21-S23).
- [x] Finding #138 2026-09-26 -- DLP (and its hang), self-extension injection, standing agents (S16, S20, S24, S29, S30).
- [x] Finding #139 2026-09-26 -- pane proxy SSRF and CSP sandbox; Electron permissions and navigation (S33-S36).
- [x] Finding #140 2026-09-26 -- one run policy per request; damaged history file recovery (S17, S27).
- [x] Finding #141 2026-09-26 -- lockdown page paths via MV3 extension; R9 answer critic.
- [x] Finding #142 2026-09-26 -- request bodies bounded (413/400); host guard accepts owner-listed proxy names and name:port.
- [x] Finding #143 2026-09-26 -- OS shell route (/shell, strict CSP) and the os_api plug-in router for screen backends; the shell and its 18 screens are next.
- [x] Finding #144 2026-09-26 -- OS shell foundation (chrome, core, kit), the HOME and SECURITY screens, the shared thread view and the live-check tool; 16 more screens next.
- [x] Finding #145 2026-09-26 -- `/api/office_log?meeting=` no longer sends a second response after the meeting.
- [x] Finding #146 2026-09-26 -- OS shell screens NEWS, ATLAS, WIKI on real data (wave A); ctx.keys.pushEsc.
- [x] Finding #147 2026-09-26 -- OS shell screens GOALS, TIMETABLE, ORCHESTRATION, OFFICE (wave B); real goal pause and resume; kit confirmHere.
- [x] Finding #148 2026-09-26 -- swap preparation: DOURMOUSE_DEFAULT_SHELL, DOURMOUSE_ELECTRON_START_PATH, native startup sign-in check in the shell.
