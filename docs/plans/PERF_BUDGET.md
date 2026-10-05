# DOURMOUSE speed budget (phase I2)

Measured 2026-10-05 on the owner's Mac (Apple silicon, macOS 26, Python 3.14, Electron 44) with
`scripts/perf_check.py`, against an ISOLATED copy of `~/Applications/Dourmouse.app` (its own ports 18890,
19390, 19391, a temporary workspace, config folder, data folder and app name). The owner's app and data were
not running and not touched. Re-measure with:

    cd ~/dourmouse-recon && .venv/bin/python scripts/perf_check.py            # about 100 s, prints the table
    .venv/bin/python scripts/perf_check.py --idle 10 --json --out run.json    # quicker, machine readable
    .venv/bin/python scripts/perf_check.py --check                            # exit 1 when a number is over budget
    .venv/bin/python scripts/perf_check.py --app /Applications/Dourmouse.app  # a packaged build

It refuses 8765, 9333, 9334 and any port that already has a listener, and stops only the processes it started
(the tree under the isolated copy's own main process, by process id, never `pkill`).

## Numbers (after the map window fix below)

Three runs on an otherwise idle Mac (run 0 used a 20 s idle period, runs 1 to 3 the 60 s the budget names; runs 0 to 2
had the map window start on `about:blank`, run 3 is the final code with a one line `data:` placeholder page, see the
fix below).

| Measure | Run 0 | Run 1 | Run 2 | Run 3 | Budget |
| --- | --- | --- | --- | --- | --- |
| App launch to the app asking for its server (`app_boot_s`) | 0.74 s | 0.39 s | 0.42 s | 0.61 s | not budgeted |
| Server start to answering (`spawn_to_ready_s`) | 1.05 s | 1.07 s | 1.07 s | 0.87 s | not budgeted |
| **Server ready** (launch to `GET /api/os/ping` = 200) | 1.79 s | 1.46 s | 1.49 s | 1.48 s | **4 s** |
| **Cold start** (launch to HOME shown with its data) | 12.95 s | 12.44 s | 7.72 s | 13.9 s | **20 s** |
| HOME, return visit (cold / warm) | 27 / 45 ms | 205 / 71 ms | 100 / 483 ms | 56 / 56 ms | 3.5 s / 1 s |
| BROWSER, first / second visit | 2930 / 59 ms | 1057 / 172 ms | 938 / 337 ms | 866 / 419 ms | 3.5 s / 1 s |
| MEDIA, first / second visit | 589 / 32 ms | 622 / 207 ms | 1260 / 439 ms | 358 / 47 ms | 3.5 s / 1 s |
| SETTINGS, first / second visit | 716 / 307 ms | 368 / 168 ms | 588 / 529 ms | 358 / 248 ms | 3.5 s / 1 s |
| **Server memory** after 1 minute idle (resident) | 358 MB (20 s) | 404 MB | 386 MB | 352 MB | **700 MB** |
| **App memory** after 1 minute idle (8 processes: the main process, Chromium's helpers and renderers, the shell's Python helpers; the server is counted separately) | 785 MB (20 s) | 902 MB | 895 MB | 839 MB | **1300 MB** |

How a screen switch is timed: in the console page, the URL hash is set (`#/browser`) and the clock stops when the
screen's root exists in `#body` and its `data-state` is no longer `loading` (a screen with no state counts once its
root exists). A first visit includes the import of that screen's module. "HOME, return visit" is a return to HOME after
the other three, because HOME is already showing at launch (its cold start is the cold start line above).

How the cold start is timed: from the moment `open` is called until HOME's root is mounted and no longer `loading`.

Budget method: the budget is the measured worst value with 50 to 100 percent of headroom, because the cold start swung
from 7.7 s to 13.9 s between runs of the same build on an idle Mac (the server's own start-up burst, below). Tighten it
when the numbers have been stable across several runs.

## What the numbers do not include

- The isolated copy has an empty workspace and no memory store, so it is a floor. The owner's real data (history
  sync facts, sessions, schedules, goals) adds start-up and memory.
- A Playwright (DevTools) client is attached while the screens are timed; it adds a little to every page.
- No model backend is running (`DOURMOUSE_LLM_BACKEND=ollama` only so that `/` opens the shell instead of the setup
  wizard), so nothing here measures a reply.
- One Mac, one run of each kind. Treat differences under about 30 percent as noise.

## The worst offender, found and fixed (finding #171)

Before the fix the cold start was not a number but a hang: in two runs with the page being timed, HOME never became ready
inside 60 s, and in an unattended run the console's first API calls (`/api/os/screens`, `/api/connections`,
`/api/auth/status`) took 13 to 35 seconds to be answered although the server answers each of them in 10 to 600 ms when
asked alone from a script (checked: a standalone server answers all ten first-load calls in parallel in 0.5 to 1.4 s).
The cause was not the server. The hidden "AGENT ORCHESTRATION MAP" window, created at every launch with `show: false`
and loading `/map`, is still treated by Chromium as visible (`document.visibilityState` was `visible` inside it); the map
page runs about 90 CSS animations and polls the server every second. DevTools performance metrics showed that one page
using over 100 percent of a CPU core all the time (about 35 percent of it layout), its renderer sat at 71 to 110 percent
CPU for the 40 seconds sampled, and the GPU process at 40 to 50 percent. Nothing in the shell ever shows that window (the
IPC that would, `bridge:open_map`, is not exposed by `preload.js`; `ui/index.html` calls it only if present).

Fix (`electron/main.js`, outside the supervision section, flagged in the finding): the window is still created hidden but
now starts on a one line `data:` placeholder page, and `/map` loads only the first time the window is shown (`loadMapOnce`, used by
`bridge:open_map` and by the smoke test). Result on the same Mac and the same build: HOME ready in 8 to 14 s instead of
not within 60 s, no renderer above 5 percent CPU after the first 12 seconds, app memory 785 to 902 MB instead of 1095 to
1107 MB. An attempt with no initial page at all made `connect_over_cdp` (the browser agent's own attach) hang on the
page-less window for its whole timeout; that is why a placeholder page is there, and a test pins it. It is a `data:` page
rather than `about:blank` on purpose: `browser_agent.py` treats any blank page as the pane's first tab (`_blank_url`),
so a second blank page would be mistaken for it.

## Remaining offenders, in order, not fixed

1. **Cold start of 8 to 14 s.** The server answers in 1.5 s; the rest is the first ten seconds of CPU contention. Sampled
   at one second intervals during that period the server runs about 50 threads (7 live pollers, the news stream, the
   GDELT graph poller parsing a zip, the standing librarian hashing files, the world pulse warmer, the daily report,
   four "vision probe" threads importing pywebview, pystray, sounddevice and pyobjc) and the desktop shell starts a Python
   helper process for the overlay, wake word and tray that peaks at about 76 percent CPU at the ten second mark. Candidate
   fix: start the pollers and the helpers after the first screen is ready instead of with the server (needs the owner's
   decision on which loops may wait).
2. **BROWSER first visit, 0.9 to 2.9 s.** The first import of the screen's modules plus the pane's first bounds and tab
   list. A prefetch of the screen modules while HOME is idle would hide it.
3. **App memory 900 MB in 8 processes**, server 400 MB. Mostly Chromium (renderers for the console, the hidden ATLAS window and the
   service worker, plus the GPU and network processes) and the shell's Python helper processes. The ATLAS window is created at launch and
   loaded although hidden; it measured 0.2 percent CPU, so it costs memory, not time. Candidate: the same lazy load as
   the map window.
4. **Import of `dourmouse.webui`** is 0.5 to 0.7 s warm (`python -X importtime`: `openai` 0.44 s of it) and does no
   network work (see finding #171, import-time part). The 437 s observed on 2026-10-05 was not reproduced and is not
   explained by this repository's code.
