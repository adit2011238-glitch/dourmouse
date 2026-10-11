"""Fix agent FD1: P3-28, an HTTP error from the bridge relays the bridge's own message."""

from __future__ import annotations

import http.server
import threading

import pytest

from dourmouse import gods_eye


class _H(http.server.BaseHTTPRequestHandler):
    code = 503
    body = b'{"error": "action queue full - no browser tab appears to be draining it"}'

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
        self.send_response(type(self).code)
        self.send_header("Content-Length", str(len(type(self).body)))
        self.end_headers()
        self.wfile.write(type(self).body)

    def log_message(self, *a):
        pass


@pytest.fixture
def bridge(monkeypatch):
    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(gods_eye, "gods_eye_url", lambda: f"http://127.0.0.1:{srv.server_port}")
    yield
    srv.shutdown()
    srv.server_close()


def test_503_from_the_bridge_is_relayed_not_reported_as_unreachable(bridge):
    with pytest.raises(RuntimeError) as exc:
        gods_eye.run_globe_action("flyTo", {})
    msg = str(exc.value)
    assert "action queue full" in msg and "503" in msg
    assert "not reachable" not in msg and "NOT CONFIGURED" not in msg


def test_400_with_a_plain_text_body_is_relayed(bridge):
    _H.code, _H.body = 400, b"bad request text"
    with pytest.raises(RuntimeError, match="bad request text"):
        gods_eye.run_globe_action("x", {})


def test_a_refused_connection_is_still_not_configured(monkeypatch):
    monkeypatch.setattr(gods_eye, "gods_eye_url", lambda: "http://127.0.0.1:1")
    with pytest.raises(RuntimeError, match="NOT CONFIGURED"):
        gods_eye.run_globe_action("x", {})
