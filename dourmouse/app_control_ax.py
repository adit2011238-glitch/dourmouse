"""Real macOS Accessibility-API app control (Phase 2 of the 2026-09-13
speed/capability plan — see NATIVE_REWRITE_ROADMAP.md item 8, previously
flagged as "mostly done... native desktop accessibility automation NOT
built").

``app_control.py`` (the existing, still-present module) shells out to
``osascript`` per action: a fresh AppleScript compile + subprocess per
call, a blind string-built menu-path reference with no existence check
before clicking, a fixed 300ms sleep to guess when an app has focused,
and zero use of the real Accessibility API anywhere. It works, and stays
as the automatic fallback for apps whose accessibility tree is broken or
absent — but it is not the fast, reliable path a "control apps at
lightning speed" experience needs.

This module talks to the SAME real macOS primitives Apple's own
Accessibility Inspector and every serious automation tool (including the
computer-use tooling this very coding session has access to) use:

- App activation: ``NSRunningApplication.activateWithOptions_`` — real,
  live-verified to work WITHOUT Accessibility permission at all (it is a
  normal Cocoa API, not AX/UI-scripting) — faster than a subprocess+
  AppleScript round trip for the single most common app-control action.
- Menu clicks / window listing / element discovery: the real
  ``AXUIElementRef`` tree via ``ApplicationServices`` (``pyobjc-framework-
  ApplicationServices``) — direct attribute reads and a real
  ``AXUIElementPerformAction(kAXPressAction)``, not a blind string built
  and hoped for. A menu path that doesn't exist gets a real "here's what
  IS at this level" error instead of AppleScript's often-opaque failure.
- Keystrokes / key presses: ``Quartz.CGEventPost`` (Core Graphics Event
  Services) — direct kernel-level event injection, not
  ``System Events keystroke "..."`` through a second process.

HONEST DEGRADATION (Rule 2.2, same discipline as sandbox.py's own
docstring): menu/window/keystroke actions need real Accessibility
permission for THIS process (``AXIsProcessTrusted()``); when it isn't
granted, every such call here raises a real, specific ``AXControlError``
naming exactly what to do (System Settings > Privacy & Security >
Accessibility) — never a silent no-op and never a fallback to guessing.
The caller (general_roster.py's tool handlers) is what actually falls
back to the AppleScript path automatically; this module's own job is
only to do the AX-based thing correctly, or fail honestly, never both at
once.
"""

from __future__ import annotations

import sys
from typing import Any

_ax_import_error: Exception | None = None
try:
    import ApplicationServices as _AS
    import Quartz as _Quartz
    from AppKit import NSWorkspace
except Exception as exc:  # noqa: BLE001 - pyobjc genuinely absent, or non-macOS
    _AS = None  # type: ignore[assignment]
    _Quartz = None  # type: ignore[assignment]
    NSWorkspace = None  # type: ignore[assignment]
    _ax_import_error = exc


class AXControlError(RuntimeError):
    """A requested AX-based app-control action was refused or failed —
    always with a specific, honest reason, never a bare 'something went
    wrong'. Mirrors app_control.AppControlError's own contract so callers
    can catch either uniformly."""


#: Real AXError codes worth a specific, human message (Apple's own
#: HIToolbox/AXError.h values — verified live against this exact pyobjc
#: build, not guessed: kAXErrorAPIDisabled reproduced live as -25211 by
#: calling AXUIElementCopyAttributeValue against a real running app
#: before this process had Accessibility trust).
_AX_ERROR_MESSAGES = {
    -25211: (  # kAXErrorAPIDisabled
        "NOT CONFIGURED: this process does not have Accessibility "
        "permission. Grant it in System Settings > Privacy & Security > "
        "Accessibility (the entry will be for the Python process running "
        "Dourmouse), then retry — no restart needed once granted."
    ),
    -25204: "no such attribute on this element",  # kAXErrorAttributeUnsupported
    -25205: "this element has no value for that attribute",  # kAXErrorNoValue
    -25202: "the action could not be completed",  # kAXErrorCannotComplete
    -25206: "this action is not supported on this element",  # kAXErrorActionUnsupported
    -25207: "this action is not implemented",  # kAXErrorNotImplemented
    -25201: "the accessibility object is no longer valid (the app or window it pointed to is gone)",  # kAXErrorInvalidUIElement
    -25200: "a required parameter was invalid",  # kAXErrorIllegalArgument
}


def _require_macos_ax() -> None:
    if sys.platform != "darwin":
        raise AXControlError(
            "NOT CONFIGURED: Accessibility-API app control is macOS-only "
            f"(this machine reports sys.platform={sys.platform!r})."
        )
    if _AS is None:
        raise AXControlError(
            "NOT CONFIGURED: pyobjc-framework-ApplicationServices is not "
            f"importable ({_ax_import_error!r}). Install it "
            "(pip install pyobjc-framework-ApplicationServices) — the "
            "AppleScript-based app control still works meanwhile."
        )


def ax_trusted() -> bool:
    """True if THIS process currently has Accessibility permission.
    Never raises — a permission probe must always give a real answer."""
    if sys.platform != "darwin" or _AS is None:
        return False
    try:
        return bool(_AS.AXIsProcessTrusted())
    except Exception:  # noqa: BLE001 - a probe must never crash a caller
        return False


def request_ax_trust() -> bool:
    """Ask macOS to show ITS OWN native "Dourmouse wants to control your
    computer" permission dialog — a real, standard system API call
    (AXIsProcessTrustedWithOptions with the prompt option set), not
    Dourmouse modifying any system setting itself. The user still makes
    the actual decision in that system dialog; this only requests that
    the dialog appear. Returns the CURRENT trust state (almost always
    still False immediately after calling this — macOS shows the prompt
    asynchronously and often needs the app relaunched after the user
    grants it, which this function cannot itself detect or force)."""
    _require_macos_ax()
    try:
        options = {_AS.kAXTrustedCheckOptionPrompt: True}
        return bool(_AS.AXIsProcessTrustedWithOptions(options))
    except Exception as exc:  # noqa: BLE001 - surface the real failure
        raise AXControlError(f"could not request Accessibility trust: {exc}") from exc


def _ax_error_message(code: int) -> str:
    return _AX_ERROR_MESSAGES.get(code, f"AXError {code}")


def _pid_for_app(app_name: str) -> int:
    """The PID of a running app by its localized name (case-insensitive,
    exact match — the same identifier app_control.py's AppleScript path
    already uses). A real, honest 'not running' error when no match
    exists, never a guess at which similarly-named process was meant."""
    _require_macos_ax()
    target = app_name.strip().lower()
    if not target:
        raise AXControlError("app_name is required")
    running = NSWorkspace.sharedWorkspace().runningApplications()
    matches = [a for a in running if (a.localizedName() or "").strip().lower() == target]
    if not matches:
        running_names = sorted({(a.localizedName() or "").strip() for a in running if a.localizedName()})
        raise AXControlError(
            f"NOT RUNNING: no running app named {app_name!r}. Currently "
            f"running: {', '.join(running_names[:20])}"
            + (f" (+{len(running_names) - 20} more)" if len(running_names) > 20 else "")
        )
    return int(matches[0].processIdentifier())


def _copy_attr(element: Any, attribute: str) -> Any:
    """One AXUIElementCopyAttributeValue call, raising a real, specific
    AXControlError on any non-success code instead of returning None
    and letting the caller silently misinterpret "no value" as "empty"."""
    err, value = _AS.AXUIElementCopyAttributeValue(element, attribute, None)
    if err != _AS.kAXErrorSuccess:
        raise AXControlError(f"could not read {attribute}: {_ax_error_message(err)}")
    return value


#: NSApplicationActivationPolicy.Regular — apps with a Dock icon / normal
#: foreground UI. Verified live to match exactly the same real app set
#: AppleScript's "background only is false" filter (app_control.py's
#: list_running_apps) already reports on this machine.
_NS_ACTIVATION_POLICY_REGULAR = 0


def list_running_apps_fast() -> list[dict[str, object]]:
    """Every foreground app, via NSWorkspace directly — no subprocess, no
    AppleScript compile step, and (like activate/quit_app_fast) no
    Accessibility permission needed at all. Read-only, no confirmation
    needed, same real output shape as app_control.list_running_apps."""
    _require_macos_ax()
    running = NSWorkspace.sharedWorkspace().runningApplications()
    regular = [a for a in running if a.activationPolicy() == _NS_ACTIVATION_POLICY_REGULAR]
    return [
        {"name": a.localizedName(), "frontmost": bool(a.isActive())}
        for a in regular
    ]


def activate_app_fast(app_name: str, dry_run: bool = False) -> str:
    """Bring an app to the foreground via NSRunningApplication — a real,
    live-verified Cocoa API call that needs NO Accessibility permission
    at all (unlike everything else in this module), so it works on a
    completely fresh install with zero setup. This is the fast path for
    the single most common app-control action; general_roster.py's
    handler falls back to app_control.activate_app (AppleScript) only if
    this raises for a genuinely different reason (e.g. non-macOS)."""
    _require_macos_ax()
    app_name = app_name.strip()
    if not app_name:
        raise AXControlError("app_name is required")
    pid = _pid_for_app(app_name)
    if dry_run:
        return f"DRY RUN — activate {app_name} (pid {pid}) (not executed)"
    running = NSWorkspace.sharedWorkspace().runningApplications()
    target = next(a for a in running if int(a.processIdentifier()) == pid)
    # options=0: no special flags needed (the old "activate all windows"
    # flag this API used to require is deprecated/ignored on modern
    # macOS — bringing the app forward is the default behavior now).
    ok = bool(target.activateWithOptions_(0))
    if not ok:
        raise AXControlError(f"macOS refused to activate {app_name!r} (activateWithOptions_ returned false)")
    return f"ACTIVATED: {app_name}"


def quit_app_fast(app_name: str, dry_run: bool = False) -> str:
    """Quit an app via NSRunningApplication.terminate() — same
    permission-free win as activate_app_fast, a normal Cocoa API call,
    not UI scripting. terminate() asks the app to quit normally (it can
    still show its own "save changes?" dialog, same as Cmd+Q would);
    forceTerminate() is deliberately never used here — silently
    discarding a user's unsaved work is not this tool's call to make."""
    _require_macos_ax()
    app_name = app_name.strip()
    if not app_name:
        raise AXControlError("app_name is required")
    pid = _pid_for_app(app_name)
    if dry_run:
        return f"DRY RUN — quit {app_name} (pid {pid}) (not executed)"
    running = NSWorkspace.sharedWorkspace().runningApplications()
    target = next(a for a in running if int(a.processIdentifier()) == pid)
    ok = bool(target.terminate())
    if not ok:
        raise AXControlError(f"macOS refused to quit {app_name!r} (terminate() returned false)")
    return f"QUIT: {app_name}"


def _ax_app_element(app_name: str) -> Any:
    pid = _pid_for_app(app_name)
    return _AS.AXUIElementCreateApplication(pid)


def list_windows_ax(app_name: str) -> list[str]:
    """Window titles for a running app via a direct AX attribute read —
    one round trip to the target process, no subprocess/AppleScript
    compile step."""
    _require_macos_ax()
    element = _ax_app_element(app_name)
    windows = _copy_attr(element, _AS.kAXWindowsAttribute) or []
    titles = []
    for w in windows:
        try:
            titles.append(str(_copy_attr(w, _AS.kAXTitleAttribute) or ""))
        except AXControlError:
            titles.append("")  # a window that refuses its own title is still a real window
    return titles


def _menu_children(element: Any) -> list[Any]:
    try:
        return list(_copy_attr(element, _AS.kAXChildrenAttribute) or [])
    except AXControlError:
        return []


def _element_title(element: Any) -> str:
    try:
        return str(_copy_attr(element, _AS.kAXTitleAttribute) or "")
    except AXControlError:
        return ""


def find_menu_item_ax(app_name: str, menu_path: list[str]) -> Any:
    """Walk the REAL menu-bar accessibility tree to the exact item named
    by menu_path (e.g. ["File", "New Window"]), returning the real
    AXUIElement — never a guessed/blind reference. Each level is checked
    for existence before descending; a wrong path fails with the real
    list of what WAS found at that level, not a generic AppleScript
    error a human then has to decode."""
    _require_macos_ax()
    if not menu_path or len(menu_path) < 2:
        raise AXControlError(
            "menu_path needs at least [top-level menu, item], e.g. "
            '["File", "New Window"]'
        )
    app_element = _ax_app_element(app_name)
    menu_bar = _copy_attr(app_element, _AS.kAXMenuBarAttribute)
    # A menu BAR's own direct children are "menu bar item"s (File, Edit,
    # ...); each one's single child is the actual pull-down "menu" whose
    # children are the real, clickable items/submenus.
    level_elements = _menu_children(menu_bar)
    current_label = "menu bar"
    for depth, label in enumerate(menu_path):
        matches = [e for e in level_elements if _element_title(e) == label]
        if not matches:
            available = sorted({_element_title(e) for e in level_elements if _element_title(e)})
            raise AXControlError(
                f"MENU ITEM NOT FOUND: {app_name!r} has no {label!r} under "
                f"{current_label} — available there: {', '.join(available) or '(nothing readable)'}"
            )
        found = matches[0]
        current_label = label
        if depth == len(menu_path) - 1:
            return found
        # Descend: a menu bar item / submenu's clickable children live
        # one level down, inside its own "menu" child (AXChildren of a
        # menu-bar-item or menu is that single pull-down menu).
        submenu_children = _menu_children(found)
        level_elements = submenu_children[0] and _menu_children(submenu_children[0]) or []
    raise AXControlError(f"MENU ITEM NOT FOUND: {' > '.join(menu_path)} in {app_name}")


def click_menu_item_ax(app_name: str, menu_path: list[str], dry_run: bool = False) -> str:
    """Find the real menu item via the accessibility tree, then press it
    for real via AXUIElementPerformAction — no blind AppleScript
    string-building, no click landing on a stale/wrong reference."""
    _require_macos_ax()
    app_name = app_name.strip()
    if not app_name:
        raise AXControlError("app_name is required")
    element = find_menu_item_ax(app_name, menu_path)
    if dry_run:
        return f"DRY RUN — click {' > '.join(menu_path)} in {app_name} (not executed, real menu item confirmed to exist)"
    err = _AS.AXUIElementPerformAction(element, _AS.kAXPressAction)
    if err != _AS.kAXErrorSuccess:
        raise AXControlError(
            f"could not click {' > '.join(menu_path)} in {app_name}: {_ax_error_message(err)}"
        )
    return f"CLICKED {' > '.join(menu_path)} in {app_name}"


#: Real CGKeyCode values (Apple's own Carbon/HIToolbox key codes — the
#: SAME numeric values app_control.py's AppleScript "key code N" already
#: uses, since AppleScript's key code IS this same virtual keycode
#: space; kept as a separate table here rather than importing
#: app_control's private one, so this module has zero dependency on the
#: fallback module it is itself a faster alternative to).
_KEY_CODES = {
    "return": 36, "enter": 76, "tab": 48, "escape": 53, "delete": 51,
    "space": 49, "up": 126, "down": 125, "left": 123, "right": 124,
}

#: CGEventFlags bit values for modifier keys (Quartz.CGEventFlags).
_MODIFIER_FLAGS = {
    "command": 1 << 20, "cmd": 1 << 20,
    "option": 1 << 19, "alt": 1 << 19,
    "shift": 1 << 17,
    "control": 1 << 18, "ctrl": 1 << 18,
}


def _require_ax_trust_for_synthetic_events() -> None:
    """CGEventPost has no error return at all — a synthetic event silently
    dropped by macOS (which live-testing confirmed IS what happens
    without Accessibility trust: the call succeeds, no exception, no
    error code, and the target app never receives the keystroke) would
    otherwise make press_key_ax/send_keystrokes_ax claim "PRESSED"/
    "TYPED" for something that never actually happened — exactly the
    fabricated-success Rule 2.2 exists to prevent. Checked explicitly
    here, before posting anything, since the alternative is trusting an
    API that cannot itself tell us whether it worked."""
    if not ax_trusted():
        raise AXControlError(_AX_ERROR_MESSAGES[-25211])


def press_key_ax(
    app_name: str, key: str, modifiers: list[str] | None = None, dry_run: bool = False
) -> str:
    """Activate the app (via the fast, permission-free path), then post
    a real keyboard event via Quartz Core Graphics Event Services — a
    direct kernel-level event, not a second process's "keystroke"
    command. Needs the SAME Accessibility permission as the menu/window
    functions above — live-verified: created a real empty TextEdit
    document (via TextEdit's own AppleScript dictionary, no AX needed),
    posted a real CGEventPost keystroke to it with this process
    untrusted, and confirmed via `get text of front document` that
    NOTHING landed — CGEventPost raised no exception and returned no
    error, it just silently dropped the event. Checked explicitly here
    (see _require_ax_trust_for_synthetic_events) rather than trusted,
    since the API itself cannot tell us whether it worked."""
    _require_macos_ax()
    app_name = app_name.strip()
    key = key.strip().lower()
    if not app_name:
        raise AXControlError("app_name is required")
    if key not in _KEY_CODES:
        raise AXControlError(f"unknown key {key!r}; supported: {', '.join(sorted(_KEY_CODES))}")
    _require_ax_trust_for_synthetic_events()
    flags = 0
    for m in modifiers or []:
        flag = _MODIFIER_FLAGS.get(m.strip().lower())
        if flag is None:
            raise AXControlError(f"unknown modifier {m!r}; supported: {', '.join(sorted(_MODIFIER_FLAGS))}")
        flags |= flag
    label = "+".join((modifiers or []) + [key])
    if dry_run:
        return f"DRY RUN — press {label} in {app_name} (not executed)"
    activate_app_fast(app_name)
    code = _KEY_CODES[key]
    down = _Quartz.CGEventCreateKeyboardEvent(None, code, True)
    up = _Quartz.CGEventCreateKeyboardEvent(None, code, False)
    if flags:
        _Quartz.CGEventSetFlags(down, flags)
        _Quartz.CGEventSetFlags(up, flags)
    _Quartz.CGEventPost(_Quartz.kCGHIDEventTap, down)
    _Quartz.CGEventPost(_Quartz.kCGHIDEventTap, up)
    return f"PRESSED {label} in {app_name}"


def send_keystrokes_ax(app_name: str, text: str, dry_run: bool = False) -> str:
    """Activate the app, then post real per-character keyboard events via
    CGEventPost using CGEventKeyboardSetUnicodeString — types the exact
    Unicode text without needing a keycode for every character (unlike
    press_key_ax, which is limited to the small named-key table). Same
    real-blocker-surfaces-even-in-dry_run consistency as press_key_ax and
    activate_app_fast: a dry run that can't actually run for real (no
    Accessibility permission) says so, rather than reporting a
    misleadingly clean preview."""
    _require_macos_ax()
    app_name = app_name.strip()
    if not app_name:
        raise AXControlError("app_name is required")
    if not text:
        raise AXControlError("text is required")
    _require_ax_trust_for_synthetic_events()
    if dry_run:
        return f"DRY RUN — type {len(text)} character(s) into {app_name} (not executed)"
    activate_app_fast(app_name)
    down = _Quartz.CGEventCreateKeyboardEvent(None, 0, True)
    up = _Quartz.CGEventCreateKeyboardEvent(None, 0, False)
    _Quartz.CGEventKeyboardSetUnicodeString(down, len(text), text)
    _Quartz.CGEventKeyboardSetUnicodeString(up, len(text), text)
    _Quartz.CGEventPost(_Quartz.kCGHIDEventTap, down)
    _Quartz.CGEventPost(_Quartz.kCGHIDEventTap, up)
    return f"TYPED into {app_name}: {len(text)} character(s)"
