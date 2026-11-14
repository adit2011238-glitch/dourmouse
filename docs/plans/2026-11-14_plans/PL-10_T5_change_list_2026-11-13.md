# PL-10 T5: browser change list for the owner's yes (2026-11-13)

Thread T5 of PL-10. Status: proposed, waiting for the owner's yes. Nothing below is built yet.
Facts about today's code were read on 2026-11-13 from `~/dourmouse-recon` at `cd777c1`.
Work happens in the worktree `youthful-lederberg-3fbc81` (owner-requested exception to Rule 1 for
the six parallel threads) and is merged back into `recon-2026-09-11`.

## Owner decisions so far

- 2026-11-13: option A approved. A real Google Chrome with its own "Dourmouse" profile folder,
  driven over the DevTools protocol. The Electron pane stays for quick signed-out reading. The pane
  is never disguised as Chrome.
- 2026-11-13: Chrome's password saving and autofill stay off in the Dourmouse profile. Dourmouse
  code never types, reads or exports passwords; the owner signs in by hand.
- 2026-11-13: Chrome sync is allowed in the Dourmouse profile. Synced passwords would still fill
  even with saving off, so when turning sync on the owner unticks Passwords (and Addresses and
  Payment methods) in Chrome's sync choices. The engine reports what it can read of that setting
  and says "unknown" when it cannot.
- Open: which Google account goes in the profile. Proposed: personal only; the school account
  stays in its own profile, as T2 advises.
- Open: control channel, see "Decision: port or pipe" below.

## What exists today (read, not assumed)

- `dourmouse/browser_agent.py` (2,850 lines) drives either the Electron pane over CDP
  (`connect_over_cdp`, `DOURMOUSE_ELECTRON_CDP_PORT`) or a Chrome it launches through Playwright
  (`chromium.launch(channel="chrome")`, line 743), which gets a throwaway profile every time.
- Browser tools are registered in `dourmouse/general_roster.py` (browser agent, about lines 5790
  to 6200). Gates live in `dourmouse/dispatch.py`: `_url_gate` (line 3117) refuses odd spellings
  and Dourmouse's own ports and asks before local-network addresses, but only for addresses the
  model types; `click_gate` and `press_gate` (lines 3204 to 3219) ask before submit-like clicks
  and Enter presses. `browser_fill` has no gate.
- The browser agent's prompt tells the model never to sign the owner into Google (finding #160).
  `browser_signin` uses Dourmouse's own credential vault, behind an approval.
- Jobs: the goals engine (`goals.py`, `GoalStore.log_event`). Message bus: `message_bus.py`
  (`post`, `inbox`). Store: `memory_store.py` (`remember(source, title, body)`, `search`).
- Not on trunk yet: T4's library API, T3's Agent Room, T2's link reputation checks (no Safe
  Browsing, punycode or lookalike code exists in `dourmouse/` today).
- Installed Chrome: 157.0.8103.51, so remote control needs a non-default profile folder.

## Change list

### Step 1. Chrome profile engine

New `dourmouse/chrome_profile.py`, tests in `dourmouse/tests/test_chrome_profile.py`.

- Profile folder `~/Library/Application Support/Dourmouse/Chrome Profile`, created private (0700),
  outside the repo and the code backups so Google session cookies never land in a backup. Tests
  use a temporary folder (`DOURMOUSE_CHROME_PROFILE_DIR`).
- Before Chrome starts, the engine writes the profile's preferences: password saving off,
  automatic sign-in off, address and card autofill off, continue where you left off. It re-asserts
  them on each launch.
- Launch: the owner's Google Chrome as its own process, with the profile folder and a random local
  control port. Not through Playwright, whose launch adds automation flags and closes Chrome when
  Dourmouse exits. Chrome survives a Dourmouse restart.
- Attach: read the `DevToolsActivePort` file Chrome writes in the profile folder, check that the
  port really belongs to that Chrome (process id from the profile's lock, `lsof`), then
  `connect_over_cdp`, the same call the pane uses.
- Reattach after a Dourmouse restart: the same path. A stale port file is detected, never trusted.
- Status (a new block in `browser_status` and `/api/browser/status`): installed and version,
  profile folder, running or not, control port, attached or not, which processes are connected to
  the port, the Google account Chrome shows (if any), password saving on or off, sync of passwords
  on, off or unknown, last error in plain words.
- Sign-in: the owner asks in the directive box ("open the Dourmouse browser so I can sign in");
  the engine opens Google's sign-in page in that window with no agent attached, and the owner types
  everything. If Google still objects, the engine restarts that window without remote control for
  the sign-in, then turns control back on.
- Quit: closes that Chrome cleanly, and asks first if the owner has it open.
- Tests: pure tests for preferences, port-file parsing and status wording; live tests that start
  real Chrome headless on a temporary profile, attach, drop the connection, reattach and read the
  status (skipped when Chrome is not installed).

### Step 2. Agent tools against it

Edits in `browser_agent.py`, `dispatch.py` and `general_roster.py`.

- `browser_open` gains `signed_in: true` to open the page in the Dourmouse Chrome instead of the
  pane. The other tools (`browser_snapshot` to read, `browser_extract`, `browser_fill`,
  `browser_click` and the rest) act on whichever page is current, as they do now.
- Gate: on the signed-in Chrome, every fill, type, select, click, key press and submit asks the
  owner first (the existing confirm card, with the page address and the field or button). Reading
  never asks. `browser_signin` and the credential vault tools are refused there: the owner signs in
  by hand.
- Google Docs: the editor draws on a canvas, so reading a Doc uses its plain view (`/mobilebasic`)
  or its text export in the same signed-in session. To verify live.
- Prompt: the browser agent's Google rule changes from "never sign in, use SETTINGS" to "for Gmail,
  Docs and other signed-in Google pages use signed_in: true; never type credentials; if not signed
  in, ask the owner to sign in in the Dourmouse Chrome window".
- Tests: gate decisions per tool and engine, refusal of the sign-in tools, engine selection, Docs
  address handling; a live test against a local test page on the temporary profile.

### Step 3. Save pages and notes to the library

New `dourmouse/browser_memory.py`.

- New tool `browser_save_page(notes, job)`: saves the current page's address, title, readable text
  and the agent's notes, with the job id.
- Writes through T4's `add(source, text, meta)` once T4 has landed. Until then it writes to the
  existing `memory_store` (source `browser_page`), which T4 migrates. No new store.
- Tests: what is saved, the fallback, refusal on Dourmouse's own pages and in Private mode.

### Step 4. Tabs per job and Agent Room posts

- Each job gets its own window in the Dourmouse Chrome; its tabs open there and close with the
  job. Chrome's coloured tab groups are an extension-only API, not reachable over the DevTools
  protocol, so they are not used.
- Saving a page in a job posts a short note (what was found, the address, the library entry) to
  the job's Agent Room thread through T3's API once T3 has landed; until then to the existing
  message bus plus the job's log (`GoalStore.log_event`).
- Tests: a window per job, the tab-to-job mapping, closing with the job, the note's content, the
  fallback.

### Step 5. Link check before any page loads

- Every top-level page load in the Dourmouse Chrome, whether typed by the model, clicked by the
  owner or reached by a redirect, is checked before it loads. T2's check runs once it has landed;
  until then the existing `_url_gate` rules (Dourmouse's own ports refused, odd spellings refused,
  local-network addresses asked) apply to every load, not only typed ones.
- A risky address gets a full-page warning, never a silent block. Its look is T6's; until T6 has
  designed it, a plain text page, shown to the owner first.
- Tests: decisions for typed, clicked and redirected loads; a live test with a local redirect to a
  refused address.

## Decision: port or pipe

- **Port (recommended, matches the plan).** Chrome listens on a random local port. Dourmouse can
  reattach after a restart and the window stays open. Honest risk: while it runs, any program
  running as the owner could connect to that port and drive the signed-in Chrome; Chrome blocks
  this on the everyday profile for that reason. Mitigations: random port, port file inside the
  private profile folder, the status lists every process connected to the port, and T2 can alert
  on unknown ones.
- **Pipe.** Only Dourmouse can drive Chrome (no listener). Chrome closes whenever Dourmouse quits or
  restarts and reopens with its tabs next time; no reattach. Needs Playwright's own launch with its
  automation flags filtered out, which is more fragile, and Google may treat that browser as
  automated (to verify).
- Either way the profile is only as safe as the owner's Mac account: a program running as the
  owner could also start Chrome on this folder itself while it is not running.

## Not changing

- The Electron pane and its sign-in rule, `electron/importers.js` (still reads no cookies or
  passwords), the owner's everyday Chrome profile and its settings.
- No new screens. The only new thing on screen is the Dourmouse Chrome window itself. A status chip
  and the warning page design go to T6.

## Order, tests and records

- Steps in order, tests per step. Full suite (`.venv/bin/python -m pytest dourmouse/tests -q`) and
  `scripts/lint_ratchet.py` green before each commit, counts in the message, run only when no other
  thread is running the suite.
- Per change: a numbered finding in `docs/ENGINEERING_AUDIT.md` and `docs/GODSPEED_ROADMAP.md`
  (next free number at commit time; 193 today), a row in `FIX_LEDGER_2026-10-06.md`, evidence in
  `EVIDENCE/`.
- Live checks on an isolated Dourmouse instance (its own ports and workspace), never the owner's
  running app. The engine uses the real Dourmouse profile only for the acceptance runs, after the
  owner has signed in.
- Merge into `recon-2026-09-11` after rebasing on its latest commit and a green suite. No push
  without the owner's yes.

## Acceptance evidence (from PL-10 T5)

- The owner signs in to Google once in the Dourmouse profile: a screenshot of the status.
- The agent opens Gmail and a Google Doc and reads the Doc on request, typed in the directive box:
  screenshots. They show the owner's inbox, so they stay only in `EVIDENCE/`.
- A research job reads three papers through it, with the notes in the library and the Agent Room:
  screenshots. Honest limit: until T3 and T4 land their screens, the evidence for that part is the
  stored entries and the bus messages, not those screens.
