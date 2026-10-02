# Dourmouse execution plan: phases, models, effort, parallel waves (2026-10-02)

Goal and decisions: see `PLAN_REPLACE_EVERYTHING.md` and `PRODUCT_VISION_AND_STATUS.md`.
Rules that never change (HARD_RULES.md): one checkout `~/dourmouse-recon` branch `recon-2026-09-11`;
full suite green before every commit (13 min, run only when no agent is writing); a numbered
finding in `docs/ENGINEERING_AUDIT.md` plus a roadmap line; lint ratchet; no em dashes; never
fabricate; never touch ports 8765/9333/9334 (use 18xxx demo ports, isolated data dirs); never
pkill, kill by PID; no real chat messages to the cloud model in tests (use `--stub-chat`);
push with the owner account (`adit2011238-glitch`, see section 6); check BOTH usage windows
(`get_usage`) before every wave, stop line 95 percent weekly.

## 1. Model and effort policy (to save usage and still do it very well)

Owner choice: Sonnet 5.5 by default, Opus 5.5 only where it is needed (marked below). In the
workflow and agent tools the model values are `sonnet` and `opus`, which resolve to these.

- **Sonnet 5.5, medium**: default builder for well-specified work (screens, tools, tests).
- **Sonnet 5.5, high**: builders on risky or cross-cutting code (routing, approvals, Electron main).
- **Opus 5.5, high**: only design-heavy or security-critical phases where a wrong design is costly
  (phase F app driving, phase H security fixes, phase C collision lock). Roughly 3 to 5 times the
  cost of Sonnet; use sparingly.
- **Sonnet 5.5, low or Haiku 4.5**: mechanical work (restyles, doc updates, running the benchmark,
  screenshots, evidence collection). 
- **Reviewers** (read-only, find bugs and holes): Sonnet high, one per wave, after the builders.
- **Main thread** (you and me): Sonnet 5.5, medium; I integrate, run the suite, commit.
- Cost guide: a Sonnet builder is about 1 to 4 weekly points; Opus 3 to 5 times that. Weekly
  budget allows about 25 builders if nothing else runs.

## 2. Phases, each with owner files, model, effort, exit test

| Ph | What | Model, effort | Owns (only these files) | Exit test |
|---|---|---|---|---|
| J | Shared desk: every chat box can open the browser and player; open txt, md, csv, json, code; player controls as tools | Sonnet, high | `webui.py` routing and preview route, `dispatch.py` scoping, `general_roster.py`, `system_access.py` (open_file_preview), `code_backends.py`/`mcp_bridge.py` | from each of 6 chat boxes: open a PDF, an mp3, a txt, google.com; one test per screen |
| D | Player and reader: video, audio, PDF reader with annotations, queue, now playing, media keys, YouTube and Spotify Web surface | Sonnet, medium | `ui/assets/os/screens/media/**`, `os_api/media.py`, `media_convert.py`, new `pdf_*` files | play mp3 and mp4, open a PDF and highlight, queue 3 items; live in Electron |
| G0 | Benchmark harness: 40 real tasks, scripted runner with stub and real modes, pass-rate report | Haiku or Sonnet low | new `scripts/bench/**`, new tests only | runs, prints pass rate; no cloud spend unless `--real` |
| F1 | App driving core: accessibility-tree reader, click and type by element, per-app allow list, indicator, kill switch (design first, then build) | Opus, high | new `dourmouse/app_driver/**`, new `os_api/apps.py`, new tests | drive TextEdit and Music through approvals; stop button works |
| B | Chrome parity browser: tabs, window, bookmarks, history, downloads, find, zoom, print, passwords and autofill, permission prompts, extensions, DevTools, profiles, Widevine build, import from Chrome | Sonnet, high (split into B1 tabs and downloads, B2 passwords and permissions, B3 DRM and extensions) | `electron/main.js`, `preload.js`, `policy.js`, `ui/assets/os/screens/browser/**`, `browser_pane.py`, `os_api/browser.py`, `electron/package.json` | the 60-site parity list, target 95 percent |
| H | Security closeout: A5 per-launch secret, R2B-08 DLP, R2B-11 MCP policy, A9, R2B-07/09 rest, N3, review of the new surfaces | Opus, high (reviewers Sonnet high) | `webui.py` auth parts, `google_auth.py`, `governance.py`, `mcp_bridge.py`, `request_guard.py` | each finding has a failing-then-passing test |
| C | Shared browser control: stable element ids and accessibility snapshot, user/model lock, live Docs typing, Sheets edit, YouTube control tests | Opus, high for the lock; Sonnet high for ids and tests | `browser_agent.py`, injected script files, small `electron/main.js` hooks AFTER B | type a paragraph into a real Doc while the user types elsewhere; no collisions |
| G | Tool skill: tool descriptions with examples, routing, fix top failures from the benchmark | Sonnet, high | `agent_prompts.py`, tool descriptions in `general_roster.py`/`system_access.py` AFTER J | pass rate from baseline to 85 percent on the 40 tasks |
| F2 | App driving UI and Screen Recording flow, replacement screens (calendar, files, notes) | Sonnet, medium | `ui/assets/os/screens/apps/**` and new screens, `os_api/apps.py` UI side | first-run permission walkthrough; drive an app from chat |
| I | Ship: self-contained signed build, auto-update, first-run walkthrough, crash recovery, UX backlog S3 and the 56 lesser items, performance budget | Sonnet medium; restyles Haiku/Sonnet low | `electron/**` build files, `scripts/**`, `ui/setup.html`, `ui/login.html`, css | fresh install on a clean user account passes the launch checklist |
| A | Owner: sign in to Google once in the pane; allow DRM build; first-launch macOS prompts | you | none | Gmail, Drive, YouTube stay logged in after restart |

## 3. Parallel waves (files never overlap inside a wave)

- **Wave 1 (parallel):** J (Sonnet high), D (Sonnet medium), G0 (Haiku), F1 (Opus high).
  Ownership is disjoint: J owns webui, dispatch, roster, system_access; D owns the media screen;
  G0 owns scripts/bench; F1 owns the new app_driver package. F1 does not register its tools; main
  thread wires them into the roster after the wave. Then: one Sonnet-high reviewer, full suite, commit.
- **Wave 2 (parallel):** B1 then B2 then B3 (serial inside B, Sonnet high), H (Opus high) in
  parallel with B. Disjoint: B owns Electron and the browser screen; H owns server security files.
  Reviewer, full suite, commit after each of B1, B2, B3.
- **Wave 3 (parallel):** C (Opus high on the lock) and G (Sonnet high). C owns browser_agent and
  the injected scripts; G owns agent_prompts and tool descriptions. Reviewer, suite, commit.
- **Wave 4 (parallel):** F2 (Sonnet medium) and I (Sonnet medium plus Haiku restyle). Reviewer,
  suite, commit, then the owner acceptance run (the 60 sites and the 40 tasks).
- Between waves the main thread (Sonnet medium) runs the full suite, reviews the risky diffs,
  commits, pushes, refreshes the backup, updates the tracking docs and memory.
- Do not run the full suite while builders write. Builders run only their own test files.
- If usage is tight, run one wave at a time and pick the cheapest phase order: J, D, B1, G0,
  G, B2, H, C, B3, F1, F2, I.

## 4. Why this order

J first because it unblocks the owner's rule (every chat has the browser and player) and the
tool-skill work. D and G0 run beside it because they touch different files. B is the biggest
and most visible; H runs with it because security files and Electron files do not overlap.
C needs B's tabs. G needs J's shared tools. F is isolated, so F1 can start early; F2 waits for
the permission flow. I is last because it packages everything.

## 5. Exit gates (the product is done when)

60-site browser parity at 95 percent; 40-task benchmark at 85 percent with a real cloud key;
every chat box opens the browser and player; Docs typing and YouTube control live-verified;
security findings closed or accepted in writing; clean-account install passes; full suite green.

## 6. Push credentials note

The Mac's active GitHub login can be `fourclaude208-droid`, which cannot push. Push with:
`git -c "credential.helper=" -c "credential.helper=!f() { echo username=adit2011238-glitch; echo password=\$(gh auth token --user adit2011238-glitch); }; f" push`

## 7. Budget

Budget rule: never more than 4 agents running at once (the 5-hour window hit 96 percent with about 8). Estimated weekly cost: Wave 1 about 15 points, Wave 2 about 25, Wave 3 about 20, Wave 4 about 10, plus reviewers and main-thread steps about 25. Total about 95, so plan two weeks: Waves 1 and 2 this week, Waves 3 and 4 after the 10-09 reset. Re-measure after Wave 1 and update these numbers.
