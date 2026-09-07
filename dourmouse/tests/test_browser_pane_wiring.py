"""End-to-end: a real open_browser_pane tool call actually reaches a real
GET /api/events SSE client — the same real-push pattern
test_activity_broadcast.py already proves for agent_activity events,
applied to backlog #8's browser pane trigger.
"""

from __future__ import annotations

import http.client
import json
import threading
import time

import pytest

from dourmouse.browser_pane import BrowserPaneRequests
from dourmouse.dispatch import DispatchRegistry, Subagent, ToolSpec
from dourmouse.general_roster import _open_browser_pane_tool
from dourmouse.webui import run_server


def _registry() -> DispatchRegistry:
    reg = DispatchRegistry()
    reg.register_subagent(Subagent(name="echo_agent", domain="test", description="d", tools=()))
    return reg


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    bpr = BrowserPaneRequests()
    srv = run_server(_registry(), port=0, client=None, config=None, browser_pane_requests=bpr)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    port = srv.server_address[1]
    yield srv, port, bpr
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


class TestRealServerWiring:
    def test_run_server_wires_the_injected_instance(self, server):
        srv, _port, bpr = server
        assert srv.browser_pane_requests is bpr

    def test_run_server_defaults_to_the_global_singleton(self, monkeypatch, tmp_path):
        from dourmouse.browser_pane import get_browser_pane_requests, set_browser_pane_requests

        set_browser_pane_requests(None)
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
        srv = run_server(_registry(), port=0, client=None, config=None)
        try:
            assert srv.browser_pane_requests is get_browser_pane_requests()
        finally:
            srv.server_close()
            set_browser_pane_requests(None)


class TestRealSseDelivery:
    def test_real_sse_client_receives_a_real_browser_pane_open_event(self, server, monkeypatch):
        """End-to-end: a real GET /api/events connection actually
        receives the event a real open_browser_pane tool call triggers —
        no mocked bridge, the real singleton the tool itself calls."""
        srv, port, bpr = server
        from dourmouse.browser_pane import set_browser_pane_requests

        set_browser_pane_requests(bpr)  # the tool calls the GLOBAL, not srv's copy directly
        try:
            events: list[dict] = []
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", "/api/events")
            resp = conn.getresponse()

            def _read_loop():
                while True:
                    line = resp.readline()
                    if not line:
                        break
                    if line.startswith(b"data:"):
                        events.append(json.loads(line[len(b"data:"):].strip()))

            reader = threading.Thread(target=_read_loop, daemon=True)
            reader.start()
            time.sleep(0.2)  # let the connection register with the hub

            result = _open_browser_pane_tool({"url": "https://example.com"})
            assert "OPENED BROWSER PANE" in result

            deadline = time.time() + 5
            while time.time() < deadline and not any(e.get("type") == "browser_pane_open" for e in events):
                time.sleep(0.05)
            conn.close()
            reader.join(timeout=2)

            pane_events = [e for e in events if e.get("type") == "browser_pane_open"]
            assert pane_events, f"no browser_pane_open event received; got {events!r}"
            assert pane_events[0]["url"] == "https://example.com"
        finally:
            set_browser_pane_requests(None)


class TestCheckEndpoint:
    """GET /api/browser-pane/check — the real fix for the live-testing
    bug where an X-Frame-Options-blocked site left the pane blank
    forever (see test_browser_pane.py's TestCheckFrameable for the
    header-parsing logic this endpoint wraps)."""

    def test_rejects_non_http_urls_without_making_any_request(self, server, monkeypatch):
        srv, port, _bpr = server

        def fail_if_called(*a, **k):
            raise AssertionError("must not attempt a network check for a non-http(s) URL")

        monkeypatch.setattr("dourmouse.browser_pane.urllib.request.urlopen", fail_if_called)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/browser-pane/check?url=javascript:alert(1)")
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        conn.close()
        assert resp.status == 200
        assert data["frameable"] is False

    def test_real_call_reaches_check_frameable(self, server, monkeypatch):
        srv, port, _bpr = server
        seen = {}

        def fake_check(url):
            seen["url"] = url
            return {"frameable": False, "reason": "X-Frame-Options: DENY", "checked": True}

        monkeypatch.setattr("dourmouse.browser_pane.check_frameable", fake_check)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/browser-pane/check?url=https://www.google.com")
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        conn.close()
        assert resp.status == 200
        assert data == {"frameable": False, "reason": "X-Frame-Options: DENY", "checked": True}
        assert seen["url"] == "https://www.google.com"


class TestProxyEndpoint:
    """GET /api/browser-pane/proxy — the real fix for sites that block
    framing (not just check_frameable()'s honest detection of it)."""

    def test_rejects_non_http_url_without_a_network_call(self, server, monkeypatch):
        srv, port, _bpr = server

        def fail_if_called(url):
            raise AssertionError("must not attempt to fetch a non-http(s) URL")

        monkeypatch.setattr("dourmouse.browser_pane.fetch_and_rewrite_for_proxy", fail_if_called)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/browser-pane/proxy?url=javascript:alert(1)")
        resp = conn.getresponse()
        body = resp.read().decode()
        conn.close()
        assert resp.status == 200
        assert "Refused" in body

    def test_real_call_reaches_fetch_and_rewrite_and_serves_its_body(self, server, monkeypatch):
        srv, port, _bpr = server
        seen = {}

        def fake_fetch(url):
            seen["url"] = url
            return {"ok": True, "body": b"<html><head><base href='x'></head></html>"}

        monkeypatch.setattr("dourmouse.browser_pane.fetch_and_rewrite_for_proxy", fake_fetch)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/browser-pane/proxy?url=https://www.google.com")
        resp = conn.getresponse()
        body = resp.read()
        headers = dict(resp.getheaders())
        conn.close()
        assert resp.status == 200
        assert seen["url"] == "https://www.google.com"
        assert body == b"<html><head><base href='x'></head></html>"
        assert "text/html" in headers["Content-Type"]
        # The whole point: this response must carry no framing-blocking
        # header of its own, however the real upstream site was blocking it.
        assert "X-Frame-Options" not in headers
        assert "Content-Security-Policy" not in headers
