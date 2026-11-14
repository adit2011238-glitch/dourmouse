# PL-04: A global Ask panel (hotkey from any app) and quick capture

Status: planned. Sources: Raycast and the macOS launcher habit; Gemini's side panel; ChatGPT desktop's companion window. Verified: there is **no global shortcut anywhere in `electron/`** (grep `globalShortcut` returns nothing); Command K exists only inside the window.

## Goal
Press one key anywhere on the Mac, a small panel appears over whatever you are doing, you type or speak a question or a thought, you get a streaming answer (or the thought is saved), and the panel goes away. One more key sends it to the full app.

## Design
- **Hotkey:** default Option+Space (setting; refuse combinations the system already owns; show the conflict). Registered with Electron's `globalShortcut` in the main process. No Accessibility permission needed for this.
- **Panel:** a small frameless always-on-top window (480 by 140, grows with the answer to a maximum), centred near the top, dark theme, closes on Escape or focus loss (setting), never steals focus from the app you came from unless you start typing. It is its own `BrowserWindow` with the same locked-down preload as other windows and a new minimal page `ui/ask.html` using the shell kit (not a copy of the shell).
- **Modes (first word decides, or Tab to switch):**
  - **Ask** (default): goes through the normal chat path in a dedicated thread "Ask panel" with the normal privacy gate and approvals (an approval card shows in the panel; high-risk ones say "open the app to approve").
  - **Note** (`n `): saved as a dated note with the existing `write_note` tool (`general_roster.py`, workspace notes folder); shows "saved". WIKI is a read-only view of file summaries today, not a notes store, so a Notes view is a follow-up (see PL-08).
  - **Remind** (`r `): creates a one-shot automation (needs PL-02; until then it creates a goal).
  - **Do** (`/`): a skill from PL-05.
- **Context picker (opt-in, off by default):** a small chip "use selected text" and "use clipboard"; screen content only with the screen-capture permission and a visible indicator (PL-11). Selected text is read through the clipboard with a short, restored paste; never read silently.
- **Send to app:** Cmd+Enter opens the full window on HOME with the exchange continued.
- **Voice:** a microphone button and a push-to-talk key inside the panel; uses the voice stack of PL-09 once built.

## Work breakdown
1. `electron/main.js`: `globalShortcut` registration with conflict handling, a `createAskWindow()` factory, show/hide/position logic, setting read through the existing prefs path. New `electron/ask.js` (keep main.js from growing).
2. `electron/preload.js`: expose a narrow `ask` channel (`ask:send`, `ask:close`, `ask:open-main`) with sender validation (the SEC4-6 lesson: validate the sender frame URL on every handler).
3. `ui/ask.html` and `ui/assets/os/ask/*.js`: input, streaming answer, mode chip, approval card, keyboard handling; uses `core/api.js` for `/api/chat` with a fixed `screen: "ASK"` and `tab_id: "ask-panel"`.
4. Server: add `ASK` to the screen names the chat route accepts (thread kept separate), `NOTE` handler in `os_api` calling the same function as `write_note`.
5. SETTINGS: hotkey, close-on-blur, default mode, "keep conversation for N minutes".
6. Registry/test updates: `test_os_core` screen lists unaffected (this is not a shell screen).

## Tests
Node tests for the mode parser and key handling; Python tests for the ASK screen thread isolation and the note handler; Electron-level test using the existing shell test harness (policy tests in `test_electron_hardening.py`): the window has context isolation, no node integration, a fixed page, navigation locked; hotkey registration is attempted and a conflict is reported (mock `globalShortcut`). Live: press the key in another app, type a question, see the answer (screenshot), press Escape, focus returns to the previous app.

## Security and privacy
The panel is a second entry to a model that can act; it gets the **same gates** and no new tools. It must not become a way around the approval rule: approvals needing the full card open the app. The hotkey is a global keyboard hook only in the sense of Electron's registered shortcut, not a key logger; no other keystrokes are seen. Clipboard and selection reads are explicit, per-use, and shown in the panel as chips before sending; they pass through the privacy gate.

## Risks
Focus handling on macOS Spaces and full-screen apps is fiddly (use `setVisibleOnAllWorkspaces` and test with a full-screen app). The default hotkey may clash with Spotlight or input-source switching; ship a first-run check.

## Effort and dependencies
About 2 sessions. Optional dependencies: PL-02 (Remind), PL-05 (Do), PL-09 (voice). Works without them.

## Done when
From Finder, Safari and a full-screen app: hotkey opens the panel in under 200 ms, a question streams an answer, a note is saved, Escape returns focus; all gates behave as in HOME; hardening tests green.
