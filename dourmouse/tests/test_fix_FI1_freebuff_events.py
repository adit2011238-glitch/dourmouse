"""FI1 fix P4-23: FreebuffEventWatcher must deliver small SSE events at once
(read1, not a blocking read(4096)) and treat a quiet stream as idle, not
offline."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import dourmouse.freebuff_events as fe


def _thread(turn: str) -> dict:
    return {"id": "t1", "title": "T", "status": "open", "turnState": turn,
            "updatedAt": int(time.time() * 1000), "projectPath": "/p/x"}


def _state(turn: str) -> str:
    return "data: " + json.dumps({"type": "state", "snapshot": {"threads": [_thread(turn)]}}) + "\n\n"


class _HoldOpenServer:
    """Sends each (delay_seconds, text) chunk, then keeps the stream open
    (no EOF) until closed. Uses chunked encoding like a real SSE endpoint."""

    def __init__(self, script):
        done = threading.Event()
        self.done = done

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                try:
                    for delay, text in script:
                        time.sleep(delay)
                        data = text.encode("utf-8")
                        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                        self.wfile.flush()
                    done.wait(30)
                except OSError:
                    pass

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._srv.server_address[1]
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    def close(self):
        self.done.set()
        self._srv.shutdown()
        self._srv.server_close()


def _wait(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def test_small_events_are_delivered_without_waiting_for_4kb():
    fake = _HoldOpenServer([(0.0, _state("idle")), (0.1, _state("running"))])
    out: list[dict] = []
    w = fe.FreebuffEventWatcher(out.append, base_url=f"http://127.0.0.1:{fake.port}")
    try:
        w.start()
        assert _wait(lambda: any(e.get("activity", {}).get("kind") == "turn_started" for e in out)), out
        assert not any(e.get("state") == "offline" for e in out)
    finally:
        w.stop()
        fake.close()


def test_stalled_stream_is_reconnected_quietly_not_reported_offline(monkeypatch):
    monkeypatch.setattr(fe, "_READ_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(fe, "_BACKOFF", (0.05,))
    connects = []

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            connects.append(1)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            time.sleep(3)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    out: list[dict] = []
    w = fe.FreebuffEventWatcher(out.append, base_url=f"http://127.0.0.1:{srv.server_address[1]}")
    try:
        w.start()
        assert _wait(lambda: len(connects) >= 2, timeout=5), connects
        assert not any(e.get("state") == "offline" for e in out), out
    finally:
        w.stop()
        srv.shutdown()
        srv.server_close()
