"""Tests for dourmouse/browser_pane.py — the embedded browser pane's
real trigger bridge (backlog #8). Same shape as test_message_bus.py's
own MessageBus tests, deliberately (same real singleton pattern)."""

from __future__ import annotations

import urllib.error

import pytest

from dourmouse import browser_pane
from dourmouse.browser_pane import (
    BrowserPaneRequests,
    check_frameable,
    get_browser_pane_requests,
    set_browser_pane_requests,
)


class _FakeHeaders(dict):
    def get(self, key, default=None):
        # Real HTTP headers are case-insensitive; email.message.Message
        # (what urlopen actually returns) already behaves this way —
        # mirror that here so the tests exercise the real lookup shape.
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


class _FakeResponse:
    def __init__(self, headers, body: bytes = b""):
        self.headers = _FakeHeaders(headers)
        self._body = body

    def read(self, n: int = -1) -> bytes:
        return self._body if n < 0 else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestCheckFrameable:
    """Real bug found live-testing the embedded pane: X-Frame-Options-
    blocked sites fired the iframe's `load` event anyway (the request
    succeeds — only rendering is refused), leaving the pane blank with
    no fallback. check_frameable() catches this server-side, before the
    frontend ever commits to the iframe."""

    def test_no_blocking_header_is_frameable(self, monkeypatch):
        monkeypatch.setattr(
            browser_pane.urllib.request, "urlopen", lambda *a, **k: _FakeResponse({})
        )
        result = check_frameable("https://example.com")
        assert result["frameable"] is True
        assert result["checked"] is True

    @pytest.mark.parametrize("value", ["DENY", "SAMEORIGIN", "deny", "sameorigin"])
    def test_x_frame_options_blocks_it(self, monkeypatch, value):
        monkeypatch.setattr(
            browser_pane.urllib.request,
            "urlopen",
            lambda *a, **k: _FakeResponse({"X-Frame-Options": value}),
        )
        result = check_frameable("https://www.google.com")
        assert result["frameable"] is False
        assert "X-Frame-Options" in result["reason"]

    def test_csp_frame_ancestors_none_blocks_it(self, monkeypatch):
        monkeypatch.setattr(
            browser_pane.urllib.request,
            "urlopen",
            lambda *a, **k: _FakeResponse(
                {"Content-Security-Policy": "frame-ancestors 'none'"}
            ),
        )
        result = check_frameable("https://example.com")
        assert result["frameable"] is False
        assert "frame-ancestors" in result["reason"]

    def test_csp_frame_ancestors_specific_origin_blocks_us(self, monkeypatch):
        monkeypatch.setattr(
            browser_pane.urllib.request,
            "urlopen",
            lambda *a, **k: _FakeResponse(
                {"Content-Security-Policy": "frame-ancestors https://example.com"}
            ),
        )
        result = check_frameable("https://example.com")
        assert result["frameable"] is False

    def test_csp_frame_ancestors_wildcard_allows_it(self, monkeypatch):
        monkeypatch.setattr(
            browser_pane.urllib.request,
            "urlopen",
            lambda *a, **k: _FakeResponse({"Content-Security-Policy": "frame-ancestors *"}),
        )
        result = check_frameable("https://example.com")
        assert result["frameable"] is True

    def test_http_error_still_reads_its_headers(self, monkeypatch):
        def raise_http_error(*a, **k):
            raise urllib.error.HTTPError(
                "https://x", 405, "Method Not Allowed", _FakeHeaders({"X-Frame-Options": "DENY"}), None
            )

        monkeypatch.setattr(browser_pane.urllib.request, "urlopen", raise_http_error)
        result = check_frameable("https://x")
        assert result["frameable"] is False

    def test_network_failure_stays_honest_and_does_not_block(self, monkeypatch):
        def raise_url_error(*a, **k):
            raise urllib.error.URLError("no route to host")

        monkeypatch.setattr(browser_pane.urllib.request, "urlopen", raise_url_error)
        result = check_frameable("https://unreachable.example")
        assert result["frameable"] is True
        assert result["checked"] is False
        assert "no route to host" in result["reason"]


class TestBrowserPaneRequests:
    def test_request_open_returns_the_real_event(self):
        bpr = BrowserPaneRequests()
        event = bpr.request_open("https://example.com")
        assert event == {"type": "browser_pane_open", "url": "https://example.com"}

    def test_observers_are_notified(self):
        bpr = BrowserPaneRequests()
        seen = []
        bpr.on_request(seen.append)
        bpr.request_open("https://example.com")
        assert seen == [{"type": "browser_pane_open", "url": "https://example.com"}]

    def test_multiple_observers_all_fire(self):
        bpr = BrowserPaneRequests()
        a, b = [], []
        bpr.on_request(a.append)
        bpr.on_request(b.append)
        bpr.request_open("https://example.com")
        assert len(a) == 1 and len(b) == 1

    def test_a_raising_observer_never_breaks_the_request(self):
        bpr = BrowserPaneRequests()

        def _boom(event):
            raise RuntimeError("a broken UI bridge must never break the tool call")

        bpr.on_request(_boom)
        good = []
        bpr.on_request(good.append)
        event = bpr.request_open("https://example.com")
        assert event["url"] == "https://example.com"
        assert len(good) == 1


class TestSingleton:
    def test_get_returns_the_same_instance_across_calls(self):
        set_browser_pane_requests(None)
        try:
            a = get_browser_pane_requests()
            b = get_browser_pane_requests()
            assert a is b
        finally:
            set_browser_pane_requests(None)

    def test_set_overrides_for_isolation(self):
        fresh = BrowserPaneRequests()
        set_browser_pane_requests(fresh)
        try:
            assert get_browser_pane_requests() is fresh
        finally:
            set_browser_pane_requests(None)


class TestInjectBaseTag:
    """The real mechanism behind the rewriting proxy: one <base> tag
    makes every relative URL a page already uses resolve against the
    REAL site, without touching any of those URLs individually.
    2026-09-14 (feature 4, fix #2): _inject_base_tag now ALSO injects
    the navigation-intercept shim immediately after <base>, so every
    assertion here checks for both landing together, in that order."""

    def _shim_tag(self) -> bytes:
        return browser_pane._NAV_INTERCEPT_SCRIPT_TEMPLATE.encode("utf-8")

    def test_inserts_right_after_head_tag(self):
        html = b"<html><head><title>x</title></head><body>hi</body></html>"
        out = browser_pane._inject_base_tag(html, "https://example.com/page")
        assert out == (
            b'<html><head><base href="https://example.com/page">'
            + self._shim_tag()
            + b"<title>x</title></head><body>hi</body></html>"
        )

    def test_head_tag_with_attributes_still_matches(self):
        html = b'<html><head lang="en"><title>x</title></head></html>'
        out = browser_pane._inject_base_tag(html, "https://example.com/")
        assert b'<head lang="en"><base href="https://example.com/">' in out
        # The shim must land between <base> and whatever the page had next.
        base_end = out.index(b'<base href="https://example.com/">') + len(
            b'<base href="https://example.com/">'
        )
        assert out[base_end : base_end + len(self._shim_tag())] == self._shim_tag()

    def test_falls_back_to_after_html_tag_when_no_head(self):
        html = b"<html><body>no head here</body></html>"
        out = browser_pane._inject_base_tag(html, "https://example.com/")
        assert out == (
            b'<html><base href="https://example.com/">'
            + self._shim_tag()
            + b"<body>no head here</body></html>"
        )

    def test_prepends_outright_for_malformed_markup(self):
        html = b"just text, no tags at all"
        out = browser_pane._inject_base_tag(html, "https://example.com/")
        assert out == (
            b'<base href="https://example.com/">' + self._shim_tag() + b"just text, no tags at all"
        )

    def test_injected_base_wins_over_an_existing_one(self):
        """Only the FIRST <base> in document order takes effect per spec
        -- this must land before any <base> the page already has."""
        html = b'<html><head><base href="/wrong"><title>x</title></head></html>'
        out = browser_pane._inject_base_tag(html, "https://example.com/right")
        first_base = out.index(b"<base")
        second_base = out.index(b"<base", first_base + 1)
        assert b'href="https://example.com/right"' in out[first_base:second_base]
        # The shim rides along with the WINNING (first) <base>, not the page's own.
        assert self._shim_tag() in out[first_base:second_base]


class TestNavInterceptScript:
    """2026-09-14 (feature 4, fix #2), real live-reported problem: "a lot
    of pages refuse embedding or proxy" -- the original proxy only ever
    handled the FIRST page load; the very next click inside it navigated
    the iframe straight at the real site, bypassing the proxy entirely.
    This script (injected right after <base> — see TestInjectBaseTag)
    intercepts clicks/window.open/GET-form-submit and routes them back
    through the proxy. These tests check the injected script is well-
    formed and contains the real behaviors it claims to (a live browser
    engine isn't available in this hermetic suite -- see
    test_browser_pane_wiring.py for the real end-to-end SSE coverage of
    this whole feature)."""

    def test_script_is_syntactically_parseable_javascript(self):
        # A cheap but real syntax sanity check without a JS engine
        # dependency: balanced braces/parens is enough to catch the
        # class of mistake a hand-edited template is most likely to make.
        text = browser_pane._NAV_INTERCEPT_SCRIPT_TEMPLATE
        assert text.startswith("<script>") and text.endswith("</script>")
        body = text[len("<script>") : -len("</script>")]
        assert body.count("{") == body.count("}")
        assert body.count("(") == body.count(")")

    def test_routes_through_the_real_proxy_endpoint(self):
        assert "/api/browser-pane/proxy?url=" in browser_pane._NAV_INTERCEPT_SCRIPT_TEMPLATE

    def test_intercepts_clicks_window_open_and_get_form_submits(self):
        text = browser_pane._NAV_INTERCEPT_SCRIPT_TEMPLATE
        assert 'addEventListener("click"' in text
        assert 'addEventListener("submit"' in text
        assert "window.open = function" in text
        # POST forms are an honest, disclosed non-goal (see the module's
        # own limitations comment) -- the shim must actively skip them,
        # not silently mishandle them.
        assert 'method !== "get"' in text

    def test_ignores_bare_hash_links(self):
        """A same-page anchor (href="#section") must never be rewritten
        into a real navigation -- it isn't one."""
        text = browser_pane._NAV_INTERCEPT_SCRIPT_TEMPLATE
        assert 'href.charAt(0) === "#"' in text


class TestNeutralizeFrameBusting:
    """2026-09-14 (feature 4, fix #1), real live-reported problem:
    JS frame-busting (`if (top !== self) top.location = ...`) is
    completely separate from the X-Frame-Options/CSP headers
    check_frameable() detects, and very common on real sites. A real
    browser will not let this proxy override `window.top`'s getter at
    runtime (non-configurable in every real engine) -- the standard
    technique other HTML-rewriting proxies use instead, and the one
    this applies, is textual neutralization of the common busting
    phrasings before the page is ever parsed. See
    _FRAME_BUST_REPLACEMENTS' own docstring for the honest limitations
    (inline script only, common phrasings only)."""

    def test_neutralizes_the_classic_top_not_equal_self_check(self):
        html = b"<script>if (top !== self) { top.location = self.location; }</script>"
        out = browser_pane._neutralize_frame_busting(html)
        assert b"top !== self" not in out
        assert b"top.location" not in out
        assert b"false" in out

    def test_neutralizes_the_window_prefixed_variant(self):
        html = b"<script>if (window.top != window.self) window.top.location.href = 'x';</script>"
        out = browser_pane._neutralize_frame_busting(html)
        assert b"window.top != window.self" not in out
        assert b"window.top" not in out

    def test_neutralizes_parent_location_variant(self):
        html = b"<script>parent.location = window.parent.location.href;</script>"
        out = browser_pane._neutralize_frame_busting(html)
        assert b"parent.location" not in out
        assert b"window.parent" not in out

    def test_leaves_ordinary_unrelated_script_untouched(self):
        html = b"<script>console.log('hello, world'); var x = 1 + 1;</script>"
        out = browser_pane._neutralize_frame_busting(html)
        assert out == html

    def test_applied_by_the_real_proxy_fetch(self, monkeypatch):
        monkeypatch.setattr(
            browser_pane.urllib.request,
            "urlopen",
            lambda *a, **k: _FakeResponse(
                {"Content-Type": "text/html"},
                body=b"<html><head></head><body><script>if(top!==self){top.location='x';}</script></body></html>",
            ),
        )
        result = browser_pane.fetch_and_rewrite_for_proxy("https://example.com/")
        assert result["ok"] is True
        assert b"top!==self" not in result["body"]
        assert b"top.location" not in result["body"]


class TestFetchAndRewriteForProxy:
    def test_real_html_page_gets_base_tag_injected(self, monkeypatch):
        monkeypatch.setattr(
            browser_pane.urllib.request,
            "urlopen",
            lambda *a, **k: _FakeResponse(
                {"Content-Type": "text/html; charset=utf-8"},
                body=b"<html><head></head><body>hi</body></html>",
            ),
        )
        result = browser_pane.fetch_and_rewrite_for_proxy("https://example.com/page")
        assert result["ok"] is True
        assert b'<base href="https://example.com/page">' in result["body"]

    def test_non_html_content_type_is_an_honest_failure(self, monkeypatch):
        monkeypatch.setattr(
            browser_pane.urllib.request,
            "urlopen",
            lambda *a, **k: _FakeResponse({"Content-Type": "application/pdf"}, body=b"%PDF-1.4"),
        )
        result = browser_pane.fetch_and_rewrite_for_proxy("https://example.com/file.pdf")
        assert result["ok"] is False
        assert b"not an HTML page" in result["body"]
        assert b"example.com/file.pdf" in result["body"]

    def test_oversized_page_is_an_honest_failure(self, monkeypatch):
        monkeypatch.setattr(browser_pane, "PROXY_MAX_BYTES", 10)
        monkeypatch.setattr(
            browser_pane.urllib.request,
            "urlopen",
            lambda *a, **k: _FakeResponse(
                {"Content-Type": "text/html"}, body=b"x" * 1000
            ),
        )
        result = browser_pane.fetch_and_rewrite_for_proxy("https://example.com/")
        assert result["ok"] is False
        assert b"proxy limit" in result["body"]

    def test_network_failure_is_a_real_html_error_page_not_a_crash(self, monkeypatch):
        def raise_url_error(*a, **k):
            raise urllib.error.URLError("no route to host")

        monkeypatch.setattr(browser_pane.urllib.request, "urlopen", raise_url_error)
        result = browser_pane.fetch_and_rewrite_for_proxy("https://unreachable.example/")
        assert result["ok"] is False
        assert b"<html>" in result["body"]
        assert b"no route to host" in result["body"]
        assert b"unreachable.example" in result["body"]  # the real "open it directly" link
