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

import re
import threading
import urllib.error
import urllib.request
from typing import Any, Callable

_CHECK_TIMEOUT = 5.0

# Real, live-found reason this is a normal browser UA string rather than
# something honest like "Dourmouse/1.0": that honest string is exactly
# what got a real proxy request 403'd by a real site's bot detection
# during this session's own live testing (a plain "not a browser" UA is
# one of the cheapest, most common bot-detection signals to trip). This
# doesn't defeat real bot detection (TLS fingerprinting, JS challenges,
# rate limits) -- it just stops failing on the simplest, UA-string-only
# checks, which is genuinely most of what's out there.
_FETCH_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


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
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": _FETCH_USER_AGENT})
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


# --------------------------------------------------------------------------- #
# Rewriting proxy — the real fix for "most sites refuse to be framed at
# all", not just an honest fallback for it. check_frameable() above
# decides WHETHER a site can go straight into the iframe; this is what
# runs when it can't.
#
# The one thing that actually blocks framing is a *header*
# (X-Frame-Options / CSP frame-ancestors), applied only to the top-level
# document load — it does not apply to the subresources a page pulls in
# (its own JS/CSS/images/XHR). So the fix does not need to rewrite every
# URL in the page: fetch the document server-side (so the header this
# server sends back is OURS, not theirs), and inject a single <base
# href="..."> pointing at the real URL so every relative link/asset the
# page already references resolves against the REAL site, exactly as it
# would have unproxied. Everything else about the page — its own script,
# its own fetch calls to its own API — keeps working unmodified.
#
# Real, disclosed limitations (Rule 2.2 — say what's lost, don't pretend
# it's a perfect mirror):
#   - Fetched server-side, so none of the browser's own cookies for that
#     site are sent — a page that requires the user's own login session
#     will render logged-out, same honest tradeoff as the sandbox fix
#     above.
#   - Only the exact requested URL is proxied. A link the user clicks
#     INSIDE the proxied page navigates the iframe directly to the real
#     site (via the injected <base>), bypassing the proxy — if that next
#     page also blocks framing, it fails again with no automatic re-proxy
#     (fully covering in-page navigation would mean rewriting every link/
#     form/JS-driven navigation recursively, real scope this stays
#     honest about not attempting).
#   - Only real text/html responses are rewritten. Anything else (a PDF,
#     an image, JSON) is returned as an honest failure — proxying content
#     that was never going to render as a framed *document* in the first
#     place buys nothing.
# --------------------------------------------------------------------------- #

PROXY_MAX_BYTES = 5_000_000
PROXY_TIMEOUT = 10.0

_BASE_TAG_RE = re.compile(rb"<head\b[^>]*>", re.IGNORECASE)
_HTML_TAG_RE = re.compile(rb"<html\b[^>]*>", re.IGNORECASE)


def _inject_base_tag(html_bytes: bytes, url: str) -> bytes:
    """Insert <base href="url"> as the very first thing inside <head>
    (or right after <html> if there's no head, or right at the start for
    genuinely malformed markup) so every relative URL the page already
    uses resolves against the real site. Inserting first matters: if the
    page already has its own <base>, only the FIRST <base> in document
    order takes effect, and this must win."""
    base_tag = f'<base href="{url}">'.encode("utf-8")
    m = _BASE_TAG_RE.search(html_bytes)
    if m:
        return html_bytes[: m.end()] + base_tag + html_bytes[m.end() :]
    m = _HTML_TAG_RE.search(html_bytes)
    if m:
        return html_bytes[: m.end()] + base_tag + html_bytes[m.end() :]
    return base_tag + html_bytes


def _honest_proxy_error_page(url: str, reason: str) -> bytes:
    """A real HTML document (not a JSON error) — the proxy endpoint always
    returns 200 with real content so the iframe's `load` event means what
    it says, never a heuristic-dependent blank page."""
    from html import escape

    return (
        "<!doctype html><html><head><meta charset=\"utf-8\"></head>"
        '<body style="font:14px system-ui;padding:24px;color:#333">'
        f"<p>Couldn't load this page through the proxy: {escape(reason)}</p>"
        f'<p><a href="{escape(url)}" target="_blank" rel="noopener">Open it directly</a> instead.</p>'
        "</body></html>"
    ).encode("utf-8")


def fetch_and_rewrite_for_proxy(url: str) -> dict[str, Any]:
    """Real fetch + <base>-injection rewrite for one URL. Always returns
    real HTML bytes to serve with a 200 — see _honest_proxy_error_page
    for why failures are still a real document, not a bare error code."""
    req = urllib.request.Request(url, headers={"User-Agent": _FETCH_USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=PROXY_TIMEOUT) as resp:
            content_type = (resp.headers.get("Content-Type") or "").lower()
            if "text/html" not in content_type:
                return {
                    "ok": False,
                    "body": _honest_proxy_error_page(
                        url, f"not an HTML page (Content-Type: {content_type or 'unknown'})"
                    ),
                }
            body = resp.read(PROXY_MAX_BYTES + 1)
    except Exception as exc:  # noqa: BLE001 - honest: the error page IS the result
        return {"ok": False, "body": _honest_proxy_error_page(url, str(exc))}
    if len(body) > PROXY_MAX_BYTES:
        return {
            "ok": False,
            "body": _honest_proxy_error_page(url, f"page exceeds {PROXY_MAX_BYTES:,} byte proxy limit"),
        }
    return {"ok": True, "body": _inject_base_tag(body, url)}


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
