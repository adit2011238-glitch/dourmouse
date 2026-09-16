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
- [ ] Security pass: shell/subprocess call sites, path traversal, credential handling, prompt-injection boundary for tool output / external content.
- [ ] Concurrency pass: shared mutable state, race conditions in session/job handling.
- [ ] Error-handling pass: bare excepts, silent failures, missing timeouts.
- [ ] Dead code / duplicate utility sweep.
- [ ] `docs/ARCHITECTURE.md`, `docs/SOURCE_MAP.md`, `docs/DEVELOPMENT.md`, `docs/TESTING.md` written from real findings.

## Phase 2 — Autonomous agent runtime (the core new system)

Direct translation of the spec's Claude-Code-derived architecture onto Dourmouse's existing
dispatch/agent system (additive, not a rewrite — reuse `dispatch.py`'s model-call plumbing,
`planner.py`/`agent_router_model.py` routing, the existing tool registry and permission gate).

- [ ] `Goal` and `Task` persistent objects + durable store (survive restart).
- [ ] Real background worker/runtime independent of the chat UI (biggest known gap — today
      everything is request-scoped; confirming exact shape via the architecture-map agent).
- [ ] Plan → Execute → Observe → Verify → Replan loop with an independent verification step
      (never trust "the model says it's done").
- [ ] Failure classification + bounded, non-identical-repeat retry + recovery strategies.
- [ ] Approval-gate system integrated with the existing `DOURMOUSE_AUTO_APPROVE` / confirmation
      gate rather than a second, competing one.
- [ ] Scheduler for routines (time-based and event-based), surviving restarts without a new chat
      prompt.
- [ ] Multi-agent delegation building on the existing `delegate_task` primitive.
- [ ] **Cross-device control** (explicit user requirement, 2026-09-16): Dourmouse should be able to
      act on every device on the Tailscale network — every app, every browser — with the same
      reach a user has at the keyboard, gated by the same approval layer as any other high-risk
      action (not a separate permission model). Builds on the existing node/remote-job
      architecture referenced in the spec (Mac/Windows/Dell nodes) rather than a new one.
- [ ] Live progress model + notifications through the existing notification mechanism.
- [ ] Run the spec's 20 acceptance tests for real against the implementation.

## Phase 3 — UI/UX redesign (Claude Desktop / Claude Code interaction quality)

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

- [ ] `SecurityPlatformAdapter` (macOS first, matches current deployment).
- [ ] Network/host telemetry (connection profile, listening ports, outbound connections, ARP/DNS).
- [ ] Baseline engine + security event schema + local AI sentries (evidence-backed, never fabricated).
- [ ] Security dashboard, integrated into the Phase 3 design system, not a bolt-on.
- [ ] Threat model doc + security test suite (including prompt-injection-via-network-data tests).

## Notes / decisions log

- 2026-09-16: Chose audit → runtime → UI → security ordering. Runtime is the highest product
  value and most novel piece; UI redesign should visualize it once it exists rather than being
  redone twice; security is the largest standalone vertical, done last.
- 2026-09-16: Declined the spec's literal `src/core/agents/tools/...` directory reorg as a
  big-bang move — too risky against a live, daily-used app. Reorganizing incrementally per
  subsystem as each is touched instead.
