"""Backlog #8 — the embedded browser pane's real trigger mechanism.

The one thing dourmouse/browser_agent.py's real Playwright engine has never
had: a way for the LLM to open something the HUMAN can actually see, inline
in the app's own window (not a second OS window, not headless automation).

Architecture, reconciled with what pywebview can actually do (see
docs/browser_pane_architecture.md for the full CDP/WKWebView constraint):
an <iframe>-based pane embedded directly in the page (console.html), one
shared instance reused across every open (the "Global Panel Manager" from
the user's own spec) rather than one per tab. This module is the trigger:
a tool call (open_browser_pane) posts a request here; webui.py observes it
and rebroadcasts over the SAME real SSE hub (/api/events) console.html
already keeps open at all times (see startNewsStream()), so the frontend
just needs one more `data.type` case, not a second connection.

Same real, tested, thread-safe singleton pattern as message_bus.py
(get_message_bus/set_message_bus/on_post) — deliberately, for the same
reason: tool handlers in general_roster.py are pure functions with no
reference to the live HTTP server object, so a module-level observable is
the real bridge, not a guess.
"""

from __future__ import annotations

import threading
import urllib.error
import urllib.request
from typing import Any, Callable

_CHECK_TIMEOUT = 5.0


def check_frameable(url: str) -> dict[str, Any]:
    """Real bug found live-testing the pane: the frontend's only signal
    was the iframe's `load` event, and `load` fires even when a site's
    X-Frame-Options/CSP frame-ancestors headers block it from actually
    rendering (the request still succeeds — it's a *display* refusal,
    not a network failure) — so a blocked site left the pane showing a
    permanently blank iframe with the fallback/escape-hatch never
    shown. There is no reliable way to detect that from inside the
    iframe itself (cross-origin), so this checks it the one place it
    IS visible: the real HTTP response headers, fetched server-side
    before the frontend ever commits to the iframe.

    Honest on every branch: a header that blocks embedding -> not
    frameable, with the real reason quoted back. Anything else
    (the header allows it, or this check itself couldn't complete —
    network error, timeout, non-2xx) -> frameable stays True, so a
    site this check can't be sure about still gets a real chance
    to load, with the existing client-side timeout as the last-resort
    safety net for genuine network hangs it was already built for.
    """
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Dourmouse/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=_CHECK_TIMEOUT) as resp:
            headers = resp.headers
    except urllib.error.HTTPError as exc:
        # A real response with headers, just a non-2xx status (some sites
        # 405 a bare HEAD) — the headers are still real signal.
        headers = exc.headers
    except Exception as exc:  # noqa: BLE001 - honest: couldn't check, don't block on a guess
        return {"frameable": True, "reason": f"could not check: {exc}", "checked": False}

    xfo = (headers.get("X-Frame-Options") or "").strip().upper()
    if xfo in ("DENY", "SAMEORIGIN"):
        return {
            "frameable": False,
            "reason": f"X-Frame-Options: {xfo}",
            "checked": True,
        }

    csp = headers.get("Content-Security-Policy") or ""
    for directive in csp.split(";"):
        directive = directive.strip()
        if directive.lower().startswith("frame-ancestors"):
            sources = directive.split()[1:]
            # 'none' or anything that isn't a wildcard/'self' means this
            # origin (an arbitrary localhost dev port) is not allowed —
            # the common real-world case is an explicit allowlist of the
            # site's own domains, which never includes ours.
            if sources and not any(s in ("*", "'self'") for s in sources):
                return {
                    "frameable": False,
                    "reason": f"Content-Security-Policy: {directive}",
                    "checked": True,
                }
            if sources == ["'none'"]:
                return {
                    "frameable": False,
                    "reason": "Content-Security-Policy: frame-ancestors 'none'",
                    "checked": True,
                }

    return {"frameable": True, "reason": "no blocking header found", "checked": True}


class BrowserPaneRequests:
    """Thread-safe fan-out: request_open() is called from a tool handler
    (no server reference available there); on_request() lets webui.py's
    run_server() subscribe once and rebroadcast over the real SSE hub."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._observers: list[Callable[[dict[str, Any]], None]] = []

    def on_request(self, fn: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            self._observers.append(fn)

    def request_open(self, url: str) -> dict[str, Any]:
        event = {"type": "browser_pane_open", "url": url}
        with self._lock:
            observers = list(self._observers)
        for fn in observers:
            try:
                fn(event)
            except Exception:
                # An observer must never break the tool call that
                # triggered it (same principle as message_bus's own
                # on_post — a broken UI bridge is not a broken turn).
                pass
        return event


_singleton: BrowserPaneRequests | None = None
_singleton_lock = threading.Lock()


def get_browser_pane_requests() -> BrowserPaneRequests:
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = BrowserPaneRequests()
        return _singleton


def set_browser_pane_requests(instance: BrowserPaneRequests | None) -> None:
    """Test isolation hook — same shape as message_bus.set_message_bus."""
    global _singleton
    with _singleton_lock:
        _singleton = instance
