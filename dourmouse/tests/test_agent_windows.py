"""Per-agent live window tests (v2.7).

Each agent gets its own DOURMOUSE window showing live activity. Tests cover the
real HTTP routes (/agent/<name> page + /api/agent/<name> focused snapshot),
the DesktopBridge.open_agent() native-window creation/dedupe logic (via a
fake webview — no real GUI in CI), and the UI wiring strings.
"""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from dourmouse import desktop
from dourmouse.tests.test_webui import _echo_registry


# --------------------------------------------------------------------------- #
# HTTP routes
# --------------------------------------------------------------------------- #

@pytest.fixture
def server(monkeypatch, tmp_path):
    from dourmouse.webui import run_server

    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    srv = run_server(_echo_registry(), port=0, client=None, config=None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    port = srv.server_address[1]
    yield srv, port
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read().decode()
    conn.close()
    return resp.status, body


class TestAgentPageRoute:
    def test_agent_page_served_for_known_agent(self, server):
        status, body = _get(server[1], "/agent/echo_agent")
        assert status == 200
        assert "<!DOCTYPE html>" in body
        assert "AGENT LIVE WINDOW" in body

    def test_unknown_agent_404(self, server):
        status, body = _get(server[1], "/agent/does_not_exist")
        assert status == 404

    def test_map_and_index_still_served(self, server):
        assert _get(server[1], "/")[0] == 200
        assert _get(server[1], "/map")[0] == 200


class TestAgentApi:
    def test_snapshot_returns_identity_tools_status_feed(self, server):
        status, body = _get(server[1], "/api/agent/echo_agent")
        assert status == 200
        data = json.loads(body)
        assert data["agent"]["name"] == "echo_agent"
        assert data["agent"]["domain"] == "Test"
        assert any(t["name"] == "echo" for t in data["agent"]["tools"])
        assert data["status"] == "idle"
        assert isinstance(data["feed"], list)

    def test_snapshot_unknown_agent_404(self, server):
        status, body = _get(server[1], "/api/agent/nope")
        assert status == 404
        assert "no such agent" in json.loads(body)["error"]

    def test_snapshot_tracks_activity(self, server):
        """After a tool_use event, the focused snapshot shows it live."""
        # Feed the real ActivityTracker a synthetic tool_use for the echo
        # registry's one tool; the snapshot must reflect it over HTTP.
        tracker = server[0].tracker
        tracker.on_event({"type": "tool_use", "name": "echo", "raw_arguments": "{}"})
        status, body = _get(server[1], "/api/agent/echo_agent")
        data = json.loads(body)
        assert data["status"] == "computing"
        assert data["feed"][-1]["type"] == "tool_use"


# --------------------------------------------------------------------------- #
# DesktopBridge.open_agent — native per-agent windows (fake webview)
# --------------------------------------------------------------------------- #

class _FakeWindow:
    def __init__(self, title, url, **kwargs):
        self.title = title
        self.url = url
        self.kwargs = kwargs
        self.shown = False
        self.closed = False
        self.destroyed = False

    def show(self):
        self.shown = True

    def destroy(self):
        self.closed = True
        self.destroyed = True


class _FakeWebview:
    def __init__(self):
        self.windows: list[_FakeWindow] = []

    def create_window(self, title, url=None, **kwargs):
        win = _FakeWindow(title, url, **kwargs)
        self.windows.append(win)
        return win


class TestOpenAgent:
    def _bridge(self):
        wv = _FakeWebview()
        bridge = desktop.DesktopBridge(_FakeWindow("MAP", "http://127.0.0.1:1/map"), wv, "http://127.0.0.1:1")
        return bridge, wv

    def test_open_agent_creates_window_with_correct_url(self):
        bridge, wv = self._bridge()
        bridge.open_agent("news")
        assert len(wv.windows) == 1
        win = wv.windows[0]
        assert win.url == "http://127.0.0.1:1/agent/news"
        assert "NEWS" in win.title

    def test_open_agent_dedupes_reuses_existing_window(self):
        bridge, wv = self._bridge()
        bridge.open_agent("news")
        bridge.open_agent("news")
        assert len(wv.windows) == 1  # second call reuses, not duplicates
        assert wv.windows[0].shown is True

    def test_open_agent_recreates_after_close(self):
        bridge, wv = self._bridge()
        bridge.open_agent("news")
        wv.windows[0].destroy()  # user closed the window
        bridge.open_agent("news")
        assert len(wv.windows) == 2  # recreated, not reused

    def test_open_agent_different_agents_get_separate_windows(self):
        bridge, wv = self._bridge()
        bridge.open_agent("news")
        bridge.open_agent("markets")
        assert len(wv.windows) == 2
        assert {w.url.split("/")[-1] for w in wv.windows} == {"news", "markets"}

    def test_open_agent_empty_name_is_noop(self):
        bridge, wv = self._bridge()
        bridge.open_agent("")
        bridge.open_agent(None)
        assert wv.windows == []

    def test_open_map_still_reveals_map(self):
        bridge, wv = self._bridge()
        map_win = bridge._map_window
        assert map_win.shown is False
        bridge.open_map()
        assert map_win.shown is True


# --------------------------------------------------------------------------- #
# UI wiring
# --------------------------------------------------------------------------- #

class TestUiWiring:
    def _read(self, rel: str) -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parents[2] / rel).read_text()

    def test_index_agent_windows_gesture_opens_tab_auto_open_is_bridge_gated(self):
        html = self._read("ui/index.html")
        # The per-agent live window is user-gesture driven: roster ⧉ / map
        # button open the native window via the desktop bridge, falling back
        # to a plain-browser tab (a real click, so the popup is allowed).
        assert "openedAgentWindows" in html
        assert "openAgentWindow(ag.name)" in html
        assert "tryAutoOpenAgentWindow(node)" in html
        assert "api.open_agent" in html
        assert "window.open('/agent/" in html
        # The SSE tool_use callback is NOT a user gesture — it must only
        # auto-open when a real desktop bridge exists, never via window.open
        # (silently blocked in browsers, tab-hijacking in strict automation).
        assert "tryAutoOpenAgentWindow" in html
        assert "function tryAutoOpenAgentWindow(name)" in html
        # The SSE handler routes through the gated helper, not the raw opener:
        # the auto-open call site appears only where the gated helper is used.
        idx_auto = html.index("tryAutoOpenAgentWindow(node)")
        idx_raw = html.index("function tryAutoOpenAgentWindow")
        assert idx_auto > idx_raw

    def test_index_roster_has_window_button(self):
        html = self._read("ui/index.html")
        assert "class=\"nwin\"" in html
        assert "openAgentWindow(ag.name)" in html

    def test_map_detail_panel_has_live_window_button(self):
        html = self._read("ui/map.html")
        assert "[LIVE WINDOW]" in html
        assert "api.open_agent(ag.name)" in html

    def test_agent_page_self_contained(self):
        html = self._read("ui/agent.html")
        assert "LIVE ACTIVITY FEED" in html
        assert "/api/agent/" in html
        assert "focus_agent" in html

    def test_desktop_bridge_ships_in_engine(self):
        src = self._read("dourmouse/desktop.py")
        assert "def open_agent" in src
        assert "def open_map" in src
