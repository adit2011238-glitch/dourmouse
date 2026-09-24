"""Finding #086 (X-9 / R0-SEC): SSRF-safe fetching of model-supplied URLs.

The redirect and pinning tests run real HTTP servers on 127.0.0.1. Loopback
is (correctly) not public, so those tests relax only ``is_public_address``
to accept 127.0.0.1; every other address keeps the real rule. That lets a
real first hop succeed and proves the NEXT hop is still checked.
"""

from __future__ import annotations

import http.server
import ipaddress
import socket
import threading
import urllib.error

import pytest

from dourmouse import net_guard
from dourmouse.general_roster import _fetch_url_tool
from dourmouse.net_guard import FetchRefused, guarded_urlopen, is_public_address, vet_host


def _ip(s: str):
    return ipaddress.ip_address(s)


class TestIsPublicAddress:
    @pytest.mark.parametrize("addr", [
        "127.0.0.1", "10.0.0.5", "172.16.3.4", "192.168.1.1",
        "169.254.169.254",  # cloud metadata
        "100.64.0.1",       # CGNAT: the old range list let this through
        "0.0.0.0", "224.0.0.1", "192.0.2.10",
        "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1",
    ])
    def test_internal_addresses_are_not_public(self, addr):
        assert is_public_address(_ip(addr)) is False

    @pytest.mark.parametrize("addr", ["93.184.215.14", "1.1.1.1", "2606:4700:4700::1111", "::ffff:1.1.1.1"])
    def test_public_addresses_are_public(self, addr):
        assert is_public_address(_ip(addr)) is True


def _answers(*ips):
    def fake(host, port, *a, **k):
        out = []
        for ip in ips:
            fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
            out.append((fam, socket.SOCK_STREAM, 6, "", (ip, port)))
        return out
    return fake


class TestVetHost:
    def test_every_answer_is_checked_not_just_the_first(self, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", _answers("93.184.215.14", "10.0.0.7"))
        with pytest.raises(FetchRefused, match="10.0.0.7"):
            vet_host("mixed.test", 80)

    def test_all_public_answers_pass(self, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", _answers("93.184.215.14", "1.1.1.1"))
        assert len(vet_host("ok.test", 443)) == 2

    def test_an_unresolvable_name_is_a_network_error_not_a_refusal(self, monkeypatch):
        def fail(*a, **k):
            raise socket.gaierror("nodename nor servname provided")
        monkeypatch.setattr(socket, "getaddrinfo", fail)
        with pytest.raises(socket.gaierror):
            vet_host("nope.invalid", 80)


class _Handler(http.server.BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, dict[str, str], bytes]] = {}

    def do_GET(self):  # noqa: N802
        status, headers, body = self.routes.get(self.path, (404, {}, b"missing"))
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def server(monkeypatch):
    """A real local HTTP server; only 127.0.0.1 is treated as public."""
    real = net_guard.is_public_address
    monkeypatch.setattr(
        net_guard, "is_public_address",
        lambda a: a == ipaddress.ip_address("127.0.0.1") or real(a),
    )
    routes: dict[str, tuple[int, dict[str, str], bytes]] = {}
    handler = type("H", (_Handler,), {"routes": routes})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", routes
    finally:
        srv.shutdown()
        srv.server_close()


class TestRedirects:
    def test_a_normal_redirect_is_followed(self, server):
        base, routes = server
        routes["/a"] = (302, {"Location": "/b"}, b"")
        routes["/b"] = (200, {"Content-Type": "text/plain"}, b"landed")
        with guarded_urlopen(base + "/a", timeout=5) as resp:
            assert resp.read() == b"landed"

    @pytest.mark.parametrize("target", [
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/admin",
        "http://[::1]:8765/api",
    ])
    def test_a_redirect_to_an_internal_address_is_refused(self, server, target):
        """The X-9 hole: the first hop passed, the second went internal."""
        base, routes = server
        routes["/go"] = (302, {"Location": target}, b"")
        with pytest.raises(FetchRefused, match="private/internal"):
            guarded_urlopen(base + "/go", timeout=5)

    def test_a_redirect_to_a_non_web_scheme_is_refused(self, server):
        base, routes = server
        routes["/go"] = (302, {"Location": "ftp://example.com/file"}, b"")
        with pytest.raises(FetchRefused, match="non-web scheme"):
            guarded_urlopen(base + "/go", timeout=5)

    def test_the_redirect_chain_is_capped(self, server):
        base, routes = server
        for i in range(10):
            routes[f"/r{i}"] = (302, {"Location": f"/r{i + 1}"}, b"")
        routes["/r10"] = (200, {}, b"too far")
        with pytest.raises(urllib.error.HTTPError):
            guarded_urlopen(base + "/r0", timeout=5)


class TestConnectionIsPinned:
    def test_the_connection_goes_to_the_address_that_was_vetted(self, server, monkeypatch):
        """DNS rebinding: a name that answers public for the check and
        internal for the connect. The guarded path resolves once per
        connection and connects to exactly what it vetted, so the second
        answer is never used."""
        base, routes = server
        port = int(base.rsplit(":", 1)[1])
        routes["/"] = (200, {}, b"pinned")
        answers = iter([
            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))],
            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.9.9.9", port))],
        ])
        calls = []

        def rebinding(host, p, *a, **k):
            calls.append(host)
            return next(answers)

        monkeypatch.setattr(socket, "getaddrinfo", rebinding)
        with guarded_urlopen(f"http://rebind.test:{port}/", timeout=5) as resp:
            assert resp.read() == b"pinned"
        assert calls == ["rebind.test"]


class TestFetchUrlTool:
    def test_redirect_into_the_network_comes_back_as_refused(self, server):
        base, routes = server
        routes["/p"] = (302, {"Location": "http://169.254.169.254/"}, b"")
        out = _fetch_url_tool({"url": base + "/p"})
        assert out.startswith("REFUSED:")
        assert "169.254.169.254" in out

    def test_a_real_page_is_fetched_through_the_guard(self, server):
        base, routes = server
        routes["/ok"] = (200, {"Content-Type": "text/html"}, b"<p>Hello &amp; welcome</p>")
        out = _fetch_url_tool({"url": base + "/ok"})
        assert out.startswith("FETCHED")
        assert "Hello & welcome" in out

    def test_environment_proxies_are_not_used(self, server, monkeypatch):
        """Through a proxy the real destination cannot be vetted."""
        base, routes = server
        routes["/ok"] = (200, {}, b"direct")
        monkeypatch.setenv("http_proxy", "http://10.0.0.1:3128")
        monkeypatch.setenv("HTTP_PROXY", "http://10.0.0.1:3128")
        with guarded_urlopen(base + "/ok", timeout=5) as resp:
            assert resp.read() == b"direct"
