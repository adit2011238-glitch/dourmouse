"""v5.9 Freebuff live-activity tests (freebuff_events.py + webui fan-out).

Every test is hermetic (Rule 2.1): a tiny stdlib SSE server under a free
port feeds canned Freebuff-shaped events, and FREEBUFF_API_URL points the
watcher at it — no real Freebuff app, no network beyond loopback. Verifies:

- the watcher turns the SSE firehose into MEANINGFUL transitions only
  (idle->running = turn_started, running->idle = turn_finished, new
  thread, status flip) and ignores repeated identical snapshots
- per-thread rate limiting prevents a flapping thread from flooding
- honest offline: unreachable app -> watch_status offline + reconnect
- bounded ring: recent() only keeps the newest N events
- the webui GET /api/events fan-out: a connected HUD stream receives
  every broadcast, and the freebuff_events run_server flag starts the
  watcher while tests keep it off by default
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

import dourmouse.freebuff_events as fe
from dourmouse.webui import _SSEBroadcast, run_server


def _thread(tid: str, *, status: str = "open", turn: str = "idle",
            updated: int | None = None, title: str = "T", project: str = "/p/x") -> dict[str, Any]:
    return {
        "id": tid,
        "title": title,
        "status": status,
        "turnState": turn,
        "updatedAt": updated if updated is not None else int(time.time() * 1000),
        "projectPath": project,
    }


def _state_event(*threads: dict[str, Any]) -> str:
    return "data: " + json.dumps(
        {"type": "state", "snapshot": {"project": {}, "threads": list(threads)}}
    ) + "\n\n"


def _thread_event(t: dict[str, Any]) -> str:
    return "data: " + json.dumps({"type": "thread", "threadId": t["id"], "thread": t, "items": []}) + "\n\n"


class _FakeFreebuffServer:
    """Serves canned SSE data lines until closed; EOF on stop()."""

    def __init__(self, script: list[str]) -> None:
        script = list(script)
        lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                try:
                    while True:
                        with lock:
                            if not script:
                                break
                            line = script.pop(0)
                        self.wfile.write(line.encode("utf-8"))
                        self.wfile.flush()
                        time.sleep(0.02)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()


@pytest.fixture
def sink():
    out: list[dict[str, Any]] = []
    out_lock = threading.Lock()

    def _sink(payload: dict[str, Any]) -> None:
        with out_lock:
            out.append(payload)

    return out, _sink


@pytest.fixture
def server(monkeypatch, tmp_path):
    """A real UI server bound to a free port (watcher OFF by default)."""
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    from dourmouse.dispatch import DispatchRegistry, Subagent, ToolSpec

    reg = DispatchRegistry()
    reg.register_subagent(
        Subagent(
            name="echo_agent",
            domain="Test",
            description="echoes",
            tools=(
                ToolSpec(
                    name="echo",
                    description="echo back",
                    parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
                    handler=lambda a: f"ECHOED: {a['text']}",
                ),
            ),
        )
    )
    srv = run_server(reg, port=0, client=None, config=None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    port = srv.server_address[1]
    yield srv, port
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def _make_watcher(fake: _FakeFreebuffServer, sink_fn) -> fe.FreebuffEventWatcher:
    return fe.FreebuffEventWatcher(
        sink_fn, base_url=f"http://127.0.0.1:{fake.port}"
    )


# --------------------------------------------------------------------------- #
# Watcher — transitions
# --------------------------------------------------------------------------- #
class TestWatcherTransitions:
    def test_idle_to_running_emits_turn_started(self, sink):
        out, sink_fn = sink
        # First snapshot is the SILENT baseline; only the idle->running flip
        # after it is real activity.
        fake = _FakeFreebuffServer(
            [
                _state_event(_thread("t1", turn="idle")),
                _thread_event(_thread("t1", turn="running")),
            ]
        )
        try:
            w = _make_watcher(fake, sink_fn)
            w.start()
            deadline = time.time() + 5
            while time.time() < deadline and not any(
                e.get("type") == "freebuff_activity" for e in out
            ):
                time.sleep(0.05)
            w.stop()
        finally:
            fake.close()
        acts = [e["activity"] for e in out if e.get("type") == "freebuff_activity"]
        assert acts and acts[0]["kind"] == "turn_started"
        assert acts[0]["thread_id"] == "t1"
        assert acts[0]["turnState"] == "running"

    def test_running_to_idle_emits_turn_finished(self, sink):
        out, sink_fn = sink
        fake = _FakeFreebuffServer(
            [
                _state_event(_thread("t1", turn="running")),
                _thread_event(_thread("t1", turn="idle")),
            ]
        )
        try:
            w = _make_watcher(fake, sink_fn)
            w.start()
            deadline = time.time() + 5
            while time.time() < deadline and not any(
                e.get("type") == "freebuff_activity" and e.get("activity", {}).get("kind") == "turn_finished"
                for e in out
            ):
                time.sleep(0.05)
            w.stop()
        finally:
            fake.close()
        acts = [e["activity"] for e in out if e.get("type") == "freebuff_activity"]
        # Baseline (already running) is silent — never a fake turn_started for
        # pre-existing state; the running->idle flip is the real transition.
        assert acts and acts[0]["kind"] == "turn_finished"

    def test_new_thread_emits_thread_created(self, sink):
        out, sink_fn = sink
        # Baseline has t1; t9 appears AFTER connect -> thread_created.
        fake = _FakeFreebuffServer(
            [
                _state_event(_thread("t1", title="Base")),
                _thread_event(_thread("t9", title="Fresh")),
            ]
        )
        try:
            w = _make_watcher(fake, sink_fn)
            w.start()
            deadline = time.time() + 5
            while time.time() < deadline and not any(
                e.get("type") == "freebuff_activity" for e in out
            ):
                time.sleep(0.05)
            w.stop()
        finally:
            fake.close()
        acts = [e["activity"] for e in out if e.get("type") == "freebuff_activity"]
        assert acts and acts[0]["kind"] == "thread_created"
        assert acts[0]["title"] == "Fresh"
        assert acts[0]["project"] == "x"

    def test_baseline_snapshot_is_silent(self, sink):
        """Connect-time state must NOT flood the feed — pre-existing threads
        are recorded silently, only later activity surfaces."""
        out, sink_fn = sink
        fake = _FakeFreebuffServer(
            [
                _state_event(_thread("t1"), _thread("t2"), _thread("t3")),
                _state_event(_thread("t1"), _thread("t2"), _thread("t3")),
            ]
        )
        try:
            w = _make_watcher(fake, sink_fn)
            w.start()
            time.sleep(0.8)
            w.stop()
        finally:
            fake.close()
        acts = [e for e in out if e.get("type") == "freebuff_activity"]
        assert acts == []  # baseline + identical snapshot = nothing

    def test_status_flip_without_turn_change_emits_status_changed(self, sink):
        out, sink_fn = sink
        fake = _FakeFreebuffServer(
            [
                _state_event(_thread("t1", status="open", turn="idle")),
                _thread_event(_thread("t1", status="closed", turn="idle")),
            ]
        )
        try:
            w = _make_watcher(fake, sink_fn)
            w.start()
            deadline = time.time() + 5
            while time.time() < deadline and not any(
                e.get("type") == "freebuff_activity"
                and e.get("activity", {}).get("kind") == "status_changed"
                for e in out
            ):
                time.sleep(0.05)
            w.stop()
        finally:
            fake.close()
        kinds = [
            e["activity"]["kind"] for e in out if e.get("type") == "freebuff_activity"
        ]
        assert "status_changed" in kinds

    def test_repeated_identical_snapshot_emits_nothing(self, sink):
        out, sink_fn = sink
        t = _thread("t1", turn="idle")
        fake = _FakeFreebuffServer([_state_event(t), _state_event(t), _state_event(t)])
        try:
            w = _make_watcher(fake, sink_fn)
            w.start()
            time.sleep(0.8)
            w.stop()
        finally:
            fake.close()
        acts = [e for e in out if e.get("type") == "freebuff_activity"]
        # Baseline + two identical snapshots = zero activity (no spam).
        assert acts == []

    def test_title_collapsed_and_truncated(self, sink):
        out, sink_fn = sink
        long = "line1\nline2 " + "x" * 300
        fake = _FakeFreebuffServer(
            [
                _state_event(_thread("t1", title="Base")),
                _thread_event(_thread("t2", title=long)),
            ]
        )
        try:
            w = _make_watcher(fake, sink_fn)
            w.start()
            deadline = time.time() + 5
            while time.time() < deadline and not any(
                e.get("type") == "freebuff_activity" for e in out
            ):
                time.sleep(0.05)
            w.stop()
        finally:
            fake.close()
        acts = [e["activity"] for e in out if e.get("type") == "freebuff_activity"]
        assert "\n" not in acts[0]["title"]
        assert len(acts[0]["title"]) <= 120


# --------------------------------------------------------------------------- #
# Watcher — honesty, rate limiting, bounded ring, reconnect
# --------------------------------------------------------------------------- #
class TestWatcherHonesty:
    def test_unreachable_app_emits_offline(self, sink):
        out, sink_fn = sink
        w = fe.FreebuffEventWatcher(
            sink_fn, base_url="http://127.0.0.1:1"  # port 1: nothing listens
        )
        w.start()
        time.sleep(0.5)  # backoff starts at 1s; give it a beat to fail once
        deadline = time.time() + 6
        while time.time() < deadline and not any(
            e.get("type") == "freebuff_watch" and e.get("state") == "offline" for e in out
        ):
            time.sleep(0.1)
        w.stop()
        off = [e for e in out if e.get("type") == "freebuff_watch" and e.get("state") == "offline"]
        assert off, "expected an honest offline event"

    def test_rate_limit_prevents_flood(self, sink):
        out, sink_fn = sink
        # rapid idle->running flapping AFTER the baseline: the same-kind gap
        # collapses repeats, so a flapping thread can't flood the feed.
        t = _thread("t1", turn="idle")
        flapping = [_thread_event(_thread("t1", turn="running")), _thread_event(t)]
        fake = _FakeFreebuffServer([_state_event(t)] + flapping * 5)
        try:
            w = _make_watcher(fake, sink_fn)
            w.start()
            time.sleep(0.9)
            w.stop()
        finally:
            fake.close()
        t1_acts = [
            e["activity"]
            for e in out
            if e.get("type") == "freebuff_activity" and e.get("activity", {}).get("thread_id") == "t1"
        ]
        # Baseline is silent; the flapping yields at most one turn_started and
        # one turn_finished within the gap — never 10 events.
        assert len(t1_acts) <= 2

    def test_recent_ring_bounded(self, sink):
        _out, sink_fn = sink
        fake = _FakeFreebuffServer(
            [_state_event(_thread(f"t{i}", title=f"T{i}")) for i in range(80)]
        )
        try:
            w = _make_watcher(fake, sink_fn)
            w.start()
            deadline = time.time() + 6
            while time.time() < deadline and len(w.recent(1000)) < 40:
                time.sleep(0.05)
            w.stop()
        finally:
            fake.close()
        assert len(w.recent(1000)) <= fe._MAX_EVENTS

    def test_online_event_on_connect(self, sink):
        out, sink_fn = sink
        fake = _FakeFreebuffServer([_state_event(_thread("t1"))])
        try:
            w = _make_watcher(fake, sink_fn)
            w.start()
            deadline = time.time() + 5
            while time.time() < deadline and not any(
                e.get("type") == "freebuff_watch" and e.get("state") == "online" for e in out
            ):
                time.sleep(0.05)
            w.stop()
        finally:
            fake.close()
        assert any(
            e.get("type") == "freebuff_watch" and e.get("state") == "online" for e in out
        )

    def test_first_payload_thread_event_is_silent_baseline(self, sink):
        """Robust baseline: a thread event arriving BEFORE any state snapshot
        is recorded silently, never mislabeled 'created'."""
        out, sink_fn = sink
        fake = _FakeFreebuffServer(
            [
                _thread_event(_thread("t1", title="Existing")),
                _thread_event(_thread("t1", turn="running")),
            ]
        )
        try:
            w = _make_watcher(fake, sink_fn)
            w.start()
            deadline = time.time() + 5
            while time.time() < deadline and not any(
                e.get("type") == "freebuff_activity" for e in out
            ):
                time.sleep(0.05)
            w.stop()
        finally:
            fake.close()
        acts = [e["activity"] for e in out if e.get("type") == "freebuff_activity"]
        assert acts and acts[0]["kind"] == "turn_started"  # no thread_created
        assert all(a["kind"] != "thread_created" for a in acts)


# --------------------------------------------------------------------------- #
# WebUI — fan-out broadcast + run_server flag
# --------------------------------------------------------------------------- #
class TestFanOut:
    def test_broadcast_hub_fans_out_to_all_clients(self):
        hub = _SSEBroadcast()
        got: list[list[dict[str, Any]]] = [[], []]

        class _FakeStream:
            def __init__(self, bucket):
                self._bucket = bucket

            def emit(self, payload):
                self._bucket.append(payload)

        a, b = _FakeStream(got[0]), _FakeStream(got[1])
        hub.register(a)
        hub.register(b)
        hub.broadcast({"type": "freebuff_activity", "activity": {"thread_id": "t1", "kind": "turn_started"}})
        assert len(got[0]) == 1 and len(got[1]) == 1
        assert got[0][0]["activity"]["kind"] == "turn_started"
        hub.unregister(a)
        hub.broadcast({"type": "freebuff_watch", "state": "online"})
        assert len(got[0]) == 1  # unregistered client got nothing more
        assert len(got[1]) == 2

    def test_watcher_emits_into_broadcast_hub(self, sink):
        _out, _sink_fn = sink
        fake = _FakeFreebuffServer(
            [_state_event(_thread("t1", turn="idle")), _thread_event(_thread("t1", turn="running"))]
        )

        hub = _SSEBroadcast()
        received: list[dict[str, Any]] = []

        class _FakeStream:
            def emit(self, payload):
                received.append(payload)

        hub.register(_FakeStream())
        try:
            w = fe.FreebuffEventWatcher(hub.broadcast, base_url=f"http://127.0.0.1:{fake.port}")
            w.start()
            deadline = time.time() + 5
            while time.time() < deadline and not any(
                r.get("type") == "freebuff_activity" for r in received
            ):
                time.sleep(0.05)
            w.stop()
        finally:
            fake.close()
        assert any(
            r.get("type") == "freebuff_activity"
            and r.get("activity", {}).get("kind") == "turn_started"
            for r in received
        )

    def test_run_server_default_has_no_watcher(self):
        """Hermetic default: freebuff_events=False -> hub exists, watcher None."""
        from dourmouse.dispatch import DispatchRegistry, Subagent, ToolSpec

        reg = DispatchRegistry()
        reg.register_subagent(
            Subagent(
                name="echo_agent",
                domain="Test",
                description="echoes",
                tools=(ToolSpec(name="echo", description="e", parameters={"type": "object", "properties": {}}, handler=lambda a: "ok"),),
            )
        )
        srv = run_server(reg, port=0, client=None, config=None)
        try:
            assert srv.events_broadcast is not None
            assert srv.freebuff_watcher is None
        finally:
            srv.server_close()

    def test_events_endpoint_replays_watch_status_for_late_subscriber(self, server):
        """A client attaching AFTER the watcher connected still learns the
        watch state — the handler replays the current status on register."""
        srv, port = server
        hub = srv.events_broadcast
        assert hub is not None

        class _FakeWatcher:
            online = True

        srv.freebuff_watcher = _FakeWatcher()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/api/events")
        resp = conn.getresponse()
        first = resp.readline()  # first data line is the replay
        conn.close()
        assert first.startswith(b"data:")
        payload = json.loads(first[len(b"data:"):].strip())
        assert payload["type"] == "freebuff_watch"
        assert payload["state"] == "online"
        srv.freebuff_watcher = None

    def test_events_endpoint_streams_broadcast(self, server):
        """A real GET /api/events client receives the hub's broadcasts."""
        srv, port = server
        hub = srv.events_broadcast
        assert hub is not None
        events: list[str] = []

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/api/events")
        resp = conn.getresponse()

        def _read_loop():
            try:
                while True:
                    line = resp.readline()
                    if not line:
                        break
                    if line.startswith(b"data:"):
                        events.append(line[len(b"data:"):].strip().decode())
            except (OSError, http.client.RemoteDisconnected):
                pass

        reader = threading.Thread(target=_read_loop, daemon=True)
        reader.start()
        time.sleep(0.2)
        hub.broadcast({"type": "freebuff_activity", "activity": {"thread_id": "t1", "kind": "turn_started", "title": "X"}})
        time.sleep(0.3)
        conn.close()
        reader.join(timeout=2)
        assert any("freebuff_activity" in e for e in events)
