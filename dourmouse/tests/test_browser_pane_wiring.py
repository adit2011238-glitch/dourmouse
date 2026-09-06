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
