"""App control (backlog: "control other apps in the background, like
Claude Desktop") — real macOS automation of other running applications:
list what's running, bring one forward, send it keystrokes, click a menu
item, list/close its windows, quit it.

Backend: AppleScript via ``osascript``, driving System Events (the same
mechanism macOS's own Accessibility/Automation frameworks use — there is
no lower-risk way to do this on macOS short of an app publishing its own
scripting dictionary, which most modern apps, Claude Desktop included,
don't). ``activate``/``quit`` are part of every Cocoa/Electron app's
generic scripting suite and work without a custom dictionary; keystrokes,
menu clicks and window listing go through System Events UI scripting,
which needs the user to have granted this process Accessibility access
in System Settings > Privacy & Security > Accessibility — when that
hasn't happened, every call here reports NOT CONFIGURED honestly (Rule
2.2) rather than silently failing or guessing.

Not implemented for Windows/Linux yet — this Dourmouse deployment is
Mac-first; the same structured-action shape (activate/keystroke/click/
windows/quit) should map to UI Automation / pywinauto on the Windows
desktop later, but that is a separate, real backend, not a stub here.

Every mutating action (activate/keystroke/press_key/click_menu_item/
quit) is confirmation-gated at the tool-registration layer (see
general_roster.py's "apps" subagent) — this module itself does not know
about confirmation, only about doing the thing correctly and honestly
once it's been approved.
"""

from __future__ import annotations

import subprocess
import sys

_OSASCRIPT_TIMEOUT = 15

#: Apps this module refuses to drive at all, regardless of confirmation —
#: security-surface software where blind UI scripting (a wrong click, a
#: misdirected keystroke) risks something worse than "the wrong app did
#: the wrong thing" (e.g. approving a permission dialog, revealing a
#: stored password). Case-insensitive exact match on the app name.
_BLOCKED_APPS = frozenset(
    {
        "system settings",
        "system preferences",
        "keychain access",
        "finder",  # UI-scripting Finder can trigger delete/empty-trash dialogs
    }
)

#: A handful of named keys mapped to AppleScript `key code` numbers —
#: covers the ones an agent actually needs (submit a form, dismiss a
#: dialog, navigate a list) without pretending to be a full keyboard map.
_KEY_CODES = {
    "return": 36,
    "enter": 76,
    "tab": 48,
    "escape": 53,
    "delete": 51,
    "space": 49,
    "up": 126,
    "down": 125,
    "left": 123,
    "right": 124,
}

_MODIFIER_CLAUSE = {
    "command": "command down",
    "cmd": "command down",
    "option": "option down",
    "alt": "option down",
    "shift": "shift down",
    "control": "control down",
    "ctrl": "control down",
}


class AppControlError(RuntimeError):
    """A requested app-control action was refused or failed — always with
    a specific, honest reason (Rule 2.1), never a bare 'something went
    wrong'."""


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise AppControlError(
            "NOT CONFIGURED: app control is implemented for macOS only in "
            f"this build (this machine reports sys.platform={sys.platform!r})."
        )


def _check_not_blocked(app_name: str) -> None:
    if app_name.strip().lower() in _BLOCKED_APPS:
        raise AppControlError(
            f"REFUSED: {app_name!r} is on the app-control blocklist "
            "(security-surface apps aren't UI-scriptable through this tool, "
            "regardless of confirmation)."
        )


def _run_osascript(script: str) -> str:
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=_OSASCRIPT_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise AppControlError(f"osascript timed out after {_OSASCRIPT_TIMEOUT}s") from exc
    except OSError as exc:
        raise AppControlError(f"could not run osascript: {exc}") from exc
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        if "not allowed assistive access" in err.lower() or "1002" in err:
            raise AppControlError(
                "NOT CONFIGURED: this needs Accessibility permission. Grant it "
                "in System Settings > Privacy & Security > Accessibility for "
                "whatever process is running Dourmouse (Terminal/python), "
                "then retry."
            )
        if "osascript is not allowed" in err.lower() or "1743" in err:
            raise AppControlError(
                "NOT CONFIGURED: this needs Automation permission for the "
                "target app. Grant it in System Settings > Privacy & "
                "Security > Automation."
            )
        raise AppControlError(f"osascript error: {err or 'unknown failure'}")
    return (proc.stdout or "").strip()


def list_running_apps() -> list[dict[str, object]]:
    """Every foreground (non-background-only) process System Events can
    see, with which one (if any) is frontmost. Read-only, no
    confirmation needed."""
    _require_macos()
    names_raw = _run_osascript(
        'tell application "System Events" to get name of every process '
        "whose background only is false"
    )
    names = [n.strip() for n in names_raw.split(",") if n.strip()]
    frontmost = ""
    try:
        frontmost = _run_osascript(
            'tell application "System Events" to get name of first process '
            "whose frontmost is true"
        )
    except AppControlError:
        pass
    return [{"name": n, "frontmost": n == frontmost} for n in names]


def list_windows(app_name: str) -> list[str]:
    """Window titles for one running app. Read-only, no confirmation
    needed."""
    _require_macos()
    app_name = app_name.strip()
    if not app_name:
        raise AppControlError("app_name is required")
    out = _run_osascript(
        f'tell application "System Events" to tell process "{_escape(app_name)}" '
        "to get name of every window"
    )
    return [w.strip() for w in out.split(",") if w.strip()]


def activate_app(app_name: str) -> str:
    """Bring an app to the foreground. Part of every app's generic
    scripting suite — works even for apps with no custom AppleScript
    dictionary (Claude Desktop included)."""
    _require_macos()
    app_name = app_name.strip()
    if not app_name:
        raise AppControlError("app_name is required")
    _check_not_blocked(app_name)
    _run_osascript(f'tell application "{_escape(app_name)}" to activate')
    return f"ACTIVATED: {app_name}"


def quit_app(app_name: str) -> str:
    """Quit a running app via its generic scripting suite."""
    _require_macos()
    app_name = app_name.strip()
    if not app_name:
        raise AppControlError("app_name is required")
    _check_not_blocked(app_name)
    _run_osascript(f'tell application "{_escape(app_name)}" to quit')
    return f"QUIT: {app_name}"


def send_keystrokes(app_name: str, text: str) -> str:
    """Activate an app, then type literal text into whatever has focus
    inside it. Real risk if the app takes noticeably longer than the
    fixed delay to actually focus a text field — this reports what it
    did, never that the text definitely landed where intended."""
    _require_macos()
    app_name = app_name.strip()
    if not app_name:
        raise AppControlError("app_name is required")
    if not text:
        raise AppControlError("text is required")
    _check_not_blocked(app_name)
    script = (
        f'tell application "{_escape(app_name)}" to activate\n'
        "delay 0.3\n"
        "tell application \"System Events\"\n"
        f'  keystroke "{_escape(text)}"\n'
        "end tell"
    )
    _run_osascript(script)
    return f"TYPED into {app_name}: {len(text)} character(s)"


def press_key(app_name: str, key: str, modifiers: list[str] | None = None) -> str:
    """Activate an app, then send one named key (return/tab/escape/
    delete/space/arrows) with optional modifiers (command/option/shift/
    control)."""
    _require_macos()
    app_name = app_name.strip()
    key = key.strip().lower()
    if not app_name:
        raise AppControlError("app_name is required")
    if key not in _KEY_CODES:
        raise AppControlError(
            f"unknown key {key!r}; supported: {', '.join(sorted(_KEY_CODES))}"
        )
    _check_not_blocked(app_name)
    mods = []
    for m in modifiers or []:
        clause = _MODIFIER_CLAUSE.get(m.strip().lower())
        if clause is None:
            raise AppControlError(
                f"unknown modifier {m!r}; supported: {', '.join(sorted(_MODIFIER_CLAUSE))}"
            )
        mods.append(clause)
    using_clause = f" using {{{', '.join(mods)}}}" if mods else ""
    script = (
        f'tell application "{_escape(app_name)}" to activate\n'
        "delay 0.3\n"
        "tell application \"System Events\"\n"
        f"  key code {_KEY_CODES[key]}{using_clause}\n"
        "end tell"
    )
    _run_osascript(script)
    return f"PRESSED {'+'.join((modifiers or []) + [key])} in {app_name}"


def click_menu_item(app_name: str, menu_path: list[str]) -> str:
    """Click a menu item by its full path, e.g. ["File", "New Window"]
    or ["File", "Export", "PDF..."]. Built as nested AppleScript
    `menu item ... of menu ... of menu bar item ... of menu bar 1`."""
    _require_macos()
    app_name = app_name.strip()
    if not app_name:
        raise AppControlError("app_name is required")
    if not menu_path or len(menu_path) < 2:
        raise AppControlError(
            "menu_path needs at least [top-level menu, item], e.g. "
            '["File", "New Window"]'
        )
    _check_not_blocked(app_name)
    top, *rest = menu_path
    # Innermost-first: the last entry is the "menu item" being clicked;
    # everything before it (walked in reverse) is a "menu of" ancestor,
    # bottoming out at the top-level menu bar item.
    ref = f'menu item "{_escape(rest[-1])}"'
    for label in reversed(rest[:-1]):
        ref += f' of menu "{_escape(label)}"'
    ref += f' of menu "{_escape(top)}" of menu bar item "{_escape(top)}" of menu bar 1'
    script = (
        f'tell application "{_escape(app_name)}" to activate\n'
        "delay 0.3\n"
        "tell application \"System Events\"\n"
        f'  tell process "{_escape(app_name)}"\n'
        f"    click {ref}\n"
        "  end tell\n"
        "end tell"
    )
    _run_osascript(script)
    return f"CLICKED {' > '.join(menu_path)} in {app_name}"


def _escape(value: str) -> str:
    """Escape a value for embedding inside a double-quoted AppleScript
    string literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')
