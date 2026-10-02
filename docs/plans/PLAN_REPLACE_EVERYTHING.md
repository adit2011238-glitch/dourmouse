# Dourmouse: the plan to replace every other app (written 2026-10-02)

End goal (owner): Dourmouse replaces the apps on the Mac. It opens browser tabs and uses them,
uses other apps, plays music, writes documents, uses YouTube, all from one window, and the owner
and the model can both do all of it, at the same time, with the same abilities.

## 1. Where it stands (measured, not guessed)

- 159 numbered engineering findings plus #160 in progress; 259 Python modules, 299 test files,
  about 6,950 tests (last full run 6,942 passed, 0 failed; a new run is in progress).
- One pinned app, `~/Applications/Dourmouse.app` (a re-branded Electron clone of the checkout).
- 18 shell screens: HOME (chat), COMMS (Gmail), RESEARCH, BROWSER, MEDIA, CODE, PROJECTS, WIKI,
  GOALS, TIMETABLE, ORCHESTRATION, AGENTSMITH, VOICE, ATLAS, NEWS, SECURITY, SETTINGS, OFFICE.
- Tools the model already has: about 129 registered, including Google Docs/Sheets/Slides/Drive/
  Calendar/Gmail, Spotify play and control, open any app, app keystrokes, run commands, read and
  write files, a real shared browser (Playwright over CDP into the embedded Chromium pane).
- Real shared browser already works (one Chromium, one cookie jar for owner and model).
- Weekly usage 2% (reset 2026-10-02 07:00 UTC; next 10-09), so budget is not the constraint today.

## 2. What the end goal needs, and how far each part is

| Capability | Today | Gap |
|---|---|---|
| Browse like Chrome | One pane, no tabs; Chrome user agent and persistent profile (#160, uncommitted until suite green); Google sign-in page now loads | tabs, new window, downloads, find, zoom, history, bookmarks, address bar suggestions, password saving, permission prompts, extensions, pop-ups, printing |
| Model uses the browser | snapshot by selector, fill, click, submit | stable element ids, user/model collision lock, Google Docs typing and YouTube control never live-tested |
| Google account in the browser | sign-in page loads in the pane; real sign-in not yet done by a human | owner signs in once, then verify Docs, Drive, Gmail, YouTube logged in; two separate cookie jars today (SETTINGS sign-in vs pane) |
| YouTube | plays as a web page only | DRM (Widevine) build, media keys, tools: search, play, pause, seek, queue, captions |
| Music | Spotify tools exist (control a Spotify app); MEDIA plays local files | Spotify Web Player needs DRM; or YouTube Music; one MUSIC surface with now playing and a queue |
| Writing documents | Google Docs tools (append, insert) via the API | a real editor: either a native Dourmouse doc editor (offline, instant) or Docs in the pane with proven typing; decide |
| Use other apps | open app, send keystrokes, run commands | no seeing the screen of other apps; needs macOS Accessibility and Screen Recording, an accessibility-tree reader, click and type by element, approvals per app |
| Replace mail, calendar, notes, files, terminal, messages | COMMS (Gmail), TIMETABLE, WIKI notes, CODE | calendar UI, files manager, notes, messages are thin or missing |
| Model is good at using all this | owner reports it fumbles tools | tool descriptions, examples, routing, a task benchmark with pass rates |
| Safe by default | approval gate, sandbox, DLP, deny-lists | open: A5 owner-vs-driven-browser, R2B-08, R2B-11, A9, parts of R2B-07/09, N3 |
| Ships as a product | live-checkout app, ad-hoc signed | self-contained build, notarization, auto-update, first-run permissions, crash recovery |

## 3. The plan, in order (each phase ends with something the owner can use)

**Phase A: Finish and prove the Chrome-like pane (1 session).**
1. Commit #160 (UA + partition) after the suite is green. 2. Owner signs into Google in the pane
once (a human does this; the model never does). 3. Live test: Gmail, Docs, Drive, YouTube are
logged in; cookies survive an app restart. 4. Decide cookie-jar policy: SETTINGS sign-in and the
pane stay separate (simple) or SETTINGS drives the pane (one sign-in everywhere). 5. Fix the
pane permission policy (it denies every site permission; Chrome prompts instead).

**Phase B: A real browser (2 sessions).** Tabs (one BrowserView per tab, tab strip in BROWSER),
new window and pop-ups, downloads shelf, find in page, zoom, history, bookmarks, address bar
that searches, reload/stop, saved passwords via the macOS Keychain with a prompt, printing,
extension support where Electron allows, Widevine via the castLabs Electron build (decision
needed: it changes the Electron binary the app clones). Exit test: a day of the owner's normal
browsing in Dourmouse with no need to open Chrome.

**Phase C: Model and owner share the browser properly (2 sessions).** Stable element ids and an
accessibility-tree snapshot; the user/model lock (pause the model for ~800 ms after a real
keystroke, and show who is driving); per-tab model control; live tests: type a paragraph in a
real Google Doc, edit a Sheet cell, play/pause/seek/search on YouTube, fill a real form. Every
test is run against the real pane and recorded with a screenshot.

**Phase D: Music and YouTube as first-class (1 to 2 sessions).** One MUSIC/PLAYER surface: now
playing, queue, play/pause/seek, from YouTube (pane), Spotify (Web Player once DRM works, else
the Spotify app via its tools), and local files (MEDIA). Model tools: play X, queue, pause,
volume, what is playing. Media keys and the macOS now-playing widget.

**Phase E: Writing (1 to 2 sessions).** Decide with the owner: (1) native Dourmouse documents
(fast, offline, exports to Google Docs/Word/PDF) with the model editing the same document live,
or (2) Google Docs in the pane only. Recommendation: native editor plus Google Docs sync, because
Docs is a canvas editor that automation reaches poorly.

**Phase F: Use other apps (3 sessions, the biggest risk).** macOS Accessibility and Screen
Recording permissions with a clear first-run flow; an accessibility-tree reader for any app
(roles, labels, positions); click, type, scroll by element; screenshots when the tree is empty;
per-app allow list and an approval for each new app; a visible indicator while the model drives
an app; kill switch. Replaces the blind `send_app_keystrokes`. Built-in replacements where
cheaper than driving apps: Calendar (TIMETABLE), Files, Notes (WIKI), Messages later.

**Phase G: The model gets good at it (runs alongside B to F).** A benchmark of 40 real owner
tasks ("open my Doc and add a heading", "play X on YouTube", "summarise this tab", "email Y") run
against the live app, measured pass rate per phase; rewrite tool descriptions with examples;
route each task to the right tool group; log every failed tool call and fix the top causes.
This addresses "the model struggles to use the tools since it does not know how to".

**Phase H: Security closeout (1 session, can run any time).** A5 per-launch secret so a driven
browser cannot approve its own actions; R2B-08 DLP gaps and scan outbound arguments; R2B-11 MCP
bridge policy; A9 /mobile pre-login leak; rest of R2B-07, R2B-09, N3. New surfaces (tabs, app
control) get the same review before they ship.

**Phase I: Ship it (2 sessions).** Self-contained build (electron-builder, bundled Python),
Developer ID signing and notarization (needs an Apple Developer account, owner decision), auto
update, first-run permission walkthrough, crash recovery and restart, performance budget
(cold start under 3 s, tab switch under 100 ms), the UX backlog (S3 setup and login restyle and
the 56 open items in UX_ISSUES_2026-09-27.md).

## 4. Order and cost

A, then B and G together, then C, D, E, then F, with H and I last (H earlier if anything new
touches approvals). Roughly 15 to 20 working sessions. A Sonnet builder costs about 1 to 4
weekly points; with the weekly window at 2% a full phase fits in one week. Rule kept: check both
usage windows before launching workers, stop at 95% weekly, full suite green before every commit.

## 5. Decisions needed from the owner

1. Widevine DRM build of Electron (castLabs) for YouTube Premium, Spotify and Netflix: yes/no.
2. Native document editor or Google Docs only (recommendation: native plus sync).
3. One sign-in for SETTINGS and the browser, or two.
4. Apple Developer account for signing and notarization (about 99 USD per year), or stay local.
5. Which other apps the model may drive first (Music? Messages? Terminal? Finder?).

## 6. Honest limits

Chrome itself cannot be embedded; the embedded Chromium is the same engine but not Chrome, so a
few things (Chrome Web Store sync, Google Chrome-only checks) will differ. Google may still
challenge sign-in from a non-Chrome browser; that is Google's choice and a human, not the model,
completes it. Driving arbitrary Mac apps is permission-gated by macOS and will never be perfect.

## Addendum 2026-10-02 (owner decisions, and a gap found by reading the code)

Decisions: (1) the Electron browser replaces Chrome and keeps the Google Chrome ecosystem
(Google account, Gmail, Drive, YouTube, Chrome-style sign-in); (2) Google Docs in the browser
ONLY, no native editor (Phase E becomes: prove typing into Docs, Sheets, Slides in the pane;
no editor to build); (3) a first-class media player and a PDF reader inside Dourmouse; (4) EVERY
screen with a chat box must be able to use the browser and the media player, and open videos,
mp3, PDFs, txt and similar files in it.

Gap found (read, not guessed): that last rule is NOT true today.
- Chat screens: HOME, COMMS, RESEARCH, MEDIA, CODE, NEWS (plus AGENTSMITH's own box).
- RESEARCH (`focusAgent: 'research_info'`), COMMS (`'mail'`) and AGENTSMITH (`'agent_smith'`) pin
  the chat to ONE agent; `webui.py` wraps the prompt with "You may ONLY use the X subagent's
  tools". Those chats cannot open the browser pane or the file preview. CODE routes to the coding
  toolchains, which have no Dourmouse tools at all.
- `open_file_preview` (system_access.py:1220) opens PDF, images, audio, video and converted
  formats only. Plain text, Markdown, CSV, JSON and code files are refused, so "open txt" fails.
- There is no player control tool (play, pause, seek, queue, what is playing); the model can
  only open a file.

Fix (new Phase J, one session, do before B): a "shared desk" tool group (browser tools, open_file_
preview, player controls, read_file) that every focus route may use in addition to its own agent;
the routing directive says so; CODE gets the group through its MCP bridge. Add a text/Markdown/
CSV/JSON/code viewer to the pane (read only, syntax highlighted) so txt opens too. Exit test:
from each of the six chat boxes, ask "open this PDF", "play this mp3", "open this txt", "open
google.com in the browser" and see it happen; one test per screen.

## Addendum 2026-10-02 (b): the browser bar is "everything Chrome does", not just Google sites

Owner correction: the browser must do everything Chrome does, on every website. Google services
are just sites. So Phase B is a Chrome PARITY list, tested site by site, not a feature sample.

Parity checklist (each item gets a live test; status: have / build / limit):
- Tabs, tab strip, drag to reorder, pin, duplicate, mute, close/reopen closed tab, tab search,
  new window, incognito window (build)
- Address bar: search, suggestions, history completion, site info and permissions, shortcuts (build)
- Bookmarks and bookmark bar, history page, downloads page and shelf, reading list (build)
- Passwords and autofill: Keychain-backed save and fill, addresses, cards with a prompt (build)
- Permissions like Chrome: ask for camera, mic, location, notifications, clipboard, per site, saved
  (the pane denies all today: build)
- Extensions: Electron loads unpacked Chromium extensions with partial API support; Chrome Web
  Store install is not available to non-Chrome browsers (limit; offer "load unpacked" and a
  curated set: uBlock-class blocker, password manager, React/dev tools) (build, partial)
- DevTools, view source, print and save as PDF, find in page, zoom, full screen, picture in
  picture, translate, spellcheck, reader view (have some, build rest)
- Media and DRM: YouTube, Netflix, Spotify Web, Widevine (castLabs build), media keys, casting
  (build, DRM decision approved by owner: Chrome parity)
- Web platform: WebGL, WebGPU, WebRTC calls (Meet, Zoom web), WebAuthn passkeys and security keys,
  file system access, service workers, notifications, PWAs "install as app" (build and test)
- Sign-in and sync: Google account sign-in works as a normal site login; Chrome Sync itself
  (bookmarks, passwords across devices) is a Google-only feature for real Chrome (limit): replace
  with import from Chrome (bookmarks, history, passwords, with consent) and optional export.
- Profiles: multiple profiles, each its own persistent partition (build)
- Safe Browsing, HTTPS-only mode, cookie and tracking controls, clear data (build)
- Cast, QR share, tab groups, side panel, search by image (nice to have, last)

What cannot be identical, said plainly: it is Chromium, not Google Chrome. Chrome Web Store
installs, Chrome Sync and Google's own Chrome-only checks differ. Everything a normal website can
do, it can do. Google may still ask extra verification at sign-in; the human completes it.

Test method: a fixed list of 60 real sites and tasks (Gmail, Drive, Docs, Sheets, YouTube, Meet,
Maps, Photos, GitHub, banking login, Netflix, Spotify, Figma, Notion, WhatsApp Web, Discord,
Reddit, X, Amazon checkout form, PDF in tab, file upload, download, drag and drop, WebRTC test,
WebGL test, passkey test), run in the pane, pass or fail recorded per site. Target 95 percent
before the owner stops opening Chrome.
