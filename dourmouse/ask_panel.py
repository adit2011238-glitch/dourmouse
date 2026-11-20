"""The Ask panel's rules (plan 4.9, PL-04): its settings, the hotkey check, the mode prefixes, and which approvals
may be answered inside the small window.

Pure and small on purpose. ``os_api/ask.py`` serves it, ``electron/main.js`` registers the hotkey it validated, and
``ui/assets/os/ask/`` shows the window. One place decides what a good hotkey is, so the app and the settings screen
cannot disagree.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

MODES = ("ask", "note", "remind", "do")
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "hotkey": "Alt+Space",  # Option Space, decision D-16
    "close_on_blur": True,
    "default_mode": "ask",
    "keep_minutes": 10,
}
KEEP_MAX = 120

_MODIFIERS = {
    "command": "Command", "cmd": "Command", "commandorcontrol": "Command", "cmdorctrl": "Command",
    "control": "Control", "ctrl": "Control",
    "alt": "Alt", "option": "Alt",
    "shift": "Shift",
    "super": "Command", "meta": "Command",
}
_ORDER = ["Command", "Control", "Alt", "Shift"]
_NAMED_KEYS = {
    "space": "Space", "tab": "Tab", "enter": "Return", "return": "Return", "escape": "Escape", "esc": "Escape",
    "backspace": "Backspace", "delete": "Delete", "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "home": "Home", "end": "End", "pageup": "PageUp", "pagedown": "PageDown",
}
# Chords macOS or this app already uses. Each answer says who has it.
_TAKEN = {
    "Command+Space": "Spotlight",
    "Command+Alt+Space": "Finder search",
    "Control+Space": "switching the input source",
    "Control+Alt+Space": "switching the input source",
    "Command+Tab": "the app switcher",
    "Command+Shift+Tab": "the app switcher",
    "Command+Q": "quitting the front app",
    "Command+W": "closing the window",
    "Command+H": "hiding the app",
    "Command+M": "minimising the window",
    "Command+C": "copy", "Command+V": "paste", "Command+X": "cut", "Command+A": "select all", "Command+Z": "undo",
    "Command+Shift+3": "a screenshot", "Command+Shift+4": "a screenshot", "Command+Shift+5": "the screenshot bar",
    "Command+Alt+Escape": "Force Quit",
    "Command+K": "Search inside Dourmouse",
    "Command+,": "Settings inside Dourmouse",
    "Command+N": "New Thread inside Dourmouse",
}


def clean_hotkey(raw: Any) -> str:
    """A canonical Electron accelerator ("Alt+Space"), or ValueError saying what is wrong."""
    text = str(raw or "").strip()
    if not text or len(text) > 40:
        raise ValueError("choose a shortcut, for example Option Space")
    parts = [p.strip() for p in re.split(r"\s*\+\s*|\s+", text) if p.strip()]
    mods: list[str] = []
    key = ""
    for part in parts:
        low = part.lower()
        if low in _MODIFIERS:
            mod = _MODIFIERS[low]
            if mod not in mods:
                mods.append(mod)
            continue
        if key:
            raise ValueError("a shortcut has one key besides the modifiers")
        if low in _NAMED_KEYS:
            key = _NAMED_KEYS[low]
        elif re.fullmatch(r"[a-z0-9]", low):
            key = low.upper()
        elif re.fullmatch(r"f([1-9]|1[0-9]|2[0-4])", low):
            key = low.upper()
        elif part in (",", ".", "/", ";", "'", "[", "]", "-", "=", "`"):
            key = part
        else:
            raise ValueError(f"'{part}' is not a key this shortcut can use")
    if not key:
        raise ValueError("a shortcut needs a key as well as a modifier")
    if not any(m in mods for m in ("Command", "Control", "Alt")):
        raise ValueError("a shortcut needs Command, Control or Option, or it would fire while you type")
    ordered = [m for m in _ORDER if m in mods]
    chord = "+".join(ordered + [key])
    taken = _TAKEN.get(chord)
    if taken:
        raise ValueError(f"{pretty_hotkey(chord)} is already used for {taken}")
    return chord


def pretty_hotkey(chord: str) -> str:
    glyph = {"Command": "Cmd", "Control": "Ctrl", "Alt": "Option", "Shift": "Shift"}
    return " ".join(glyph.get(p, p) for p in str(chord).split("+"))


def clean_settings(raw: Any, base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge ``raw`` over ``base`` (or the defaults), checking every field. Raises ValueError with the reason."""
    out = dict(DEFAULTS)
    out.update({k: v for k, v in (base or {}).items() if k in DEFAULTS})
    raw = raw if isinstance(raw, dict) else {}
    if "hotkey" in raw:
        out["hotkey"] = clean_hotkey(raw["hotkey"])
    for name in ("enabled", "close_on_blur"):
        if name in raw:
            if not isinstance(raw[name], bool):
                raise ValueError(f"{name} is on or off")
            out[name] = raw[name]
    if "default_mode" in raw:
        if raw["default_mode"] not in MODES:
            raise ValueError("the starting mode is ask, note, remind or do")
        out["default_mode"] = raw["default_mode"]
    if "keep_minutes" in raw:
        value = raw["keep_minutes"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > KEEP_MAX:
            raise ValueError(f"keep the conversation for 0 to {KEEP_MAX} minutes")
        out["keep_minutes"] = value
    return out


def load(path: Path) -> dict[str, Any]:
    """The saved settings, or the defaults. A file that cannot be read is set aside by state_file, not trusted."""
    from dourmouse.state_file import load_json_or_aside

    data, _problem = load_json_or_aside(path, dict)
    try:
        return clean_settings(data or {})
    except ValueError:
        return dict(DEFAULTS)


def save(path: Path, settings: dict[str, Any]) -> None:
    from dourmouse.state_file import atomic_write_text

    atomic_write_text(path, json.dumps(settings, indent=2, sort_keys=True), mode=0o600)


def parse_input(text: str, default_mode: str = "ask") -> dict[str, str]:
    """``n note``, ``r remind text`` and ``/skill`` pick a mode by their first characters. Anything else uses the
    panel's starting mode. A bare prefix with nothing after it stays plain text, so typing "n" alone is not a note."""
    raw = str(text or "")
    stripped = raw.lstrip()
    mode = default_mode if default_mode in MODES else "ask"
    body = stripped
    if len(stripped) > 2 and stripped[1] == " " and stripped[0] in "nN":
        mode, body = "note", stripped[2:]
    elif len(stripped) > 2 and stripped[1] == " " and stripped[0] in "rR":
        mode, body = "remind", stripped[2:]
    elif len(stripped) > 1 and stripped[0] == "/":
        mode, body = "do", stripped[1:]
    return {"mode": mode, "text": body.strip()}


# Approvals the small window may answer itself: a change to a file the owner can see, nothing that leaves the Mac,
# spends money, deletes, runs a command or drives another app. Every other tool says "open the app to approve".
# A tool the list does not know is high risk.
LOW_RISK_TOOLS = frozenset({"write_file", "write_path", "apply_patch", "search_and_replace", "make_directory"})


def approval_risk(tool: str) -> str:
    return "low" if str(tool or "") in LOW_RISK_TOOLS else "high"


def save_note(text: str) -> dict[str, Any]:
    """Keep one note in the user wiki. Refuses text that looks like a key or password (nothing is stored)."""
    from dourmouse import library

    body = str(text or "").strip()
    if not body:
        raise ValueError("there is nothing to note")
    if library.holds_secret(body):
        raise ValueError("this looks like a key or password, so it was not saved")
    first = body.splitlines()[0][:80]
    added = library.wiki().add("note", body, {"title": first, "tier": "T1"})
    return {"doc_id": added["doc_id"], "title": first}
