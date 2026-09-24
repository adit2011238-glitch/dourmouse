"""Finding #104 (MS-6): the connection failure taxonomy, one category per
observed failure, against real local sockets where possible."""

from __future__ import annotations

import errno
import http.server
import socket
import threading

import pytest

from dourmouse.security import connectivity as cx


def _serve(status: int):
    class H(http.server.BaseHTTPRequestHandler):
        def do_HEAD(self):  # noqa: N802
            self.send_response(status)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_a_working_server_is_ok():
    srv = _serve(200)
    try:
        r = cx.diagnose(f"http://127.0.0.1:{srv.server_address[1]}/", allow_loopback=True)
    finally:
        srv.shutdown()
    assert r["category"] == cx.OK and [s["step"] for s in r["steps"]] == ["dns", "tcp", "http"]


def test_a_5xx_is_server_side():
    srv = _serve(503)
    try:
        r = cx.diagnose(f"http://127.0.0.1:{srv.server_address[1]}/", allow_loopback=True)
    finally:
        srv.shutdown()
    assert r["category"] == cx.SERVER and "503" in r["why"]


def test_a_closed_port_is_unreachable():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens here now
    r = cx.diagnose(f"http://127.0.0.1:{port}/", allow_loopback=True)
    assert r["category"] == cx.UNREACHABLE


def test_a_name_that_does_not_resolve_is_dns():
    assert cx.diagnose("https://nothing-here.invalid")["category"] == cx.DNS


def _resolves_to(ip):
    def fake(host, port, *a, **k):
        fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(fam, socket.SOCK_STREAM, 6, "", (ip, port))]
    return fake


@pytest.mark.parametrize("ip", ["0.0.0.0", "::"])
def test_a_sinkholed_name_is_blocked(monkeypatch, ip):
    monkeypatch.setattr(socket, "getaddrinfo", _resolves_to(ip))
    r = cx.diagnose("https://youtube.com")
    assert r["category"] == cx.BLOCKED and "on purpose" in r["why"]


def test_an_internal_address_is_not_diagnosed(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _resolves_to("192.168.1.1"))
    r = cx.diagnose("https://router.example")
    assert r["category"] == cx.UNKNOWN and "internal" in r["why"]


class _Sock:
    def __init__(self, exc):
        self.exc = exc

    def settimeout(self, t):
        pass

    def connect(self, addr):
        raise self.exc

    def close(self):
        pass


@pytest.mark.parametrize(("exc", "category"), [
    (TimeoutError(), cx.TIMEOUT),
    (OSError(errno.ENETUNREACH, "Network is unreachable"), cx.ROUTING),
    (OSError(errno.EHOSTUNREACH, "No route to host"), cx.ROUTING),
])
def test_connect_failures(monkeypatch, exc, category):
    monkeypatch.setattr(socket, "getaddrinfo", _resolves_to("93.184.215.14"))
    monkeypatch.setattr(socket, "socket", lambda *a, **k: _Sock(exc))
    assert cx.diagnose("https://example.com")["category"] == category
