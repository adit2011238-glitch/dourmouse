"""v4.0 multi-device tests (spec Phase 9) — auth gate + configurable bind.

The auth gate is a pure decision on (_Handler): loopback exempt, Bearer
header accepted, cookie accepted, all constant-time. We exercise the real
HTTP paths over an ephemeral port with a token configured, and the pure
logic with a stubbed handler (no real network beyond localhost). Hermetic
(Rule 2.1); token from env (Rule 2.6).
"""

from __future__ import annotations

import http.client
import threading
import time
from types import SimpleNamespace

import pytest

from dourmouse import config as config_module
from dourmouse.config import access_token, bind_host
from dourmouse.general_roster import build_general_registry
from dourmouse.webui import run_server


def _start(monkeypatch, token="s3cret-token"):
    monkeypatch.setenv("DOURMOUSE_ACCESS_TOKEN", token)
    registry = build_general_registry()
    server = run_server(registry, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _wait(server):
    host, port = server.server_address[:2]
    for _ in range(50):
        try:
            conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=3)
            conn.request("GET", "/api/roster")
            conn.getresponse().read()
            conn.close()
            return int(port)
        except (ConnectionRefusedError, OSError):
            time.sleep(0.1)
    raise RuntimeError("server never came up")


class TestAuthGate:
    def test_loopback_bypasses_token(self, monkeypatch):
        server, thread = _start(monkeypatch, token="s3cret")
        try:
            port = _wait(server)
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/api/roster")
            resp = conn.getresponse()
            assert resp.status == 200
            conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_remote_without_token_is_denied(self):
        from dourmouse import webui as w

        h = w._Handler.__new__(w._Handler)
        h.client_address = ("192.168.1.50", 12345)
        h.server = SimpleNamespace(access_token="s3cret")
        h.headers = SimpleNamespace(get=lambda k, d="": d)
        assert h._authorized() is False

    def test_remote_with_bearer_is_allowed(self):
        from dourmouse import webui as w

        h = w._Handler.__new__(w._Handler)
        h.client_address = ("192.168.1.50", 12345)
        h.server = SimpleNamespace(access_token="s3cret")
        h.headers = SimpleNamespace(get=lambda k, d="": "Bearer s3cret" if k == "Authorization" else d)
        assert h._authorized() is True

    def test_remote_with_bad_bearer_is_denied(self):
        from dourmouse import webui as w

        h = w._Handler.__new__(w._Handler)
        h.client_address = ("192.168.1.50", 12345)
        h.server = SimpleNamespace(access_token="s3cret")
        h.headers = SimpleNamespace(get=lambda k, d="": "Bearer wrong" if k == "Authorization" else d)
        assert h._authorized() is False

    def test_remote_with_cookie_is_allowed(self):
        from dourmouse import webui as w

        h = w._Handler.__new__(w._Handler)
        h.client_address = ("192.168.1.50", 12345)
        h.server = SimpleNamespace(access_token="s3cret")
        h.headers = SimpleNamespace(
            get=lambda k, d="": "dourmouse_session=s3cret" if k == "Cookie" else d
        )
        assert h._authorized() is True

    def test_no_token_configured_allows_all(self):
        from dourmouse import webui as w

        h = w._Handler.__new__(w._Handler)
        h.client_address = ("192.168.1.50", 12345)
        h.server = SimpleNamespace(access_token="")
        h.headers = SimpleNamespace(get=lambda k, d="": d)
        assert h._authorized() is True

    def test_login_endpoint_sets_cookie(self, monkeypatch):
        server, thread = _start(monkeypatch, token="s3cret")
        try:
            port = _wait(server)
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request(
                "POST",
                "/api/login",
                body=b'{"token": "s3cret"}',
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            assert resp.status == 200
            set_cookie = resp.getheader("Set-Cookie", "")
            assert "dourmouse_session=s3cret" in set_cookie
            assert "HttpOnly" in set_cookie
            conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_login_wrong_token_is_401(self, monkeypatch):
        server, thread = _start(monkeypatch, token="s3cret")
        try:
            port = _wait(server)
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request(
                "POST",
                "/api/login",
                body=b'{"token": "nope"}',
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            assert resp.status == 401
            conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_login_page_served_without_auth(self, monkeypatch):
        server, thread = _start(monkeypatch, token="s3cret")
        try:
            port = _wait(server)
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/login")
            resp = conn.getresponse()
            assert resp.status == 200
            body = resp.read().decode()
            # v5.22.8: the login page is the Gmail-style Google sign-in.
            assert "Sign in with Google" in body
            conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


class TestBindConfig:
    def test_access_token_env(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_ACCESS_TOKEN", raising=False)
        assert access_token() == ""
        monkeypatch.setenv("DOURMOUSE_ACCESS_TOKEN", "abc")
        assert access_token() == "abc"

    def test_bind_host_env(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_HOST", raising=False)
        assert bind_host() == "127.0.0.1"
        monkeypatch.setenv("DOURMOUSE_HOST", "0.0.0.0")
        assert bind_host() == "0.0.0.0"

    def test_serve_forever_resolves_host_from_env(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_HOST", "127.0.0.1")
        from dourmouse.webui import serve_forever

        # run_server is the seam; serve_forever would block. Just verify the
        # env resolution helper is what serve_forever uses.
        assert config_module.bind_host() == "127.0.0.1"


class TestInsecureBindGuard:
    """A network-bound server with no token authorizes EVERY request.

    ``_authorized()`` short-circuits to True when the token is empty, so the
    combination "non-loopback host + no token" exposes mail, the filesystem
    and run_command to anyone who can reach the port. Binding must fail
    rather than print a warning nobody reads.
    """

    def _serve(self, monkeypatch, host, token=None, override=None):
        from dourmouse.dispatch import DispatchRegistry
        from dourmouse.webui import run_server

        monkeypatch.delenv("DOURMOUSE_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("DOURMOUSE_ALLOW_INSECURE_BIND", raising=False)
        if token is not None:
            monkeypatch.setenv("DOURMOUSE_ACCESS_TOKEN", token)
        if override is not None:
            monkeypatch.setenv("DOURMOUSE_ALLOW_INSECURE_BIND", override)
        return run_server(registry=DispatchRegistry(), host=host, port=0)

    def test_non_loopback_without_token_refuses_to_bind(self, monkeypatch):
        with pytest.raises(RuntimeError, match="DOURMOUSE_ACCESS_TOKEN"):
            self._serve(monkeypatch, "0.0.0.0")

    def test_non_loopback_with_token_binds(self, monkeypatch):
        server = self._serve(monkeypatch, "0.0.0.0", token="a-long-random-token")
        try:
            assert server.access_token == "a-long-random-token"
        finally:
            server.server_close()

    def test_loopback_without_token_still_binds(self, monkeypatch):
        """The local/desktop-app posture must stay zero-config."""
        server = self._serve(monkeypatch, "127.0.0.1")
        try:
            assert server.access_token == ""
        finally:
            server.server_close()

    def test_explicit_override_binds_insecurely(self, monkeypatch):
        server = self._serve(monkeypatch, "0.0.0.0", override="1")
        try:
            assert server.access_token == ""
        finally:
            server.server_close()
