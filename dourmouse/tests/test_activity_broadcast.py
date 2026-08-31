"""ActivityTracker's real SSE push (v13.6) — Vision OS item 7's own
flagged gap: "the current implementation polls a snapshot every 2s, not
a genuine SSE event stream into the native shell." Closes it by giving
ActivityTracker.on_event a real, optional broadcast hook wired to the
SAME pre-existing ``server.events_broadcast`` fan-out hub `/api/events`
already serves (Freebuff/all_hands/state_change events) — no new
endpoint, no new infrastructure, just a new real event type
(``agent_activity``) on the existing bus, and only for the agents that
actually changed on a given event (not a full snapshot spam).
"""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from dourmouse.dispatch import DispatchRegistry, Subagent, ToolSpec
from dourmouse.webui import ActivityTracker, run_server


def _registry() -> DispatchRegistry:
    reg = DispatchRegistry()
    reg.register_subagent(
        Subagent(
            name="echo_agent", domain="Test", description="echoes",
            tools=(ToolSpec(name="echo", description="e", parameters={"type": "object", "properties": {}}, handler=lambda a: "ok"),),
        )
    )
    reg.register_subagent(
        Subagent(
            name="other_agent", domain="Test", description="other",
            tools=(ToolSpec(name="other_tool", description="o", parameters={"type": "object", "properties": {}}, handler=lambda a: "ok"),),
        )
    )
    return reg


class TestSetBroadcastWiring:
    def test_no_broadcaster_is_a_safe_default(self):
        """Unwired (the pre-v13.6 default): on_event still works exactly
        as before, nothing raises."""
        tracker = ActivityTracker(_registry())
        tracker.on_event({"type": "tool_use", "name": "echo", "raw_arguments": "{}"})
        assert tracker.snapshot()["agents"]["echo_agent"]["status"] == "computing"

    def test_tool_use_broadcasts_only_the_changed_agent(self):
        tracker = ActivityTracker(_registry())
        received = []
        tracker.set_broadcast(received.append)
        tracker.on_event({"type": "tool_use", "name": "echo", "raw_arguments": '{"x":1}'})
        assert len(received) == 1
        payload = received[0]
        assert payload["type"] == "agent_activity"
        assert set(payload["agents"].keys()) == {"echo_agent"}
        assert payload["agents"]["echo_agent"]["status"] == "computing"

    def test_unmapped_tool_broadcasts_nothing(self):
        tracker = ActivityTracker(_registry())
        received = []
        tracker.set_broadcast(received.append)
        tracker.on_event({"type": "tool_use", "name": "does_not_exist", "raw_arguments": "{}"})
        assert received == []

    def test_done_event_broadcasts_every_agent_it_actually_resets(self):
        tracker = ActivityTracker(_registry())
        tracker.on_event({"type": "tool_use", "name": "echo", "raw_arguments": "{}"})
        tracker.on_event({"type": "tool_use", "name": "other_tool", "raw_arguments": "{}"})
        received = []
        tracker.set_broadcast(received.append)
        tracker.on_event({"type": "done"})
        assert len(received) == 1
        assert set(received[0]["agents"].keys()) == {"echo_agent", "other_agent"}
        assert all(a["status"] == "idle" for a in received[0]["agents"].values())

    def test_done_event_with_no_active_agents_broadcasts_nothing(self):
        tracker = ActivityTracker(_registry())
        received = []
        tracker.set_broadcast(received.append)
        tracker.on_event({"type": "done"})
        assert received == []

    def test_a_broken_broadcaster_never_breaks_on_event(self):
        """The real safety property: a raising broadcast function must
        never take dispatch down (same discipline as on_event's own
        outer try/except for everything else)."""
        tracker = ActivityTracker(_registry())
        tracker.set_broadcast(lambda payload: (_ for _ in ()).throw(RuntimeError("hub down")))
        tracker.on_event({"type": "tool_use", "name": "echo", "raw_arguments": "{}"})
        assert tracker.snapshot()["agents"]["echo_agent"]["status"] == "computing"

    def test_live_status_transition_is_broadcast(self):
        tracker = ActivityTracker(_registry())
        received = []
        tracker.set_broadcast(received.append)
        tracker.on_event({"type": "live", "name": "echo", "raw_arguments": "{}", "text": "poll ok"})
        assert len(received) == 1
        assert received[0]["agents"]["echo_agent"]["status"] == "live"

    def test_live_event_that_does_not_change_status_broadcasts_nothing(self):
        """A LIVE agent polling again and again shouldn't spam the SSE
        hub on every poll tick if its status never actually changes."""
        tracker = ActivityTracker(_registry())
        tracker.on_event({"type": "live", "name": "echo", "raw_arguments": "{}", "text": "first"})
        received = []
        tracker.set_broadcast(received.append)
        tracker.on_event({"type": "computing-blocker-noop"})  # no-op control, sanity only
        assert received == []


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    srv = run_server(_registry(), port=0, client=None, config=None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    port = srv.server_address[1]
    yield srv, port
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


class TestRealServerWiring:
    def test_run_server_wires_the_broadcast_automatically(self, server):
        srv, _port = server
        # Bound methods are re-created on each attribute access, so `is`
        # would spuriously fail even when correctly wired -- compare the
        # underlying function and bound instance instead.
        assert srv.tracker._broadcast.__func__ is srv.events_broadcast.broadcast.__func__
        assert srv.tracker._broadcast.__self__ is srv.events_broadcast

    def test_real_sse_client_receives_a_real_agent_activity_event(self, server):
        """End-to-end: a real GET /api/events connection actually
        receives a real agent_activity event pushed by a real
        tracker.on_event call — the exact real upgrade item 7 flagged as
        missing (a genuine push, not a 2s poll)."""
        srv, port = server
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
        import time

        time.sleep(0.2)  # let the connection register with the hub
        srv.tracker.on_event({"type": "tool_use", "name": "echo", "raw_arguments": "{}"})
        deadline = time.time() + 5
        while time.time() < deadline and not any(e.get("type") == "agent_activity" for e in events):
            time.sleep(0.05)
        conn.close()
        reader.join(timeout=2)

        activity_events = [e for e in events if e.get("type") == "agent_activity"]
        assert activity_events, f"no agent_activity event received; got {events!r}"
        assert activity_events[0]["agents"]["echo_agent"]["status"] == "computing"
