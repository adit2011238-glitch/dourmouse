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
"""

from __future__ import annotations

from typing import Any

from . import Request, route

_MAX_URL = 500


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
