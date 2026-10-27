"""ST-1 (stress test 2026-10-27): one /api/events client that stopped reading blocked every
broadcast, and so the agent thread that made it, for the whole 60 s socket timeout. Each
client now has its own queue and writer thread."""

from __future__ import annotations

import http.client
import json
import threading
import time

from dourmouse.webui import _SSEBroadcast, _SSEStream


class _BlockingFile:
    """A socket file that blocks every write until released, like a client that stopped reading."""

    def __init__(self):
        self.release = threading.Event()
        self.written = []

    def write(self, b):
        self.release.wait(10)
        self.written.append(b)

    def flush(self):
        pass


class _ListFile:
    def __init__(self):
        self.written = []

    def write(self, b):
        self.written.append(b)

    def flush(self):
        pass


def test_a_client_that_never_reads_does_not_block_the_producer():
    hub = _SSEBroadcast()
    slow = _SSEStream(_BlockingFile())
    hub.register(slow)
    t0 = time.perf_counter()
    for i in range(200):
        hub.broadcast({"type": "agent_trace", "entry": {"text": "x" * 1000, "i": i}})
    assert time.perf_counter() - t0 < 1.0  # was 60 s per event before
    slow._wfile.release.set()
    slow.close()


def test_a_healthy_client_still_gets_every_event_in_order():
    hub = _SSEBroadcast()
    good = _SSEStream(_ListFile())
    slow = _SSEStream(_BlockingFile())
    hub.register(good)
    hub.register(slow)
    for i in range(50):
        hub.broadcast({"type": "t", "i": i})
    deadline = time.time() + 3
    while len(good._wfile.written) < 50 and time.time() < deadline:
        time.sleep(0.02)
    got = [json.loads(b.decode().removeprefix("data: ").strip())["i"] for b in good._wfile.written]
    assert got == list(range(50))
    slow._wfile.release.set()
    good.close()
    slow.close()


def test_events_past_the_cap_are_dropped_for_that_client_only():
    hub = _SSEBroadcast()
    slow = _SSEStream(_BlockingFile())
    hub.register(slow)
    for i in range(_SSEStream._OUTBOX_CAP + 300):
        hub.broadcast({"type": "t", "i": i})
    assert slow.dropped > 0
    slow._wfile.release.set()
    slow.close()


def test_a_client_that_gave_up_leaves_the_hub():
    hub = _SSEBroadcast()

    class _Broken:
        def write(self, b):
            raise OSError("timed out")

        def flush(self):
            pass

    broken = _SSEStream(_Broken())
    hub.register(broken)
    hub.broadcast({"type": "t"})
    deadline = time.time() + 3
    while not broken.client_gone and time.time() < deadline:
        time.sleep(0.02)
    hub.broadcast({"type": "t"})  # the next broadcast removes it
    assert broken not in hub._clients


def test_streams_without_offer_still_work_synchronously():
    """Test doubles and any other registered object with only emit() keep the old behaviour."""
    hub = _SSEBroadcast()
    seen = []
    hub.register(type("S", (), {"emit": staticmethod(seen.append)})())
    hub.broadcast({"type": "x"})
    assert seen == [{"type": "x"}]


def test_an_overlong_integer_in_a_json_body_is_a_400_not_a_500(tmp_path, monkeypatch):
    """SEC1-16, confirmed by the stress run: json.loads raises ValueError for 4300+ digits."""
    import dourmouse.webui as webui_module
    from dourmouse.tests.test_webui import _echo_registry

    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    srv = webui_module.run_server(_echo_registry(), port=0, client=None, config=None)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("POST", "/api/chat", body=b'{"prompt":' + b"9" * 6000 + b"}", headers={"Content-Type": "application/json"})
        assert c.getresponse().status == 400
    finally:
        srv.shutdown()
        srv.server_close()
