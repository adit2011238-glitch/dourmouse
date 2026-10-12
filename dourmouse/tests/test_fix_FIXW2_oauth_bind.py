"""FIX-W2 R2-1: the Google sign-in bind must work when the OAuth callback lands
on a different host than the one the page was opened on.

DOURMOUSE_OAUTH_REDIRECT_BASE pins the callback to a loopback URI (reached over
an SSH tunnel) while the owner opened the UI on a Tailscale address. A host-only
bind cookie set at the start host never reaches the callback host. The start
route therefore hands the browser over to the callback host first, so the bind
cookie is set by the host that will receive the callback. These tests run over
real in-process HTTP with two different Host header values.
"""
from __future__ import annotations

import http.client
import threading

import pytest

from dourmouse.dispatch import DispatchRegistry
from dourmouse.webui import run_server

_IDENTITY = {"email": "owner@example.com", "name": "Owner", "picture": "", "sub": "s1"}
_TAILSCALE = "100.64.0.5"


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    srv = run_server(DispatchRegistry(), port=0, client=None, config=None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv, srv.server_address[1]
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


@pytest.fixture(autouse=True)
def _google(monkeypatch):
    from dourmouse import google_auth

    # A Tailscale name reaching the server through a local proxy is the owner's
    # documented DOURMOUSE_ALLOWED_HOSTS setup.
    monkeypatch.setenv("DOURMOUSE_ALLOWED_HOSTS", _TAILSCALE)

    monkeypatch.setattr(google_auth, "google_configured", lambda: True)
    monkeypatch.setattr(
        google_auth, "authorization_url",
        lambda *a, **k: "https://accounts.google.com/o/oauth2/v2/auth?fake=1")
    monkeypatch.setattr(
        google_auth, "exchange_code",
        lambda *a, **k: {"id_token": "tok", "access_token": "a", "refresh_token": "r"})
    monkeypatch.setattr(google_auth, "verify_id_token", lambda *a, **k: dict(_IDENTITY))


class Browser:
    """A tiny cookie-keeping browser: cookies are per host name, as in real life."""

    def __init__(self, port):
        self.port = port
        self.jar: dict[str, dict[str, str]] = {}

    def get(self, host, path):
        headers = {"Host": f"{host}:{self.port}"}
        cookies = self.jar.get(host, {})
        if cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        for raw in resp.msg.get_all("Set-Cookie") or []:
            name, _, rest = raw.partition("=")
            self.jar.setdefault(host, {})[name] = rest.split(";")[0]
        out = (resp.status, resp.getheader("Location"), body)
        conn.close()
        return out

    def follow_to_google(self, host, path):
        """Follow same-server redirects the way a browser would until Google."""
        for _ in range(5):
            status, location, _ = self.get(host, path)
            assert status == 302, (status, location)
            if location.startswith("https://accounts.google.com"):
                return host, location
            assert location.startswith("http://"), location
            rest = location[len("http://"):]
            netloc, _, path_part = rest.partition("/")
            host = netloc.rsplit(":", 1)[0]
            path = "/" + path_part
        raise AssertionError("redirect loop")


def _only_state(srv):
    with srv.oauth_lock:
        assert len(srv.oauth_pending) == 1, srv.oauth_pending
        return next(iter(srv.oauth_pending))


@pytest.fixture
def pinned(server, monkeypatch):
    srv, port = server
    monkeypatch.setenv("DOURMOUSE_OAUTH_REDIRECT_BASE", f"http://127.0.0.1:{port}")
    return srv, port


def test_signin_started_on_another_host_completes_on_the_pinned_callback_host(pinned):
    srv, port = pinned
    owner = Browser(port)
    landed_on, _ = owner.follow_to_google(_TAILSCALE, "/api/auth/google/start")
    assert landed_on == "127.0.0.1"  # the bind cookie was set by the callback host
    state = _only_state(srv)
    assert "dourmouse_oauth_bind" in owner.jar["127.0.0.1"]

    status, location, _ = owner.get("127.0.0.1", f"/api/auth/google/callback?code=c&state={state}")
    assert status == 302 and location == "/"
    assert "dourmouse_user_session" in owner.jar["127.0.0.1"]


def test_a_third_party_browser_still_cannot_redeem_the_flow(pinned):
    """Login CSRF: the attacker starts a flow, the owner's browser is sent to the
    callback link. The owner's browser holds no bind cookie for that flow."""
    srv, port = pinned
    attacker = Browser(port)
    attacker.follow_to_google(_TAILSCALE, "/api/auth/google/start")
    state = _only_state(srv)

    victim = Browser(port)
    status, _, body = victim.get("127.0.0.1", f"/api/auth/google/callback?code=c&state={state}")
    assert status == 400
    assert "dourmouse_user_session" not in victim.jar.get("127.0.0.1", {})
    assert srv.oauth_pending == {}  # the state is spent


def test_a_cookie_from_the_start_host_is_not_what_authorises_the_callback(pinned):
    """A browser that only ever visited the tailscale host (its own bind cookie
    for some other flow) cannot finish a flow it did not start."""
    srv, port = pinned
    attacker = Browser(port)
    attacker.follow_to_google(_TAILSCALE, "/api/auth/google/start")
    state = _only_state(srv)
    victim = Browser(port)
    victim.jar["127.0.0.1"] = {"dourmouse_oauth_bind": "x" * 43}
    status, _, _ = victim.get("127.0.0.1", f"/api/auth/google/callback?code=c&state={state}")
    assert status == 400


def test_handoff_redirect_carries_no_flow_state_and_creates_none(pinned):
    srv, port = pinned
    b = Browser(port)
    status, location, _ = b.get(_TAILSCALE, "/api/auth/google/start")
    assert status == 302
    assert location.startswith(f"http://127.0.0.1:{port}/api/auth/google/start")
    assert srv.oauth_pending == {}
    assert b.jar == {}


def test_handoff_does_not_loop_when_the_hosts_still_differ(pinned):
    srv, port = pinned
    b = Browser(port)
    status, location, _ = b.get(_TAILSCALE, "/api/auth/google/start?handoff=1")
    assert status == 302 and location.startswith("https://accounts.google.com")


def test_localhost_versus_pinned_loopback_is_also_handed_over(pinned):
    srv, port = pinned
    owner = Browser(port)
    landed_on, _ = owner.follow_to_google("localhost", "/api/auth/google/start")
    assert landed_on == "127.0.0.1"
    state = _only_state(srv)
    status, location, _ = owner.get("127.0.0.1", f"/api/auth/google/callback?code=c&state={state}")
    assert status == 302 and location == "/"


def test_same_host_flow_is_not_handed_over(pinned):
    srv, port = pinned
    owner = Browser(port)
    status, location, _ = owner.get("127.0.0.1", "/api/auth/google/start")
    assert status == 302 and location.startswith("https://accounts.google.com")
    assert "dourmouse_oauth_bind" in owner.jar["127.0.0.1"]


def test_bridge_flow_is_never_handed_over(pinned):
    """The app window fetches start itself and keeps the claim cookie on its own
    host; the system browser's callback needs no cookie. A handoff would break
    the fetch, so the claim flow stays where it started."""
    srv, port = pinned
    app = Browser(port)
    status, location, _ = app.get(_TAILSCALE, "/api/auth/google/start?claim=1")
    assert status == 302 and location.startswith("https://accounts.google.com")
    assert "dourmouse_claim" in app.jar[_TAILSCALE]


def test_unpinned_flow_unchanged(server, monkeypatch):
    srv, port = server
    monkeypatch.delenv("DOURMOUSE_OAUTH_REDIRECT_BASE", raising=False)
    owner = Browser(port)
    status, location, _ = owner.get("127.0.0.1", "/api/auth/google/start")
    assert status == 302 and location.startswith("https://accounts.google.com")
    state = _only_state(srv)
    status, location, _ = owner.get("127.0.0.1", f"/api/auth/google/callback?code=c&state={state}")
    assert status == 302 and location == "/"
