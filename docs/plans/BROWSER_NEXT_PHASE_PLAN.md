# The embedded browser: where it stands, and the plan for weekly reset (2026-10-02)

Owner question: "could we use a local model that embeds the user's real browser itself" plus
a pasted architecture note (CEF/WebView2, CDP synthetic input, AOM snapshot, dual-control
locking, debounced updates). Written at 99% weekly usage, read-only, no code changed.

## The good news: most of the hard architecture already exists

This is not a from-scratch build. Read just now, grounded in the real files:

- `electron/main.js` already embeds a real Chromium (a `BrowserView`) inside the app window
  (`pane:show`/`pane:hide`, `electron/preload.js`'s `window.dourmouseShell.pane.*`). That IS
  the "CEF/WebView2" layer the pasted note asks for -- it is Electron's own Chromium, not a
  second browser.
- `dourmouse/browser_agent.py` connects Playwright to that SAME pane over the Chrome DevTools
  Protocol (`chromium.connect_over_cdp`, `DOURMOUSE_ELECTRON_CDP_PORT`) when the Electron shell
  is running. The model and the user are already looking at and acting on the SAME browser
  instance and the SAME cookies/session -- not two browsers kept in sync, one browser shared.
  So "could we embed the user's real browser" -- yes, already done; the open work is making the
  SHARING good, not building the embedding.
- A no-local-model policy is already standing (`dourmouse_model_policy.md`): large cloud models
  only. Nothing here needs a local model. The pasted note's "local LLM" framing does not apply;
  the DOM-reading and input-injection pieces below are plain engineering, not a model at all.

## The real gaps (what to actually build next)

1. **No AOM/stable node-id layer.** `browser_snapshot` (`browser_agent.py:495`, `_page_summary`)
   lists interactive elements by CSS selector and text, re-matched fresh on every call via
   Playwright locators. The pasted note's `data-agent-id` injection (one stable id per element,
   read back instead of re-querying) would be faster and far less likely to grab the wrong
   element on a page that re-renders. Size: M. Owner: a new `browser_agent.py` snapshot path,
   its own test file.
2. **No concurrency lock between the user's own clicks and the model's actions.** `_GLOBAL_LOCK`
   serializes the model's OWN calls against each other; nothing stops the model from typing into
   a field the instant the user starts typing into it, or vice versa. The pasted note's
   "micro-lock on the focused node" does not exist. Size: M. Needs a small Electron-side signal
   (last real user input time per node, or simply: pause agent input for ~800ms after a real
   keydown the agent did not send) plus a test that proves it.
3. **The Google/OAuth block is contained, not solved.** Finding #160 (committed) stops the model
   from ATTEMPTING a Google sign-in through the pane and points the owner at SETTINGS instead.
   That is the honest fix (Google really does refuse automated Chrome for sign-in, this is not
   fixable by disguising the browser). What is still open: once the owner signs in once via
   SETTINGS (a different, cookie-isolated flow today -- `google_auth.py`'s session, not the
   pane's own Chromium profile), does the PANE'S browser also end up signed into Google for
   ordinary browsing (Docs, Drive, YouTube comments), or are they two separate cookie jars? This
   was never checked. If they are separate, signing in once in SETTINGS still leaves the pane
   signed out for Docs/Drive, and the owner would have to also sign into Google manually inside
   the pane itself (which works fine for ordinary sign-in, only the automated/OAuth-popup path
   is blocked) -- and the plan should make that difference clear to the owner in the UI, not
   leave them guessing why Docs doesn't recognise them.
4. **Not proven for Docs or YouTube specifically.** The pasted note's two examples (type into a
   real Google Doc like a person, drive a YouTube video) were never live-tested end to end in
   this codebase. `browser_fill`/`browser_fill_form` drive ordinary HTML forms; a Google Doc is
   a canvas-rendered contenteditable surface with its own keyboard event model, which plain
   `.fill()` may not reach at all -- needs a real live test before claiming it works, not an
   assumption from the architecture note.
5. **No debounce.** The note's "serialize the DOM tree every 100-200ms, not every mutation" has
   no equivalent here, because snapshot is already pull-based (the model asks, nothing pushes
   continuously) -- lower priority than items 1-4, revisit only if push-based updates are added.

## Proposed phases for next session (in order; check get_usage before each)

- **P1 (S):** live-test Google Docs typing and YouTube control through the real pane, as the
  owner actually described them, before building anything new. Write down exactly what breaks.
- **P2 (M):** stable node-id snapshot layer (`data-agent-id`), replacing re-matched CSS/text
  locators for repeat actions on the same page.
- **P3 (M):** the user/model concurrency lock, with a live-reproduced test (type as the "user"
  via CDP while the agent is mid-action; last writer must not stomp the other silently).
- **P4 (S):** resolve the cookie-jar question in item 3 and make the owner-facing wording honest
  about which sign-in covers which surface.
- **P5 (S):** a short note in `UX_ISSUES_2026-09-27.md` / a new finding for whatever P1 finds.

Not planned: a stealth/anti-detection layer to fool Google's automation check. That is explicitly
out of scope (dual-use, against Google's terms, and the honest #160 fix already covers it).

## Addendum 2026-10-02: "why not embed the user's real Chrome?"

Short answer: Google Chrome (the app) cannot be embedded in another app's window. macOS has no
supported way to reparent another app's window, and Chrome ships no embedding API. What CAN be
embedded is Chromium, the engine Chrome is built on, and that is exactly what Electron already
is. Same rendering, same JavaScript, same web platform.

Three options, with the real trade-offs:

- **A. Drive the real Chrome over CDP.** Launch the owner's Chrome with `--remote-debugging-port`
  and a DEDICATED `--user-data-dir` (Chrome 136+ refuses remote debugging on the default
  profile, so the owner's everyday profile cannot be driven). Result: a separate Chrome window
  the model controls. Not embedded, does not replace Chrome, and needs a second profile.
- **B. Make the embedded Chromium behave like Chrome (recommended).** Persistent profile
  (partition kept across launches), tabs, downloads, find, zoom, permissions prompts, real
  extensions where Electron supports them, Widevine DRM (needs the castLabs Electron build for
  YouTube Premium or Netflix style playback), optional one-time import of logins (needs the
  owner's explicit consent; Chrome's cookie store is Keychain-encrypted). The pane then really
  replaces Chrome for daily use, and the model shares the very same instance.
- **C. Hybrid.** B as the daily browser, A as an escape hatch for the few sites that insist on
  genuine Chrome.

Open question that decides how far B can go: Google blocks sign-in inside embedded browsers (the
"content blocked" the owner saw). Normalising the User-Agent to a plain Chrome string is common
and often enough for ordinary sites, but Google's own policy forbids embedded webviews for OAuth
and they detect more than the UA, so it may stay fragile. Needs an owner decision and a live
test (P1), not an assumption.
