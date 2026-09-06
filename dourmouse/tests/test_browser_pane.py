"""Tests for dourmouse/browser_pane.py — the embedded browser pane's
real trigger bridge (backlog #8). Same shape as test_message_bus.py's
own MessageBus tests, deliberately (same real singleton pattern)."""

from __future__ import annotations

from dourmouse.browser_pane import (
    BrowserPaneRequests,
    get_browser_pane_requests,
    set_browser_pane_requests,
)


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
