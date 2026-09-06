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
from typing import Any, Callable


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
