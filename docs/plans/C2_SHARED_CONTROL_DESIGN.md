# C2: the owner and the model share one browser without fighting (design, 2026-10-05)

Phase C2 of the execution plan. Builds on C1 (finding #165: stable element ids, the agent follows
the tab the owner is viewing). Code: `electron/main.js` (control state, input detection, bridge
`/control*`, console IPC), `electron/preload.js` (`pane.control`), `dourmouse/browser_agent.py`
(the agent side), `ui/assets/os/screens/browser/control-ui.js` (indicator and buttons),
`dourmouse/browser_scripts/media_control.js` (video control).

## 1. The problem

The pane is ONE Chromium shared by the owner and the model (the agent attaches to the very same
tabs over CDP). Before C2 nothing coordinated them:

- `_GLOBAL_LOCK` in `browser_agent.py` only serialises the model's own calls against each other.
- The model could start typing into a field the instant the owner started typing into it, so the
  two streams of characters interleave in one field, and neither side is told.
- The model could navigate (`browser_open`, `browser_back`) the page out from under the owner.
- The owner had no way to see that the model is acting, and no way to stop it.
- `browser_type` sent one CDP key event per character. Editors that do not take a value (Google
  Docs, a canvas editor fed by a hidden contenteditable iframe) need real text input in the
  focused frame; key events work there but are slow and racy.

## 2. Facts measured before designing (Electron 44.3.0, a standalone experiment, not the app)

| Input | `before-input-event` | `input-event` |
|---|---|---|
| CDP `Input.dispatchKeyEvent` (Playwright `keyboard.press`) | **not fired** | `keyDown`, `keyUp` |
| CDP `Input.dispatchMouseEvent` (Playwright `mouse.click`) | n/a | `mouseMove`, `mouseDown`, `mouseUp` (same fields as real ones) |
| CDP mouse wheel | n/a | `mouseWheel`, `gestureScroll*` |
| CDP `Input.insertText` / main `webContents.insertText` | not fired | **not fired** |
| main `webContents.sendInputEvent` key (the OS keyboard path) | fired (`keyDown`) | `rawKeyDown` |
| real mouse moving over the window (the owner's own hand, observed by accident) | n/a | `mouseMove` with fractional coordinates |

Also measured: `webContents.insertText` from the main process lands in the focused element of the
focused frame, including a contenteditable inside an iframe (the page's `beforeinput` saw
`insertText`), keeps `\n` in a textarea, turns `\n` into a paragraph in a contenteditable, and
never produces a key event, so a line break inserted this way cannot press Enter or submit.

So: real keyboard input is recognisable on its own (`before-input-event` keyDown, which CDP key
events never raise and which a page script cannot raise). Mouse input is not: a CDP click and a
real click look the same. The mouse is therefore told apart by bookkeeping (section 4).

## 3. The states

Per pane tab, kept in the Electron main process (the only place that sees real input):

- **idle**: nobody is acting.
- **owner-active**: the owner pressed a key, pressed a mouse button, scrolled or touched in that
  tab within the last `OWNER_HOLD_MS` (2500 ms). Mouse movement alone does not count.
- **model-acting**: an agent action holds a claim (an "action") on that tab.
- **model-waiting**: the agent asked to act on a tab where the owner is active and is waiting
  (at most 3 s) for the owner to pause.
- **owner-control** (global): the owner pressed Take control. No agent action starts on any tab
  until the owner presses Let the model act.

The console sees the state of the whole pane: owner-control, else model-acting, else
model-waiting, else owner-active, else idle.

## 4. Telling the owner's input from the agent's

- **Keys**: `before-input-event` with `type === "keyDown"` on a pane tab is always the owner. The
  agent never sends key events through `sendInputEvent`, and CDP key events do not raise it.
- **Pointer** (`mouseDown`, `mouseWheel`, `touchStart`, `gestureTapDown`, `gesturePinchBegin`
  from `input-event`): the owner's, unless the agent has an open **pointer window** on that tab.
  The agent opens one (`POST /control/pointer {on:true, x, y}`) immediately before it dispatches
  a CDP click, and closes it right after (150 ms grace). When the agent names the point, only a
  `mouseDown` within 12 px of it (in either CSS or zoomed pixels) is taken as the agent's. A
  window closes by itself after 3 s, so a crashed agent cannot hide the owner's clicks for long.
- **Text**: the agent's text goes in through `POST /control/type`, which the main process applies
  with `webContents.insertText` only if the action is still valid. No event is raised, so there is
  nothing to misattribute.
- Anything not declared by the agent counts as the owner. That includes another local CDP client
  (the live tests use this to stand in for the owner's mouse). The error is always on the
  owner's side: the model backs off, it never pushes through.

## 5. Who wins

1. **An agent action never starts while the owner is active on the same tab.** `POST
   /control/begin {tab, tool}` is an atomic check-and-claim in the main process. Refused with
   `owner-active` and how long until the owner's hold lapses; the agent retries until 3 s have
   passed, then returns: "OWNER IS USING THIS TAB ... Nothing was done. Try again in a moment, or
   ask the owner."
2. **The owner interrupts the model by just using the tab.** A key, click or scroll on the tab the
   model is acting on marks the action interrupted at once (synchronously, in the main process
   event loop). Every later step of that action is refused by the main process itself:
   `/control/type` will not insert another chunk, `/control/check` answers interrupted. The agent
   stops between steps and reports exactly what it did and did not do (characters typed and not
   typed, fields filled and not filled).
3. **The same field is never written by both.** Text is inserted chunk by chunk (at most 24
   characters) by the main process, which refuses a chunk once the owner's key or click has been
   seen. Because both the owner's keystroke and the agent's chunk pass through the one main
   process event loop, there is no gap between "checked" and "inserted": the owner's characters
   can only come after the agent's last accepted chunk, never between two of them.
4. **Stop** (console button) cancels every action in flight: the next step is refused, and a
   navigation the action started is stopped. The model is told the owner pressed Stop and not to
   retry without asking.
5. **Take control** (console button) is Stop plus a hold: no agent action starts on any tab until
   Let the model act. Neither the bridge nor the agent can release it (no bridge route exists for
   take, release or stop; the console IPC checks that the sender is the console's top frame).
6. **Background tabs**: the lock is per tab. Owner input on tab B does not interrupt an action on
   tab A. The agent always starts on the tab the owner is viewing (C1); if the owner switches tab
   mid-action, the action finishes on its own tab, since `insertText` goes to that tab's own
   webContents (verified live before relying on it, see the finding).
7. Read-only tools (snapshot, extract, screenshot, wait, media status) take no claim. Tools that
   change the page do: open, back, click, fill, fill_form, select, type, press, submit, signin,
   media play, pause, seek, mute, volume.

## 6. What the owner sees

A one-line bar under the toolbar of the BROWSER screen, always present in the Electron shell:

- idle or owner-active: "You have control." and a **Take control** button (it pauses the model).
- model-acting: "Model is acting in tab 2: typing." with **Stop** and **Take control**.
- model-waiting: "The model is waiting for you to pause (tab 2)." with **Take control**.
- owner-control: "You have control. The model is paused until you let it act." with **Let the
  model act**.
- After an interruption or Stop, a short note says what happened.

The bridge `GET /control` returns state and counts only (no tab title, no address, no text).

## 7. Failure modes and what happens

| Failure | Behaviour |
|---|---|
| The agent process dies mid-action | the action's lease lapses 60 s after its last contact; the bar returns to idle. Stop clears it at once. |
| A pointer window is never closed | it closes itself after 3 s. |
| An older shell without `/control` (404) | the agent acts without the lock and says so once in a NOTE line. |
| `/control` fails otherwise | fail closed: the action is refused with the reason. |
| The tab is closed mid-action | `/control/type` refuses with `no-tab`; the agent reports what was typed. |
| The owner types with an input method (dictation, emoji picker, IME) | not a key event; not detected. Recorded as a limit. |
| The owner clicks the exact pixel the agent is clicking during the ~50 ms pointer window | taken as the agent's. Recorded as a limit. |
| A local process with the CDP or bridge port | can act as the agent and can call the console IPC through the console page; same trust as before C2 (finding #162 limit). |
| Headless Chrome (no Electron) | no owner exists; no lock, the text is inserted with CDP `Input.insertText`. |

## 8. Editors and Google Docs

`browser_type` gets two modes. `text` (default) inserts text the way an input method does
(`insertText`: chunked through the main process in the pane, CDP `Input.insertText` headless).
It reaches whatever has focus, including a contenteditable inside a hidden iframe (the shape of
Google Docs' `docs-texteventtarget-iframe`), and a line break never presses Enter. `keys` sends
one real key event per character for widgets that only listen to key presses (the old
behaviour), checking the owner between chunks. Line breaks: text mode allows them in a textarea,
a contenteditable, a multi-line textbox or a focused frame, and refuses them in a one-line input;
keys mode only in a textarea. Real Google Docs needs the owner's sign-in, so it is tested by the
owner with `scripts/live_checks/docs_and_youtube.md` and its helper, never by an agent.

## 9. YouTube and media

`browser_media` acts on the page's main `<video>` or `<audio>` (YouTube's
`video.html5-main-video` first, else the largest visible one) from the agent's isolated world:
status (title, current time, duration, paused, muted, volume, ad showing), play, pause, seek,
mute, unmute, volume. No YouTube API, no key. The agent's speed filter that aborted `media`
requests is no longer installed on the pane's context: it was blocking the owner's own audio and
video in the shared browser once the agent attached.
