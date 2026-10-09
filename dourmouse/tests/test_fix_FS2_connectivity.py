"""FS2 P3-56: connectivity.diagnose tries every address and files timeouts as TIMEOUT."""

from __future__ import annotations

import errno
import http.server
import socket
import ssl
import threading
import time

from dourmouse.security import connectivity as cx


def _serve(status: int = 200):
    class H(http.server.BaseHTTPRequestHandler):
        def do_HEAD(self):  # noqa: N802
            self.send_response(status)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _silent_server():
    """Accepts a connection and never says anything."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    held = []

    def run():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            held.append(c)

    threading.Thread(target=run, daemon=True).start()
    return srv, held


def _two_addresses(port, first_family):
    def fake(host, p, *a, **k):
        v6 = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", port, 0, 0))
        v4 = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))
        return [v6, v4] if first_family == socket.AF_INET6 else [v4, v6]
    return fake


def test_a_dead_first_address_does_not_hide_a_working_second_one(monkeypatch):
    srv = _serve(200)
    port = srv.server_address[1]
    monkeypatch.setattr(socket, "getaddrinfo", _two_addresses(port, socket.AF_INET6))
    try:
        r = cx.diagnose(f"http://localhost:{port}/", allow_loopback=True)
    finally:
        srv.shutdown()
    assert r["category"] == cx.OK
    tcp = [s for s in r["steps"] if s["step"] == "tcp"]
    assert [s["ok"] for s in tcp] == [False, True]


def test_all_addresses_failing_reports_the_most_informative_failure(monkeypatch):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    monkeypatch.setattr(socket, "getaddrinfo", _two_addresses(port, socket.AF_INET6))
    r = cx.diagnose(f"http://localhost:{port}/", allow_loopback=True)
    assert r["category"] == cx.UNREACHABLE
    assert len([x for x in r["steps"] if x["step"] == "tcp"]) == 2


def test_routing_failure_on_one_family_and_timeout_on_the_other_is_a_timeout(monkeypatch):
    class Sock:
        def __init__(self, family, *a, **k):
            self.family = family

        def settimeout(self, t):
            pass

        def connect(self, addr):
            if self.family == socket.AF_INET6:
                raise OSError(errno.ENETUNREACH, "Network is unreachable")
            raise TimeoutError()

        def close(self):
            pass

    monkeypatch.setattr(socket, "getaddrinfo", _two_addresses(80, socket.AF_INET6))
    monkeypatch.setattr(socket, "socket", Sock)
    assert cx.diagnose("http://localhost/", allow_loopback=True)["category"] == cx.TIMEOUT


def test_tls_handshake_timeout_is_timeout_not_interception(monkeypatch):
    srv, held = _silent_server()
    port = srv.getsockname()[1]

    class Ctx:
        def wrap_socket(self, sock, server_hostname=None):
            raise TimeoutError("_ssl.c:1000: The handshake operation timed out")

    monkeypatch.setattr(ssl, "create_default_context", lambda *a, **k: Ctx())
    try:
        r = cx.diagnose(f"https://127.0.0.1:{port}/", timeout=1.0, allow_loopback=True)
    finally:
        srv.close()
    assert r["category"] == cx.TIMEOUT and "interception" not in r["why"]


def test_a_real_tls_failure_is_still_tls(monkeypatch):
    srv, _held = _silent_server()
    port = srv.getsockname()[1]

    class Ctx:
        def wrap_socket(self, sock, server_hostname=None):
            raise ssl.SSLError("bad record mac")

    monkeypatch.setattr(ssl, "create_default_context", lambda *a, **k: Ctx())
    try:
        r = cx.diagnose(f"https://127.0.0.1:{port}/", timeout=1.0, allow_loopback=True)
    finally:
        srv.close()
    assert r["category"] == cx.TLS


def test_http_step_timeout_is_timeout():
    srv, _held = _silent_server()
    port = srv.getsockname()[1]
    t0 = time.monotonic()
    try:
        r = cx.diagnose(f"http://127.0.0.1:{port}/", timeout=0.5, allow_loopback=True)
    finally:
        srv.close()
    assert r["category"] == cx.TIMEOUT and time.monotonic() - t0 < 10
