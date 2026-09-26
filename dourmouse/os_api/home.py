"""Backend for the OS shell's chrome and its HOME screen (finding #144).

Three routes, all behind the auth gate and request guard like every route in
this package:

* ``GET /api/os/screens``: which screen folders exist under
  ``ui/assets/os/screens/``. The router asks before importing a screen, so a
  screen that is not built yet is reported as such without the browser logging
  a 404 for a module that was never there.
* ``GET /api/os/session?tab_id=``: the conversation of ONE tab, read back from
  its own session ledger. ``/api/session/current`` reads the shared server
  session, which is not what a tab with its own tab id talks to. A tab with no
  conversation yet answers 200 with no turns (a 404 would make the browser log
  a console error for an ordinary empty state).
* ``POST /api/os/session/new {tab_id}``: NEW THREAD. Forgets the tab's server
  session, so the next message starts a fresh ChatSession with a fresh ledger
  file. The old file stays on disk untouched. Refused while a turn is running.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import ApiError, Request, route

_SLUG = re.compile(r"^[a-z0-9_-]{1,64}$")
_TAB = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")


def _screens_dir() -> Path:
    from dourmouse.webui import _UI_DIR

    return _UI_DIR / "assets" / "os" / "screens"


def built_screens(root: Path | None = None) -> list[str]:
    base = root if root is not None else _screens_dir()
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir() and _SLUG.match(p.name) and (p / "index.js").is_file())


@route("GET", "/api/os/screens")
def screens(req: Request) -> tuple[int, dict[str, Any]]:
    return 200, {"ok": True, "built": built_screens()}


def _tab(req: Request, value: str) -> str:
    tab = (value or "").strip()
    if not tab:
        raise ApiError(400, "tab_id is required")
    if not _TAB.match(tab):
        raise ApiError(400, "tab_id has characters that are not allowed")
    return tab


@route("GET", "/api/os/session")
def session(req: Request) -> tuple[int, dict[str, Any]]:
    tab = _tab(req, req.arg("tab_id"))
    srv = req.server
    live = getattr(srv, "sessions_by_tab", {}).get(tab)
    if live is None:
        return 200, {"ok": True, "id": None, "turns": [], "note": "No conversation has started in this tab yet."}
    result = srv.get_session_transcript(Path(live.session_file).stem)
    if not result.get("ok"):
        # the session exists but has written no turn yet, so there is no ledger file
        return 200, {"ok": True, "id": Path(live.session_file).stem, "turns": [], "note": "No turn has been recorded yet."}
    return 200, result


@route("POST", "/api/os/session/new")
def new_session(req: Request) -> tuple[int, dict[str, Any]]:
    tab = _tab(req, str(req.body.get("tab_id") or ""))
    srv = req.server
    with srv.tab_state_lock:
        live = srv.sessions_by_tab.get(tab)
        if live is None:
            return 200, {"ok": True, "previous": None, "note": "This tab had no conversation to close."}
        lock = srv.locks_by_tab.get(tab)
        if lock is not None and lock.locked():
            raise ApiError(409, "A turn is still running in this tab. Stop it first, then start a new thread.")
        previous = Path(live.session_file).stem
        srv.sessions_by_tab.pop(tab, None)
        srv.gates_by_tab.pop(tab, None)
        srv.locks_by_tab.pop(tab, None)
    return 200, {"ok": True, "previous": previous}
