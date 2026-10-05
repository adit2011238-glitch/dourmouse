"""Model tools for app driving, in the roster's ToolSpec shape. NOT registered.

``build_app_driver_tools()`` returns the specs; the main thread adds them to a
subagent in ``general_roster`` and to the confirmation set. Reading (status,
snapshot) and stopping are regular tools. Every act (click, type, press_key,
scroll) is ``Permission.REQUIRES_CONFIRMATION`` with a prompt that names the
exact element. There is deliberately no tool to allow an app, remove one, or
release the kill switch: those are the owner's own clicks over HTTP.
"""

from __future__ import annotations

import json
from typing import Any

from . import driver, safety
from .errors import AppDriverError

ACTOR = "model"

_INTERACTIVE_ROLES = frozenset({
    "AXWindow", "AXButton", "AXTextField", "AXTextArea", "AXSearchField", "AXCheckBox", "AXRadioButton",
    "AXPopUpButton", "AXMenuButton", "AXLink", "AXComboBox", "AXSlider", "AXScrollArea", "AXTabGroup",
    "AXRow", "AXCell", "AXMenuItem", "AXDisclosureTriangle", "AXIncrementor", "AXSecureTextField",
})

_DRY_RUN_PROP = {
    "type": "boolean",
    "default": False,
    "description": "Check everything and say what would happen, without doing it.",
}


def _dry_run(arguments: dict[str, Any]) -> bool:
    from dourmouse.config import app_control_dry_run_enabled

    return bool(arguments.get("dry_run")) or app_control_dry_run_enabled()


def _line(el: dict[str, Any]) -> str:
    parts = [f"{el['id']} {el.get('role') or '?'}"]
    if el.get("subrole"):
        parts.append(f"/{el['subrole']}")
    for key in ("title", "description", "placeholder"):
        if el.get(key):
            parts.append(f" {key}={el[key]!r}")
    if el.get("secure"):
        parts.append(" value=<secure field, not read>")
    elif el.get("value") not in (None, ""):
        parts.append(f" value={str(el['value'])[:120]!r}")
    if el.get("enabled") is False:
        parts.append(" [disabled]")
    if el.get("focused"):
        parts.append(" [focused]")
    return "  " * int(el.get("depth") or 0) + "".join(parts)


def format_snapshot(snap: dict[str, Any], show_all: bool = False, limit: int = 300) -> str:
    rows = [
        el for el in snap["elements"]
        if show_all or el.get("role") in _INTERACTIVE_ROLES
        or any(el.get(k) for k in ("title", "description", "placeholder", "value"))
    ]
    head = (
        f"SNAPSHOT {snap['snapshot_id']} of {snap['app']['name']} (pid {snap['app']['pid']}): "
        f"{snap['count']} element(s) read, {min(len(rows), limit)} shown"
        + (", tree TRUNCATED at the size limit" if snap.get("truncated") else "")
        + ". Act with snapshot_id and an element id below. Values are DLP-scrubbed; secure fields are never read."
    )
    body = "\n".join(_line(el) for el in rows[:limit])
    more = f"\n... {len(rows) - limit} more not shown" if len(rows) > limit else ""
    return f"{head}\n{body or '(no windows or readable elements)'}{more}"


def _guard(fn):
    def handler(arguments: dict[str, Any]) -> str:
        try:
            return fn(arguments)
        except AppDriverError as exc:
            return exc.as_tool_text()

    return handler


def _status_tool(arguments: dict[str, Any]) -> str:
    info = driver.status()
    return json.dumps({
        "trusted": info.get("trusted"),
        "backend": info.get("backend"),
        "kill_switch_engaged": info["kill"]["engaged"],
        "driving": info["indicator"]["driving"],
        "driving_app": info["indicator"].get("app"),
        "allowed_apps": [a.get("name") for a in info["allowed"]],
        "help": info.get("trust_help"),
    })


def _snapshot_tool(arguments: dict[str, Any]) -> str:
    snap = driver.snapshot(str(arguments.get("app_name") or ""), actor=ACTOR)
    return format_snapshot(snap, show_all=bool(arguments.get("show_all")))


def _stop_tool(arguments: dict[str, Any]) -> str:
    safety.engage_kill(str(arguments.get("reason") or "stopped by the model"), by=ACTOR)
    return "STOPPED: the kill switch is engaged. No app will be driven until the owner resumes it."


def _act(action: str):
    def run(arguments: dict[str, Any]) -> str:
        result = driver.act(
            str(arguments.get("snapshot_id") or ""),
            str(arguments.get("element_id") or ""),
            action,
            text=arguments.get("text"),
            direction=str(arguments.get("direction") or "down"),
            amount=arguments.get("amount", 0.25),
            dry_run=_dry_run(arguments),
            actor=ACTOR,
        )
        return result["detail"] if result.get("dry_run") else f"DONE: {result['detail']}"

    return run


def _press_tool(arguments: dict[str, Any]) -> str:
    result = driver.press_key(str(arguments.get("app_name") or ""), str(arguments.get("key") or ""),
                              list(arguments.get("modifiers") or []), dry_run=_dry_run(arguments), actor=ACTOR)
    return result["detail"] if result.get("dry_run") else f"DONE: {result['detail']}"


def _target_props() -> dict[str, Any]:
    return {
        "snapshot_id": {"type": "string", "description": "From app_driver_snapshot."},
        "element_id": {"type": "string", "description": "An element id from that snapshot, like 0.3.1."},
    }


def build_app_driver_tools() -> list[Any]:
    from dourmouse.dispatch import Permission, ToolSpec

    gated = Permission.REQUIRES_CONFIRMATION
    return [
        ToolSpec(
            name="app_driver_status",
            description=(
                "Whether the model can drive Mac apps right now: Accessibility permission, the "
                "kill switch, which apps the owner has allowed, and whether a drive is in "
                "progress. Call it when app_driver_snapshot is refused, to see why; only the "
                "owner can allow an app."
            ),
            parameters={"type": "object", "properties": {}},
            handler=_guard(_status_tool),
        ),
        ToolSpec(
            name="app_driver_snapshot",
            description=(
                "Read the windows and controls of a running Mac app the owner has allowed "
                "(role, title, value, position) and get a snapshot_id and element ids (like "
                "0.3.1) to act on. Always snapshot before acting, and again after anything "
                "changes. Use it instead of the older send_app_keystrokes / click_app_menu_item "
                "when you need to read or operate controls inside an app; for a web page use "
                "browser_snapshot. Example: app_name='TextEdit'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "app_name": {"type": "string", "description": "App name or bundle id, e.g. TextEdit or com.apple.Music."},
                    "show_all": {"type": "boolean", "default": False, "description": "Include unlabeled layout groups too."},
                },
                "required": ["app_name"],
            },
            handler=_guard(_snapshot_tool),
        ),
        ToolSpec(
            name="app_driver_stop",
            description="Engage the kill switch: stop all app driving at once. Only the owner can resume it.",
            parameters={"type": "object", "properties": {"reason": {"type": "string"}}},
            handler=_guard(_stop_tool),
        ),
        ToolSpec(
            name="app_driver_click",
            description=(
                "Press a button, menu item, checkbox or other control by snapshot_id and "
                "element_id from a fresh app_driver_snapshot. Refused if the interface changed "
                "since the snapshot: take a new one and retry. Asks the owner first."
            ),
            parameters={"type": "object", "properties": {**_target_props(), "dry_run": _DRY_RUN_PROP}, "required": ["snapshot_id", "element_id"]},
            handler=_guard(_act("click")),
            permission=gated,
            confirm_prompt=lambda a: f"Let the model click {driver.describe(a.get('snapshot_id', ''), a.get('element_id', ''))}?",
        ),
        ToolSpec(
            name="app_driver_type",
            description=(
                "Focus a text field by snapshot_id and element_id from a fresh "
                "app_driver_snapshot and type literal text into it. Refused for password "
                "fields, and for any text that looks like a password, key or token. Asks the "
                "owner first. For a web page use browser_type."
            ),
            parameters={
                "type": "object",
                "properties": {**_target_props(), "text": {"type": "string"}, "dry_run": _DRY_RUN_PROP},
                "required": ["snapshot_id", "element_id", "text"],
            },
            handler=_guard(_act("type")),
            permission=gated,
            confirm_prompt=lambda a: (
                f"Let the model type {str(a.get('text', ''))[:80]!r} into "
                f"{driver.describe(a.get('snapshot_id', ''), a.get('element_id', ''))}?"
            ),
        ),
        ToolSpec(
            name="app_driver_press_key",
            description=(
                "Bring an allowed app to the front and press one named key (return, enter, tab, "
                "escape, delete, space, up, down, left, right) with optional modifiers "
                "(command, option, shift, control). Asks the owner first. For a key on a web "
                "page use browser_press."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "app_name": {"type": "string"},
                    "key": {"type": "string"},
                    "modifiers": {"type": "array", "items": {"type": "string"}, "default": []},
                    "dry_run": _DRY_RUN_PROP,
                },
                "required": ["app_name", "key"],
            },
            handler=_guard(_press_tool),
            permission=gated,
            confirm_prompt=lambda a: f"Let the model press {'+'.join(list(a.get('modifiers') or []) + [str(a.get('key', '?'))])} in {a.get('app_name', '?')}?",
        ),
        ToolSpec(
            name="app_driver_scroll",
            description=(
                "Scroll the scroll area that contains an element (or is that element) by a "
                "fraction of its range, using snapshot_id and element_id from a fresh "
                "app_driver_snapshot. It scrolls Mac apps, not web pages. Asks the owner first."
            ),
            parameters={
                "type": "object",
                "properties": {
                    **_target_props(),
                    "direction": {"type": "string", "enum": ["up", "down", "left", "right"], "default": "down"},
                    "amount": {"type": "number", "default": 0.25, "description": "Fraction of the scroll range, above 0 and at most 1."},
                    "dry_run": _DRY_RUN_PROP,
                },
                "required": ["snapshot_id", "element_id"],
            },
            handler=_guard(_act("scroll")),
            permission=gated,
            confirm_prompt=lambda a: f"Let the model scroll {a.get('direction', 'down')} at {driver.describe(a.get('snapshot_id', ''), a.get('element_id', ''))}?",
        ),
    ]


#: Names the main thread adds to its confirmation bookkeeping, if it keeps one.
CONFIRMATION_TOOL_NAMES = ("app_driver_click", "app_driver_type", "app_driver_press_key", "app_driver_scroll")
