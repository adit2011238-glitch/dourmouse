"""Backend for the Ask panel (plan 4.9, PL-04).

* ``GET  /api/os/ask/settings``: the panel's settings and which modes work today.
* ``POST /api/os/ask/settings``: change them. A shortcut macOS or this app already uses is refused with who has it.
* ``POST /api/os/ask/note {text}``: keep a note in the user wiki (mode ``n``).
* ``POST /api/os/ask/remind {text}``: set a reminder through Automations (mode ``r``). The owner typed it, so no
  second approval is asked; a reminder only shows a notification.

Asking a question uses the ordinary ``POST /api/chat`` on a ``quick:`` tab, so the same gates apply and nothing from
it is kept. Do mode (``/``) needs Skills (PL-05), which is not built, so it says so.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dourmouse import ask_panel

from . import ApiError, Request, route

DO_AVAILABLE = False


def _path() -> Path:
    from dourmouse.config import workspace_dir

    return Path(workspace_dir()) / "ask_panel.json"


@route("GET", "/api/os/ask/settings")
def get_settings(req: Request) -> tuple[int, dict[str, Any]]:
    settings = ask_panel.load(_path())
    return 200, {"ok": True, "settings": settings, "hotkey_label": ask_panel.pretty_hotkey(settings["hotkey"]),
                 "modes": {"ask": True, "note": True, "remind": True, "do": DO_AVAILABLE}}


@route("POST", "/api/os/ask/settings")
def set_settings(req: Request) -> tuple[int, dict[str, Any]]:
    try:
        merged = ask_panel.clean_settings(req.body, ask_panel.load(_path()))
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None
    if merged["default_mode"] == "do" and not DO_AVAILABLE:
        raise ApiError(400, "Do mode needs Skills, which are not built yet")
    ask_panel.save(_path(), merged)
    return 200, {"ok": True, "settings": merged, "hotkey_label": ask_panel.pretty_hotkey(merged["hotkey"])}


@route("POST", "/api/os/ask/note")
def note(req: Request) -> tuple[int, dict[str, Any]]:
    try:
        saved = ask_panel.save_note(str(req.body.get("text") or ""))
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None
    return 200, {"ok": True, "note": saved}


@route("POST", "/api/os/ask/remind")
def remind(req: Request) -> tuple[int, dict[str, Any]]:
    from . import automations

    text = " ".join(str(req.body.get("text") or "").split())
    if not text:
        raise ApiError(400, "say what to remind you of and when, for example: call the bank tomorrow at 9")
    inner = Request(server=req.server, body={"sentence": "remind me " + text, "approve": True}, user=req.user)
    status, payload = automations.create(inner)
    row = payload["automation"]
    return status, {"ok": True, "reminder": {"id": row["id"], "name": row["name"], "when": row["next_run"] or row["trigger"]}}
