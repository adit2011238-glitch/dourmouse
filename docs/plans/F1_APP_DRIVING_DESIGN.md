# F1: App driving core, design note

Written 2026-10-02, before the code. Phase F1 of EXECUTION_PLAN.md: the core that lets the
model drive other Mac apps, with explicit permission, safely. Code lives in the new package
`dourmouse/app_driver/` and the new router `dourmouse/os_api/apps.py`. Nothing existing is
edited; the main thread wires the tools in.

## What already exists and is reused

| Need | Existing mechanism reused |
|---|---|
| macOS AX access, honest "not trusted" errors | `app_control_ax.py`: `ax_trusted()`, `_AX_ERROR_MESSAGES`, the PyObjC import pattern, key code and modifier tables |
| Confirmation before an action | `dispatch.Permission.REQUIRES_CONFIRMATION` + `confirm_prompt` on a `ToolSpec` |
| Audit | `execution_policy.record()`, which the server already sinks into the office event log (`webui.py` wires `set_action_sink(office_log.append_event)`). Arguments are logged as names plus a hash, never raw, so typed text never lands in the log |
| Secret detection | `governance.DlpFilter().redact()` (shape patterns, secret assignments, and exact values from the owner's own .env) |
| Dry run | `config.app_control_dry_run_enabled()` plus a per-call `dry_run` argument, same as `general_roster._app_control_dry_run` |
| Config location | `config.user_config_dir()` (isolated in tests by the conftest fixture) |
| HTTP routes | `os_api.route` plug-in router; modules are auto-discovered, so `apps.py` mounts itself |

## Threat model

Assets: everything the owner can see or do in any Mac app (mail, messages, banking sites in a
browser, password managers, shells), and the integrity of Dourmouse's own safety settings.

Adversaries and failure modes, most likely first:

1. **Prompt injection.** Text inside a driven app, a web page or a document tells the model to
   type a command into Terminal, open System Settings, or paste a password somewhere. Defence:
   the hard deny list (no shell, no settings, no password stores, no code runners, no
   Dourmouse), the owner-only allow list, a human confirmation for every act, and the secret
   check on typed text.
2. **Wrong target.** The UI changed between the snapshot the model reasoned about and the
   moment of the click (a dialog appeared, a list re-sorted, the app relaunched). Defence:
   every act re-reads the tree and refuses if the target or its neighbourhood changed, if the
   pid changed, or if the snapshot is older than its time limit.
3. **Typing into the wrong app.** The owner switches apps mid-type. Defence: keystrokes are
   posted to the target pid (`CGEventPostToPid`), the app must be frontmost immediately before
   each chunk, and typing is chunked with the kill switch and frontmost check between chunks.
4. **Secret exfiltration or entry.** The model types a password or API key it has seen.
   Defence: DLP on the text, a password-shape heuristic, refusal to type into secure text
   fields or fields labelled password, passcode or PIN, and secure field values are never read.
5. **Self-escalation.** The model tries to allow an app for itself, or to resume after a kill.
   Defence: the model tools have no allow, deny or resume. Those exist only as HTTP routes for
   the owner's own click. The auto-approve toggle skips confirmation cards but never touches
   the allow list.
6. **Runaway driving.** Defence: a global kill switch (module flag plus a file flag that any
   process, or the owner with `touch`, can set) checked before every action and between typed
   chunks, plus the existing per-run limit on approval-gated actions.

Out of scope for F1 and named honestly: screenshots when the tree is empty (Phase F2); a
per-launch secret proving an HTTP request came from the owner's UI and not from a page the
model drives (security item A5, still open, so `POST /api/os/apps/allow` is protected only by
the existing auth gate and request guard); a visible on-screen overlay (F1 provides the state
and a listener hook; the UI renders it).

## Permission model

Three layers, all must pass, checked in this order on every act:

1. **Kill switch** off (memory flag and file flag `<config>/app_driver/KILL`).
2. **Hard deny list**, matched on bundle id and name: Terminal, iTerm and iTerm2, System
   Settings and System Preferences, Keychain Access, Passwords, 1Password, Bitwarden, Dashlane,
   LastPass, KeePassXC, Enpass, Proton Pass, Script Editor, Automator, Shortcuts, SecurityAgent
   (the system password prompt), loginwindow, and Dourmouse itself (bundle id
   `com.dourmouse.app`, name Dourmouse, the dev-mode Electron shell, and this process's own pid
   and parent pid). Script Editor, Automator and Shortcuts are on it because each runs
   arbitrary code, which makes it a shell by another name. A deny-listed app cannot be added to
   the allow list, and if one is ever written into the file by hand it is still refused.
3. **Allow list**, `<config>/app_driver/allowed_apps.json`. Default empty, so every app starts
   not allowed. Only the owner adds entries, through `POST /api/os/apps/allow`.

Then, for acts only, the dispatch confirmation gate (`REQUIRES_CONFIRMATION`) asks the owner
with a prompt that names the exact element ("Click the button 'Save' in TextEdit?").

Reading a snapshot needs layers 1 to 3 but no confirmation: the allow list is the owner's
consent to the model seeing that app. Snapshot text passes through the DLP filter and secure
field values are never read.

## Element ids and re-resolve

A snapshot reads the app's windows (`AXWindows`) depth first, bounded (depth 12, 600 nodes,
values cut to 200 characters). An element id is its index path from the window list, written
like `0.3.1`. It is stable for an unchanged tree and needs no native handle kept between calls.
A snapshot gets a random `snapshot_id`, is cached in memory (last 16, five minute limit) with
the pid and an identity fingerprint per element (role, subrole, title, description).

Before an act: re-read the tree from scratch, then refuse unless the pid matches, the element
exists at the same path, and the identity of the element, each of its ancestors, and the
sibling list at every level along its path are unchanged. Values and positions are allowed to
change (a text field's text, a moved window); structure along the path is not. The fresh
native handle at that path is the one acted on; a stale handle is never used. This is
narrower than "any change anywhere refuses", on purpose: a playing track in Music updates the
tree every second, and a whole-tree rule would refuse every act there. The honest limitation is
that a change elsewhere in the tree that alters what the element means is not caught.

## Actions

- **click**: `AXPress` on the element (no mouse movement, no coordinates).
- **type**: refuse on secret checks, focus the element (`AXFocused`), activate the app
  (permission free), then post Unicode keyboard events to the pid in chunks of 20 characters,
  checking the kill switch and that the app is still frontmost before each chunk.
- **press_key**: named keys plus modifiers (same tables as `app_control_ax`), posted to the pid
  after the same frontmost check. No element needed, but an allowed app is.
- **scroll**: find the element's own or nearest ancestor `AXScrollArea`, move its vertical or
  horizontal scroll bar value by a page fraction. Pure AX, no synthetic wheel events.

## Backends

`PyObjCBackend` when `ApplicationServices` imports; otherwise `OsaBackend` runs JXA through
`osascript` with all inputs passed as a JSON argument, never spliced into script text. The
fallback types with System Events `keystroke`, which goes to the frontmost app, so the same
script checks the frontmost pid first and refuses if it differs. Both backends are behind one
small interface and the module-level backend is injectable (`set_backend`), which is how the
tests run with no macOS calls at all.

## Indicator

`indicator()` returns `{driving, app, action, since, last_action_at}`. `driving` is true while
an act runs and for 10 seconds after the last one, so a UI strip does not flicker between
steps. Listeners registered with `add_indicator_listener` are called on every change; the main
thread can forward them to the existing SSE channel.

## API

Python (`dourmouse.app_driver`): `status()`, `list_allowed()`, `allow_app(app)`,
`disallow_app(app)`, `engage_kill(reason)`, `release_kill()`, `is_killed()`, `snapshot(app)`,
`act(snapshot_id, element_id, action, **kw)`, `press_key(app, key, modifiers)`,
`indicator()`, `running_apps()`.

HTTP (`dourmouse/os_api/apps.py`, all behind the existing auth gate):

| Route | Body or query | Purpose |
|---|---|---|
| GET `/api/os/apps/status` | | trust, backend, kill, indicator, allowed, deny list |
| GET `/api/os/apps/allowed` | | allow list plus running apps with allowed and denied flags |
| POST `/api/os/apps/allow` | `{app}` | owner adds an app; 403 for a deny-listed app |
| POST `/api/os/apps/deny` | `{app}` | owner removes an app |
| POST `/api/os/apps/kill` | `{reason?}` | engage the kill switch |
| POST `/api/os/apps/resume` | `{}` | release it (owner only, no model tool) |
| GET `/api/os/apps/snapshot` | `?app=` | read an allowed app's tree |
| POST `/api/os/apps/act` | `{snapshot_id, element_id, action, text?, key?, modifiers?, direction?, amount?, dry_run?}` | owner-driven act, same checks |

Model tools (`dourmouse/app_driver/tools.py`, `build_app_driver_tools()` returns `ToolSpec`s,
not registered): `app_driver_status`, `app_driver_snapshot` (regular), `app_driver_stop`
(regular, engages the kill switch; there is no model tool to release it), and
`app_driver_click`, `app_driver_type`, `app_driver_press_key`, `app_driver_scroll` (all
`REQUIRES_CONFIRMATION`, with prompts naming the element).
