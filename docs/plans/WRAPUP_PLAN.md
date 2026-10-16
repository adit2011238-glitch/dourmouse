# Wrap-up plan (written 2026-10-16): finish, and make it work really well

Owner's instruction: wrap this up, but make sure it works very well. This plan is the shortest path that still proves it. Everything not on the path is moved to a named post-launch backlog (nothing is dismissed; each item stays in the ledger).

State at writing: commit c38e0c8 (fix waves 1 to 2d, PRIV and its review, DRM build) is pushed; suite about 9,700 green; ledger `FIX_LEDGER_2026-10-06.md`; Wave 3 (FI1, FI2, FU, 89 findings) is running; weekly usage 0 percent (resets 2026-10-23), 5-hour window fresh. This chat's context was at 80 percent; all state is on disk (memory note dourmouse_browser_next_phase.md, ledger, work files), so a /compact loses nothing.

## The path (in order)

1. **Wave 3 finishes** (FI1, FI2, FU, Sonnet): read-only review (dm-code-scanner, Sonnet medium), full suite in two halves, commit, push, backup.
2. **Automated end-to-end smoke on the real installed app** (a Sonnet agent, isolated copy, no cloud calls; `scripts/` plus Playwright over the Electron CDP port): launch the app; open all 19 screens and fail on any console error, blank page or failed request; BROWSER: open a tab, load a local page, find, zoom, download a local file; permissions bar; privacy card in SETTINGS and the Private switch; APPS screen; crash recovery (kill the server, it comes back); DRM status line; the 8 high-severity attacks from the scan (case-variant path write, `say -f`, shell curl to the owner routes, key press tool, mobile token rotate...) as regression checks against the live server. Output: `EVIDENCE/182_smoke_*` plus a report. Anything red becomes a ledger row fixed before Gate A.
3. **Cheap mechanical hunts** (one Sonnet medium agent): HUNT-A (the 489 ruff and 346 mypy findings and 158 `except: pass` sites triaged: real bug, intended, or needs a log line) and HUNT-B (a script `scripts/contract_check.py` comparing UI routes with server routes, preload with IPC handlers, tool names with descriptions, prompts and bench tasks). New defects become ledger rows and are fixed in the same wave when small.
4. **Speed**: run `scripts/perf_check.py` against PERF_BUDGET.md; fix the single worst offender if cheap.
5. **The one unread gap**: group U2 (72 UI files, 23,000 lines) read once by dm-code-scanner (Sonnet medium), findings fixed.
6. **Final limited re-read (replaces the full wave 5)**: dm-code-scanner over only the files changed since the first scan that are security-relevant (privacy.py, order_guard.py, webui.py auth and claim parts, dispatch.py gates, electron/main.js IPC, browser_agent.py), looking for anything above low.
7. **Gate A: you test the app** with `MANUAL_TEST_PLAN.md` part 1 (and the trading demo checklist if you will trade). Your FAIL/ODD notes become ledger rows `M-n`.
8. **Fix your Gate A list**, suite, commit.
9. **Gate B: RESEARCH and SECURITY tested together** (`MANUAL_TEST_PLAN.md` parts 2 to 4), plus the real 40-task benchmark (needs your OK, uses your cloud key; target 85 percent) and a clean-account install check (`SHIP_PLAN.md`).
10. **Release notes and final tracking docs**; ledger reconciled (every row fixed, owner-accepted, or in the backlog with a reason).

## Post-launch backlog (moved, not dismissed)

HUNT-C concurrency audit, HUNT-D fuzzing, HUNT-E second-opinion security pass by Opus, HUNT-F test-quality audit, HUNT-G dependency audit (needs network OK), OPT-2 token cost per turn, OPT-3 request path, OPT-4 polling, OPT-6 big files, OPT-7 UI rendering; the 13 open and 28 partly fixed UX items; the calendar, files and notes screens (need backend routes); the signed self-contained build blockers (SHIP_PLAN.md); Netflix and Disney+ (VMP signing needs a castLabs account); extensions per profile.

## Owner-only items that gate "done"

Sign in to Google once in the pane and run `scripts/live_checks/docs_and_youtube.md`; grant Accessibility and try one TextEdit drive; supervised demo order on Trading 212 and MT5 (`TRADING_DEMO_CHECKLIST.md`) before any real order; tick the provider retention boxes in SETTINGS, PRIVACY; decide the real-model benchmark; pick the folders once in the extension and Chrome import pickers; verify at least one protected title plays (Spotify Web or YouTube Premium).

## Definition of done for this wrap-up

Full suite green; smoke report green; the 8 high attacks pass live; no ledger row above low left open without an owner decision; Gate A and Gate B signed off by the owner; PERF_BUDGET numbers met or the miss recorded.
