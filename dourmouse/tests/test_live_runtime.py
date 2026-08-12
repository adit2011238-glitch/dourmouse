"""Always-on live agent loop tests (v2.8).

Covers dourmouse/live_runtime.py (the background poll loops), the
ActivityTracker 'live' event handling added in webui.py, the run_server /
serve_forever live_polling wiring, DesktopBridge.open_all_agents() (every
agent gets its own window at startup), and the [LIVE] UI wiring. All fetchers
are fakes or stub registry handlers — the suite never touches the network
(Rule 2.1 hermeticity). Honest failure lines are asserted, not fabricated
success (Rule 2.2).
"""

from __future__ import annotations

import threading
import time

import pytest

from dourmouse import desktop
from dourmouse.dispatch import DispatchRegistry, Subagent, ToolSpec
from dourmouse.live_runtime import LiveRuntime, live_enabled
from dourmouse.webui import ActivityTracker, run_server


def _live_registry() -> DispatchRegistry:
    """Registry with a subset of Live agents whose handlers are LOCAL stubs
    (no network): news_headlines + list_tasks exist; markets/rnd/mail do not,
    so the default schedule filters to exactly 2 polls."""
    r = DispatchRegistry()
    r.register_subagent(
        Subagent(
            name="news",
            domain="Live",
            description="live news",
            tools=(
                ToolSpec(
                    name="news_headlines",
                    description="live headlines",
                    parameters={"type": "object", "properties": {}},
                    handler=lambda a: "REAL NEWS: markets steady",
                ),
            ),
        )
    )
    r.register_subagent(
        Subagent(
            name="tasks",
            domain="Live",
            description="local tasks",
            tools=(
                ToolSpec(
                    name="list_tasks",
                    description="list tasks",
                    parameters={"type": "object", "properties": {}},
                    handler=lambda a: "TASKS: 2 open",
                ),
            ),
        )
    )
    return r


class _FakeTracker:
    def __init__(self):
        self.events: list[dict] = []

    def on_event(self, entry: dict) -> None:
        self.events.append(entry)


# --------------------------------------------------------------------------- #
# LiveRuntime — poll table, immediate first poll, honest errors, lifecycle
# --------------------------------------------------------------------------- #

class TestPollTable:
    def test_only_registered_agents_get_polls(self):
        rt = LiveRuntime(_live_registry(), _FakeTracker())
        # news(1 poll) + tasks(1 poll); markets/rnd/mail filtered out.
        assert rt.poll_count == 2

    def test_ghost_agent_gets_no_polls(self):
        rt = LiveRuntime(_live_registry(), _FakeTracker(), schedule={
            "no_such_agent": [("news_headlines", {}, 60)],
        })
        assert rt.poll_count == 0

    def test_tool_without_handler_is_skipped(self):
        rt = LiveRuntime(_live_registry(), _FakeTracker(), schedule={
            "news": [("market_movers", {}, 60)],  # not registered on news
        })
        assert rt.poll_count == 0


class TestPolling:
    def test_start_polls_immediately_with_real_handler(self):
        """The very first poll happens at start — 'immediately working' —
        through the REAL registered handler (same data path as dispatch)."""
        tracker = _FakeTracker()
        rt = LiveRuntime(_live_registry(), tracker)
        rt.start()
        try:
            deadline = time.time() + 2
            while not tracker.events and time.time() < deadline:
                time.sleep(0.02)
            assert tracker.events, "expected an immediate first poll"
            ev = tracker.events[0]
            assert ev["type"] == "live"
            assert "REAL NEWS: markets steady" in ev["text"]
            assert "news_headlines" in ev["name"]
        finally:
            rt.stop()

    def test_all_registered_agents_poll_immediately(self):
        """Every scheduled agent polls at start — not just the first one in
        the table (each agent runs its own loop thread)."""
        tracker = _FakeTracker()
        rt = LiveRuntime(
            _live_registry(),
            tracker,
            schedule={"news": [("news_headlines", {}, 60)], "tasks": [("list_tasks", {}, 60)]},
        )
        rt.start()
        try:
            deadline = time.time() + 1.5
            while len(tracker.events) < 2 and time.time() < deadline:
                time.sleep(0.02)
            tools = {e["name"] for e in tracker.events}
            assert "news_headlines" in tools and "list_tasks" in tools
        finally:
            rt.stop()

    def test_injected_fetcher_overrides_handler(self):
        tracker = _FakeTracker()
        rt = LiveRuntime(
            _live_registry(),
            tracker,
            fetcher=lambda tool, args: f"FAKE:{tool}",
            schedule={"news": [("news_headlines", {}, 60)]},
        )
        rt.start()
        try:
            deadline = time.time() + 2
            while not tracker.events and time.time() < deadline:
                time.sleep(0.02)
            assert tracker.events
            assert tracker.events[0]["text"] == "FAKE:news_headlines"
        finally:
            rt.stop()

    def test_failing_fetch_is_honest_not_fabricated(self):
        """A poll that fails emits an honest LIVE POLL FAILED line — never a
        made-up success (Rule 2.2)."""
        tracker = _FakeTracker()

        def _boom(tool, args):
            raise RuntimeError("network down")

        rt = LiveRuntime(
            _live_registry(),
            tracker,
            fetcher=_boom,
            schedule={"news": [("news_headlines", {}, 60)]},
        )
        rt.start()
        try:
            deadline = time.time() + 2
            while not tracker.events and time.time() < deadline:
                time.sleep(0.02)
            assert tracker.events
            assert "LIVE POLL FAILED" in tracker.events[0]["text"]
            assert "network down" in tracker.events[0]["text"]
        finally:
            rt.stop()

    def test_stop_halts_loops(self):
        tracker = _FakeTracker()
        rt = LiveRuntime(
            _live_registry(),
            tracker,
            schedule={"news": [("news_headlines", {}, 60)]},
        )
        rt.start()
        rt.stop()
        assert rt.running is False
        count = len(tracker.events)
        time.sleep(0.15)
        assert len(tracker.events) == count  # no more polls after stop


# --------------------------------------------------------------------------- #
# live_enabled env gate
# --------------------------------------------------------------------------- #

class TestLiveEnabled:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("1", True),
            ("true", True),
            ("on", True),
            ("0", False),
            ("false", False),
            ("no", False),
            ("off", False),
            ("", False),
        ],
    )
    def test_values(self, value, expected):
        assert live_enabled(value) is expected

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_LIVE", "0")
        assert live_enabled() is False
        monkeypatch.setenv("DOURMOUSE_LIVE", "1")
        assert live_enabled() is True


# --------------------------------------------------------------------------- #
# ActivityTracker — 'live' events set status, and done/error do NOT reset it
# --------------------------------------------------------------------------- #

class TestTrackerLive:
    def _tracker(self):
        return ActivityTracker(_live_registry())

    def test_live_event_sets_status_and_feed(self):
        tracker = self._tracker()
        tracker.on_event({"type": "live", "name": "news_headlines", "text": "headline!"})
        snap = tracker.snapshot()
        assert snap["agents"]["news"]["status"] == "live"
        assert snap["agents"]["news"]["feed"][-1]["type"] == "live"
        assert "headline!" in snap["agents"]["news"]["feed"][-1]["text"]

    def test_done_does_not_reset_live_agent(self):
        """A chat run ending resets computing/auth agents to idle, but a LIVE
        agent keeps its always-on status (its poll loop is independent)."""
        tracker = self._tracker()
        tracker.on_event({"type": "live", "name": "news_headlines", "text": "x"})
        tracker.on_event({"type": "done"})
        assert tracker.snapshot()["agents"]["news"]["status"] == "live"

    def test_tool_use_overrides_live_while_computing(self):
        tracker = self._tracker()
        tracker.on_event({"type": "live", "name": "news_headlines", "text": "x"})
        tracker.on_event({"type": "tool_use", "name": "news_headlines", "raw_arguments": "{}"})
        assert tracker.snapshot()["agents"]["news"]["status"] == "computing"

    def test_live_poll_does_not_clobber_mid_chat_computing(self):
        """A background poll arriving while the agent is computing in a chat
        must not flip it back to LIVE mid-run (reviewer-caught edge)."""
        tracker = self._tracker()
        tracker.on_event({"type": "tool_use", "name": "news_headlines", "raw_arguments": "{}"})
        tracker.on_event({"type": "live", "name": "news_headlines", "text": "x"})
        assert tracker.snapshot()["agents"]["news"]["status"] == "computing"


# --------------------------------------------------------------------------- #
# run_server / serve_forever wiring
# --------------------------------------------------------------------------- #

@pytest.fixture
def live_server(monkeypatch):
    monkeypatch.setenv("DOURMOUSE_LIVE", "1")
    srv = run_server(_live_registry(), port=0, client=None, config=None, live_polling=True)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    if srv.live_runtime is not None:
        srv.live_runtime.stop()
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


class TestServerWiring:
    def test_live_polling_starts_runtime(self, live_server):
        assert live_server.live_runtime is not None
        assert live_server.live_runtime.poll_count == 2

    def test_live_events_land_in_tracker(self, live_server):
        """Live polling feeds the SAME tracker the agent windows poll — so the
        /api/agent/<name> snapshot shows live activity without any prompt."""
        deadline = time.time() + 3
        status = "idle"
        while time.time() < deadline:
            snap = live_server.tracker.snapshot()["agents"]["news"]
            if snap["feed"]:
                status = snap["status"]
                break
            time.sleep(0.02)
        assert status == "live"

    def test_live_polling_off_by_default(self):
        srv = run_server(_live_registry(), port=0, client=None, config=None)
        try:
            assert srv.live_runtime is None
        finally:
            srv.server_close()

    def test_env_disables_live_polling(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_LIVE", "0")
        srv = run_server(_live_registry(), port=0, client=None, config=None, live_polling=True)
        try:
            assert srv.live_runtime is None
        finally:
            srv.server_close()


# --------------------------------------------------------------------------- #
# Desktop — every agent gets its own window at startup
# --------------------------------------------------------------------------- #

class _FakeWindow:
    def __init__(self, title, url, **kwargs):
        self.title = title
        self.url = url
        self.kwargs = kwargs
        self.shown = False
        self.closed = False

    def show(self):
        self.shown = True


class _FakeWebview:
    def __init__(self):
        self.windows = []
        self.started = False

    def create_window(self, title, url=None, **kwargs):
        win = _FakeWindow(title, url, **kwargs)
        self.windows.append(win)
        return win

    def start(self):
        self.started = True


class TestOpenAllAgents:
    def _bridge(self):
        wv = _FakeWebview()
        bridge = desktop.DesktopBridge(_FakeWindow("MAP", "http://127.0.0.1:1/map"), wv, "http://127.0.0.1:1")
        return bridge, wv

    def test_open_all_agents_creates_every_window(self):
        bridge, wv = self._bridge()
        bridge.open_all_agents(["news", "markets", "tasks"])
        assert len(wv.windows) == 3
        urls = {w.url.split("/")[-1] for w in wv.windows}
        assert urls == {"news", "markets", "tasks"}

    def test_open_all_agents_dedupes_reuses(self):
        bridge, wv = self._bridge()
        bridge.open_all_agents(["news", "markets"])
        bridge.open_all_agents(["news", "markets"])
        assert len(wv.windows) == 2  # second call reuses both

    def test_open_all_agents_empty_is_noop(self):
        bridge, wv = self._bridge()
        bridge.open_all_agents([])
        assert wv.windows == []

    def test_launch_opens_every_agent_window(self, monkeypatch):
        from dourmouse.tests.test_webui import _echo_registry

        monkeypatch.setenv("DOURMOUSE_UI_PORT", "0")
        monkeypatch.setenv("DOURMOUSE_LEARN", "0")  # v2.9: hermetic — no real memory store
        fake = _FakeWebview()
        code = desktop.launch(
            _echo_registry(), port=0, webview_loader=lambda: fake,
            live_polling=False, open_all_windows=True,
        )
        assert code == 0
        titles = [w.title for w in fake.windows]
        assert "AGENT // ECHO_AGENT" in titles  # the one agent in echo registry


# --------------------------------------------------------------------------- #
# UI wiring — [LIVE] status rendered in all three surfaces
# --------------------------------------------------------------------------- #

class TestUiWiring:
    def _read(self, rel: str) -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parents[2] / rel).read_text(encoding="utf-8")

    def test_agent_window_renders_live(self):
        html = self._read("ui/agent.html")
        assert "'[LIVE]'" in html
        assert "f.type === 'live'" in html
        assert "pill.live" in html

    def test_map_renders_live(self):
        html = self._read("ui/map.html")
        assert "'[LIVE]'" in html
        assert "npill.live" in html
        assert "f.type === 'live'" in html

    def test_dashboard_renders_live(self):
        html = self._read("ui/index.html")
        assert "npill live" in html
        assert "pollLiveActivity" in html
        assert "'[LIVE]'" in html

    def test_desktop_bridge_ships_open_all(self):
        src = self._read("dourmouse/desktop.py")
        assert "def open_all_agents" in src

    def test_webui_ships_live_runtime(self):
        src = self._read("dourmouse/webui.py")
        assert "LiveRuntime" in src
        assert "live_polling" in src
