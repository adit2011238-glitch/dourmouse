"""Backend for the OS shell's BROWSER screen (finding #153).

The pane itself needs no new route: it is driven through the Electron bridge
(``window.dourmouseShell.pane``), and outside Electron through the existing
``/api/browser-pane/proxy``. The one thing the screen could not read anywhere
was whether the browser agent is attached, which the mockup's "Dourmouse is
watching this tab" strip claims.

* ``GET /api/os/browser/attached``: whether ``browser_agent`` holds a live page
  right now, the address of that page, the engine state and the agent's last
  logged action. It never launches a browser, never navigates and never reads
  page content; ``browser_agent.browser_status()`` hard-wires ``"page": None``,
  so the live page object is read here instead.

Phase B1 (Chrome parity part 1) adds the browser's own records. They live in the
Electron shell, in its userData folder, because the shell is what browses; this
server is only a door to them. Each route below forwards to the pane bridge that
``electron/main.js`` serves on ``DOURMOUSE_ELECTRON_PANE_PORT`` (a loopback port the
shell passes to the server it spawns), so outside the Electron app they answer 503
with the reason and invent nothing:

* ``GET /api/os/browser/tabs`` and ``POST .../tabs/new|close|select|reopen``
* ``GET /api/os/browser/downloads`` and ``POST .../downloads/cancel`` (opening or
  revealing a downloaded file is the console's own explicit action, never a route here)
* ``GET /api/os/browser/history`` (``q``, ``limit``) and ``POST .../history/add|remove|clear``
* ``GET /api/os/browser/bookmarks`` and ``POST .../bookmarks/add|remove``
"""

from __future__ import annotations

import http.client
import json
import os
from typing import Any
from urllib.parse import urlencode

from . import ApiError, Request, route

_MAX_URL = 500
_BRIDGE_TIMEOUT = 5.0
_PANE_PORT_ENV = "DOURMOUSE_ELECTRON_PANE_PORT"
_MAX_FIELD = 8192


def _live_page_url() -> tuple[bool, str]:
    """(attached, url) for the agent's page, without touching the browser."""
    from dourmouse import browser_agent

    page = getattr(browser_agent, "_PAGE", None)
    if page is None:
        return False, ""
    try:
        if page.is_closed():
            return False, ""
    except Exception:  # noqa: BLE001 - a page object we cannot query is not attached
        return False, ""
    try:
        url = str(page.url or "")
    except Exception:  # noqa: BLE001 - attached, but the address is unreadable
        url = ""
    return True, url[:_MAX_URL]


@route("GET", "/api/os/browser/attached")
def attached(req: Request) -> tuple[int, dict[str, Any]]:
    from dourmouse import browser_agent

    status = browser_agent.browser_status()
    is_attached, url = _live_page_url()
    activity = status.get("activity") or []
    last = activity[-1] if activity else None
    return 200, {
        "ok": True,
        "attached": is_attached,
        "page_url": "" if url == "about:blank" else url,
        "engine": str(status.get("engine") or ""),
        "engine_ready": bool(status.get("ready")),
        "headless": bool(status.get("headless")),
        "launch_error": status.get("launch_error") or None,
        "last": (
            {"at": str(last.get("at") or ""), "kind": str(last.get("kind") or ""), "text": str(last.get("text") or "")[:300]}
            if isinstance(last, dict)
            else None
        ),
    }


# --------------------------------------------------------------------------- #
# The browser's own records (Phase B1), forwarded to the Electron pane bridge.
# --------------------------------------------------------------------------- #


def _bridge_port() -> int:
    raw = os.environ.get(_PANE_PORT_ENV, "").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= 65535:
        raise ApiError(503, "the browser records live in the Electron app, and this server was not started by it")
    return int(raw)


def _bridge(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """One loopback call to the pane bridge. The bridge refuses browser-originated
    requests, so this sends neither an Origin nor a Sec-Fetch-Site header."""
    port = _bridge_port()
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=_BRIDGE_TIMEOUT)
    try:
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read(4_000_000)
    except (OSError, http.client.HTTPException) as exc:
        raise ApiError(503, f"the Electron browser pane did not answer: {exc}") from exc
    finally:
        conn.close()
    try:
        data = json.loads(raw or b"{}")
    except ValueError as exc:
        raise ApiError(502, "the Electron browser pane sent something that is not JSON") from exc
    if not isinstance(data, dict):
        raise ApiError(502, "the Electron browser pane sent an unexpected answer")
    if resp.status >= 400 or data.get("ok") is False:
        status = resp.status if resp.status in (400, 404, 409) else 502
        raise ApiError(status, str(data.get("error") or f"the browser pane answered HTTP {resp.status}"))
    return data


def _text(req: Request, name: str, *, required: bool = False) -> str | None:
    value = req.body.get(name)
    if value is None or value == "":
        if required:
            raise ApiError(400, f"{name} is required")
        return None
    if not isinstance(value, str) or len(value) > _MAX_FIELD:
        raise ApiError(400, f"{name} must be a string of at most {_MAX_FIELD} characters")
    return value


def _tab_id(req: Request, *, required: bool) -> int | None:
    value = req.body.get("id")
    if value is None:
        if required:
            raise ApiError(400, "id is required")
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ApiError(400, "id must be a positive whole number")
    return value


def _pick(data: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {"ok": True, **{k: data[k] for k in keys if k in data}}


@route("GET", "/api/os/browser/tabs")
def tabs(req: Request) -> tuple[int, dict[str, Any]]:
    return 200, _pick(_bridge("GET", "/tabs"), "tabs", "active", "closedTabs")


@route("POST", "/api/os/browser/tabs/new")
def tabs_new(req: Request) -> tuple[int, dict[str, Any]]:
    body: dict[str, Any] = {}
    url = _text(req, "url")
    if url:
        body["url"] = url
    if req.body.get("background") is True:
        body["background"] = True
    return 200, _pick(_bridge("POST", "/tabs/new", body), "id")


@route("POST", "/api/os/browser/tabs/close")
def tabs_close(req: Request) -> tuple[int, dict[str, Any]]:
    tab = _tab_id(req, required=False)
    return 200, _pick(_bridge("POST", "/tabs/close", {"id": tab} if tab else {}))


@route("POST", "/api/os/browser/tabs/select")
def tabs_select(req: Request) -> tuple[int, dict[str, Any]]:
    return 200, _pick(_bridge("POST", "/tabs/select", {"id": _tab_id(req, required=True)}))


@route("POST", "/api/os/browser/tabs/reopen")
def tabs_reopen(req: Request) -> tuple[int, dict[str, Any]]:
    return 200, _pick(_bridge("POST", "/tabs/reopen", {}), "id")


@route("GET", "/api/os/browser/downloads")
def downloads(req: Request) -> tuple[int, dict[str, Any]]:
    return 200, _pick(_bridge("GET", "/downloads"), "downloads")


@route("POST", "/api/os/browser/downloads/cancel")
def downloads_cancel(req: Request) -> tuple[int, dict[str, Any]]:
    return 200, _pick(_bridge("POST", "/downloads/cancel", {"id": _text(req, "id", required=True)}))


@route("GET", "/api/os/browser/history")
def history(req: Request) -> tuple[int, dict[str, Any]]:
    query = "/history"
    params = {k: req.arg(k) for k in ("q", "limit") if req.arg(k)}
    if params:
        query += "?" + urlencode(params)
    return 200, _pick(_bridge("GET", query), "history")


@route("POST", "/api/os/browser/history/add")
def history_add(req: Request) -> tuple[int, dict[str, Any]]:
    body = {"url": _text(req, "url", required=True), "title": _text(req, "title") or ""}
    return 200, _pick(_bridge("POST", "/history/add", body), "entry")


@route("POST", "/api/os/browser/history/remove")
def history_remove(req: Request) -> tuple[int, dict[str, Any]]:
    return 200, _pick(_bridge("POST", "/history/remove", _selector(req)), "removed")


@route("POST", "/api/os/browser/history/clear")
def history_clear(req: Request) -> tuple[int, dict[str, Any]]:
    return 200, _pick(_bridge("POST", "/history/clear", {}), "removed")


@route("GET", "/api/os/browser/bookmarks")
def bookmarks(req: Request) -> tuple[int, dict[str, Any]]:
    return 200, _pick(_bridge("GET", "/bookmarks"), "bookmarks")


@route("POST", "/api/os/browser/bookmarks/add")
def bookmarks_add(req: Request) -> tuple[int, dict[str, Any]]:
    body = {"url": _text(req, "url", required=True), "title": _text(req, "title") or ""}
    return 200, _pick(_bridge("POST", "/bookmarks/add", body), "added", "bookmark")


@route("POST", "/api/os/browser/bookmarks/remove")
def bookmarks_remove(req: Request) -> tuple[int, dict[str, Any]]:
    return 200, _pick(_bridge("POST", "/bookmarks/remove", _selector(req)), "removed")


def _selector(req: Request) -> dict[str, Any]:
    """Which record: by id, or failing that by address."""
    ident = _text(req, "id")
    if ident:
        return {"id": ident}
    return {"url": _text(req, "url", required=True)}
