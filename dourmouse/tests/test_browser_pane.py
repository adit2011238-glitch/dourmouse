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
    def __init__(self, headers):
        self.headers = _FakeHeaders(headers)

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
