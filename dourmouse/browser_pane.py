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

from dourmouse.net_guard import guarded_urlopen

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
        with guarded_urlopen(req, timeout=_CHECK_TIMEOUT) as resp:
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
#   - Only real text/html responses are rewritten. Anything else (a PDF,
#     an image, JSON) is returned as an honest failure — proxying content
#     that was never going to render as a framed *document* in the first
#     place buys nothing.
#   - External JS files (anything loaded via <script src="...">) are
#     never fetched or rewritten — only the INLINE script text already
#     present in the fetched document. A site whose frame-busting logic
#     lives entirely in a bundled external script is not caught by
#     _neutralize_frame_busting below; only inline busting checks are.
#   - The navigation shim (_NAV_INTERCEPT_SCRIPT) only catches real
#     top-level navigation it can actually see: <a href> clicks,
#     window.open(...) calls, and GET form submits (rewritten as a
#     proxied query string). A POST form submits natively, unproxied
#     (forwarding a real request body through this proxy is real scope
#     not attempted here) — it will re-hit the same framing wall the
#     GET case exists to route around. A script-driven navigation via a
#     raw `location.href = ...`/`location.assign(...)` assignment is
#     also not caught (there is no reliable, side-effect-free way to
#     intercept a plain property write on `window.location` in a real
#     browser) — that link still navigates the iframe directly and can
#     re-hit the same wall with no automatic re-proxy.
# --------------------------------------------------------------------------- #

PROXY_MAX_BYTES = 5_000_000
PROXY_TIMEOUT = 10.0

_BASE_TAG_RE = re.compile(rb"<head\b[^>]*>", re.IGNORECASE)
_HTML_TAG_RE = re.compile(rb"<html\b[^>]*>", re.IGNORECASE)

# 2026-09-14 (feature 4, fix #1) — real, live-reported problem: "a lot of
# pages refuse embedding or proxy". The <base> tag above only fixes
# *resolution* of relative URLs; it does nothing about a page's own
# inline script actively fighting being framed (`if (top !== self)
# top.location = self.location;` and its many textual variants — very
# common on real sites, completely separate from the X-Frame-Options/CSP
# headers check_frameable() already detects). A real browser CANNOT
# reliably override `window.top`'s getter here (Chrome/Safari/Firefox
# all define it non-configurable — `Object.defineProperty(window, "top",
# ...)` throws), so runtime property overriding is not an option. The
# real, standard technique other HTML-rewriting proxies use instead:
# textually neutralize the common busting expressions in the fetched
# HTML BEFORE it is ever parsed/executed, so the busting script's own
# condition can never observe `top !== self` as true. Best-effort and
# honestly imperfect (only catches these literal, common phrasings, and
# only in INLINE script — see the module-level limitations comment
# above) — real sites vary this code arbitrarily, and a determined bust
# script can still get around simple text substitution. Ordered
# longest/most-specific first so an earlier substitution never partially
# consumes text a later one needs to match.
_FRAME_BUST_REPLACEMENTS: list[tuple[bytes, bytes]] = [
    (rb"top\s*!==\s*self", b"false"),
    (rb"self\s*!==\s*top", b"false"),
    (rb"top\s*!=\s*self", b"false"),
    (rb"self\s*!=\s*top", b"false"),
    (rb"window\.top\s*!==\s*window\.self", b"false"),
    (rb"window\.self\s*!==\s*window\.top", b"false"),
    (rb"window\.top\s*!=\s*window\.self", b"false"),
    (rb"window\.self\s*!=\s*window\.top", b"false"),
    (rb"top\.location", b"self.location"),
    (rb"parent\.location", b"self.location"),
    (rb"window\.top\b", b"window.self"),
    (rb"window\.parent\b", b"window.self"),
]
_FRAME_BUST_RES = [
    (re.compile(pattern), replacement) for pattern, replacement in _FRAME_BUST_REPLACEMENTS
]


def _neutralize_frame_busting(html_bytes: bytes) -> bytes:
    """Best-effort textual neutralization of common inline frame-busting
    code — see the real, disclosed limitations right above
    _FRAME_BUST_REPLACEMENTS for exactly what this does and does not
    catch."""
    for compiled, replacement in _FRAME_BUST_RES:
        html_bytes = compiled.sub(replacement, html_bytes)
    return html_bytes


# 2026-09-14 (feature 4, fix #2) — real, live-reported problem: the
# original proxy only ever handled the FIRST page load; the very next
# click inside it navigated the iframe directly at the real site (via
# the injected <base>), bypassing the proxy entirely and re-hitting the
# same framing wall if that next page also refuses to be framed. This
# script, injected right after <base>, intercepts the navigation paths
# it actually CAN see (real <a href> clicks, window.open, GET form
# submits) and routes them back through /api/browser-pane/proxy — see
# the module-level limitations comment above for exactly what this
# does NOT catch (POST forms, a raw `location.href = ...` assignment).
# Written mostly as plain, old-style JS (var, no arrow functions/
# template literals/optional chaining) so it runs unmodified on
# whatever engine the target page's own <!doctype> happens to trigger,
# including a real quirks-mode/legacy page — this script must never be
# the reason a page breaks. FormData/for-of (both broadly supported in
# every real engine this pane can realistically run in) are the one
# exception, used for spec-correct form-value reading rather than a
# hand-rolled field walk.
_NAV_INTERCEPT_SCRIPT_TEMPLATE = """<script>(function(){
  // Real, live-caught bug: a RELATIVE path here resolves against the
  // base tag injected right before this script -- i.e. against the
  // PROXIED SITE's own origin, not ours. A real click-through test
  // proved it: window.location.href = "/api/browser-pane/proxy?..."
  // actually navigated to "https://www.google.com/api/browser-pane/
  // proxy?..." (a real 404 on Google's own domain), because that base
  // tag repoints ALL relative resolution to the target site -- exactly
  // what it's there to do for the PAGE's own links, but it breaks a
  // same-document relative reference back to OUR server just the
  // same. window.location.origin, read here before any navigation has
  // happened, is still this real server's own origin (the base tag
  // only affects resolution, not the document's actual current
  // location) -- capturing it into an ABSOLUTE proxy URL is the fix.
  var PROXY = window.location.origin + "/api/browser-pane/proxy?url=";
  function toProxyUrl(href){
    var abs;
    try { abs = new URL(href, document.baseURI).href; }
    catch (e) { return null; }
    if (abs.slice(0, 5) !== "http:" && abs.slice(0, 6) !== "https:") { return null; }
    return PROXY + encodeURIComponent(abs);
  }
  function closestAnchor(node){
    while (node && node.tagName !== "A") { node = node.parentNode; }
    return node;
  }
  document.addEventListener("click", function(e){
    var a = closestAnchor(e.target);
    if (!a) { return; }
    var href = a.getAttribute("href");
    if (!href || href.charAt(0) === "#") { return; }
    var dest = toProxyUrl(a.href);
    if (!dest) { return; }
    e.preventDefault();
    window.location.href = dest;
  }, true);
  document.addEventListener("submit", function(e){
    var f = e.target;
    if (!f || f.tagName !== "FORM") { return; }
    var method = (f.getAttribute("method") || "get").toLowerCase();
    if (method !== "get") { return; }
    var action = f.getAttribute("action") || document.baseURI;
    var abs;
    try { abs = new URL(action, document.baseURI); }
    catch (e2) { return; }
    // FormData(form) is the real, spec-correct way to read a form's
    // submitted values -- it respects disabled fields and
    // unchecked checkboxes/radios automatically, unlike a plain
    // querySelectorAll walk (which would submit every field
    // regardless of whether the browser actually would).
    var data = new FormData(f);
    for (var pair of data.entries()) {
      if (typeof pair[1] === "string") { abs.searchParams.set(pair[0], pair[1]); }
    }
    e.preventDefault();
    window.location.href = PROXY + encodeURIComponent(abs.href);
  }, true);
  var realOpen = window.open;
  window.open = function(url){
    if (url) {
      var dest = toProxyUrl(url);
      if (dest) { window.location.href = dest; return null; }
    }
    return realOpen.apply(window, arguments);
  };
})();</script>"""


def _inject_base_tag(html_bytes: bytes, url: str) -> bytes:
    """Insert <base href="url"> plus the navigation-intercept shim
    (_NAV_INTERCEPT_SCRIPT_TEMPLATE) as the very first thing inside
    <head> (or right after <html> if there's no head, or right at the
    start for genuinely malformed markup) so every relative URL the
    page already uses resolves against the real site, and every real
    navigation it can see routes back through the proxy. Inserting
    first matters: if the page already has its own <base>, only the
    FIRST <base> in document order takes effect, and this must win —
    same reasoning for the shim running before any of the page's own
    scripts get a chance to act on a click."""
    injected = (f'<base href="{url}">'.encode("utf-8")) + _NAV_INTERCEPT_SCRIPT_TEMPLATE.encode(
        "utf-8"
    )
    m = _BASE_TAG_RE.search(html_bytes)
    if m:
        return html_bytes[: m.end()] + injected + html_bytes[m.end() :]
    m = _HTML_TAG_RE.search(html_bytes)
    if m:
        return html_bytes[: m.end()] + injected + html_bytes[m.end() :]
    return injected + html_bytes


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
        with guarded_urlopen(req, timeout=PROXY_TIMEOUT) as resp:
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
    return {"ok": True, "body": _inject_base_tag(_neutralize_frame_busting(body), url)}


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
