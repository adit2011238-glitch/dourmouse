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

- [ ] Domain E (device wiki) -- **blocked**, re-check
      `ps aux | grep -i "claude.*Documents/dourmouse"` before writing a
      single file, not just before the live-proof step. See §7's own Build
      plan.
- [ ] Domain F remainder (named specialist roles) -- a `role` table on
      `delegate_parallel` branches, not a second roster system. See §8's
      own Build plan.
- [ ] Domain G (structured research pipeline + 3-device network) --
      single-device pipeline first, device distribution layered on once
      the Dell node is confirmed reachable (it was NOT, live-checked
      2026-09-19: `server_url_configured()` is `False`). See §9's own
      Build plan.
- [ ] Domain H (Claude Code feature duplicates) -- 7 sub-features,
      sequenced by real dependency/effort; the project-instruction-file
      question is now answered (confirmed by grep: none exists yet, a
      clean-slate gap). See §10's own Build plan.
- [ ] Domain I remainder (AI security sentries) -- the real
      `InvestigationPhase` architecture, mapped onto the exact
      `research_mesh` state-machine template. See §11's own Build plan.
- [ ] Domain J (Hermes UI) -- **blocked** on the user's own reference
      images; the mechanical token-swap plan is ready so execution is
      near-instant once unblocked. See §12's own Build plan.

## Notes / decisions log

- 2026-09-16: Chose audit → runtime → UI → security ordering. Runtime is the highest product
  value and most novel piece; UI redesign should visualize it once it exists rather than being
  redone twice; security is the largest standalone vertical, done last.
- 2026-09-16: Declined the spec's literal `src/core/agents/tools/...` directory reorg as a
  big-bang move — too risky against a live, daily-used app. Reorganizing incrementally per
  subsystem as each is touched instead.
