"""FW A-20 / A-21: the system-browser sign-in claim bridge.

A-20: the claim code used to be chosen by the caller of /api/auth/google/start
and redeemed (pre-auth) by anyone who knew it. Now the server mints a secret,
hands it only to the requesting browser as an HttpOnly cookie, parks the
completed session under it, and /api/auth/claim redeems it once for the browser
that holds the cookie. A-21: the claim response must be one well-formed HTTP
response, not two status lines.
"""
from __future__ import annotations

import http.client
import json
import socket
import threading

import pytest

import dourmouse.webui as webui_module
from dourmouse.dispatch import DispatchRegistry
from dourmouse.webui import run_server

_IDENTITY = {"email": "bridge@example.com", "name": "Bridge User", "picture": "", "sub": "s1"}


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

    monkeypatch.setattr(google_auth, "google_configured", lambda: True)
    monkeypatch.setattr(
        google_auth, "authorization_url",
        lambda *a, **k: "https://accounts.google.com/o/oauth2/v2/auth?fake=1")
    monkeypatch.setattr(
        google_auth, "exchange_code",
        lambda *a, **k: {"id_token": "tok", "access_token": "a", "refresh_token": "r"})
    monkeypatch.setattr(google_auth, "verify_id_token", lambda *a, **k: dict(_IDENTITY))


def _get(port, path, cookie=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers={"Cookie": cookie} if cookie else {})
    resp = conn.getresponse()
    body = resp.read()
    out = (resp.status, resp.getheader("Location"), resp.getheader("Set-Cookie"), body)
    conn.close()
    return out


def _cookie_value(set_cookie, name):
    assert set_cookie and f"{name}=" in set_cookie, set_cookie
    return set_cookie.split(f"{name}=")[1].split(";")[0]


def _start(port, query="?claim=attacker-chosen-value"):
    status, location, set_cookie, _ = _get(port, "/api/auth/google/start" + query)
    assert status == 302 and location.startswith("https://accounts.google.com")
    return set_cookie


def _state_of(srv):
    with srv.oauth_lock:
        assert len(srv.oauth_pending) == 1
        return next(iter(srv.oauth_pending))


def test_caller_chosen_claim_is_never_used_as_the_code(server):
    srv, port = server
    _start(port)
    with srv.oauth_lock:
        pending = next(iter(srv.oauth_pending.values()))
    assert pending["claim"] != "attacker-chosen-value"
    assert len(pending["claim"]) >= 32


def test_start_hands_the_secret_only_to_the_requester_as_httponly_cookie(server):
    srv, port = server
    set_cookie = _start(port)
    assert "HttpOnly" in set_cookie and "SameSite=Strict" in set_cookie
    assert "Max-Age=" in set_cookie
    secret = _cookie_value(set_cookie, "dourmouse_claim")
    with srv.oauth_lock:
        pending = next(iter(srv.oauth_pending.values()))
    assert pending["claim"] == secret


def test_flow_redeems_once_for_the_browser_that_started_it(server):
    srv, port = server
    cookie = "dourmouse_claim=" + _cookie_value(_start(port), "dourmouse_claim")
    state = _state_of(srv)
    status, location, set_cookie, _ = _get(port, f"/api/auth/google/callback?code=c&state={state}")
    assert status == 302 and location == "/login?claimed=1"
    assert set_cookie is None  # the consenting browser gets no session

    status, _, set_cookie, raw = _get(port, "/api/auth/claim", cookie=cookie)
    assert status == 200 and json.loads(raw)["me"]["email"] == _IDENTITY["email"]
    sid = _cookie_value(set_cookie, "dourmouse_user_session")
    assert "HttpOnly" in set_cookie

    status, _, _, raw = _get(port, "/api/auth/me", cookie=f"dourmouse_user_session={sid}")
    assert json.loads(raw)["me"]["email"] == _IDENTITY["email"]

    status, _, _, _ = _get(port, "/api/auth/claim", cookie=cookie)
    assert status == 404  # single use


def test_a_caller_cannot_redeem_with_a_code_it_chose(server):
    """The attack: send the owner to start?claim=X, then redeem X with no
    cookie after the owner consented."""
    srv, port = server
    _start(port, "?claim=attacker-value")
    state = _state_of(srv)
    _get(port, f"/api/auth/google/callback?code=c&state={state}")
    for attempt in (
        "/api/auth/claim?code=attacker-value",
        "/api/auth/claim?claim=attacker-value",
        "/api/auth/claim",
    ):
        status, _, set_cookie, _ = _get(port, attempt)
        assert status in (400, 404), attempt
        assert not set_cookie or "dourmouse_user_session=" not in set_cookie
    status, _, _, _ = _get(port, "/api/auth/claim?code=attacker-value", cookie="dourmouse_claim=attacker-value")
    assert status == 404
    assert len(srv.claim_pending) == 1  # still parked, unredeemed


def test_another_browsers_secret_does_not_redeem(server):
    srv, port = server
    _start(port)
    state = _state_of(srv)
    _get(port, f"/api/auth/google/callback?code=c&state={state}")
    other = "dourmouse_claim=" + "x" * 43
    assert _get(port, "/api/auth/claim", cookie=other)[0] == 404


def test_parked_claim_expires(server, monkeypatch):
    srv, port = server
    cookie = "dourmouse_claim=" + _cookie_value(_start(port), "dourmouse_claim")
    state = _state_of(srv)
    _get(port, f"/api/auth/google/callback?code=c&state={state}")
    with srv.claim_lock:
        for entry in srv.claim_pending.values():
            entry["created"] = "2020-01-01T00:00:00"
    assert _get(port, "/api/auth/claim", cookie=cookie)[0] == 404


def test_plain_start_without_claim_sets_no_claim_cookie(server):
    srv, port = server
    status, location, set_cookie, _ = _get(port, "/api/auth/google/start")
    assert status == 302
    assert not set_cookie or "dourmouse_claim" not in set_cookie
    with srv.oauth_lock:
        assert next(iter(srv.oauth_pending.values()))["claim"] is None


def test_claim_response_is_one_well_formed_http_response(server):
    """A-21: exactly one status line in the raw bytes."""
    srv, port = server
    cookie = "dourmouse_claim=" + _cookie_value(_start(port), "dourmouse_claim")
    state = _state_of(srv)
    _get(port, f"/api/auth/google/callback?code=c&state={state}")
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(f"GET /api/auth/claim HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\nCookie: {cookie}\r\n\r\n".encode())
    raw = b""
    while chunk := s.recv(4096):
        raw += chunk
    s.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    assert head.count(b"HTTP/1.") == 1, raw
    assert b"Content-Length" in head and b"Content-Type: application/json" in head
    assert b"\"ok\": true" in body, raw


def test_module_is_the_one_under_test():
    assert hasattr(webui_module, "_Handler")


# -- H-FW-1: the plain (in-app) callback is bound to the browser that started it

def test_plain_callback_requires_the_browser_that_started_the_flow(server):
    """Without this a page could send the owner to a sign-in the attacker
    started, and the owner's browser would adopt the attacker's session."""
    srv, port = server
    status, location, set_cookie, _ = _get(port, "/api/auth/google/start")
    assert status == 302
    assert _cookie_value(set_cookie, "dourmouse_oauth_bind")
    assert "HttpOnly" in set_cookie and "SameSite=Lax" in set_cookie  # Lax: Google's redirect is cross-site
    state = _state_of(srv)

    # A different browser (no cookie) cannot complete it, and the state is spent.
    status, _, set_cookie, raw = _get(port, f"/api/auth/google/callback?code=c&state={state}")
    assert status == 400 and not set_cookie
    assert srv.oauth_pending == {}


def test_plain_callback_completes_for_the_starting_browser(server):
    srv, port = server
    _, _, set_cookie, _ = _get(port, "/api/auth/google/start")
    bind = _cookie_value(set_cookie, "dourmouse_oauth_bind")
    state = _state_of(srv)
    status, location, set_cookie, _ = _get(
        port, f"/api/auth/google/callback?code=c&state={state}", cookie=f"dourmouse_oauth_bind={bind}")
    assert status == 302 and location == "/"
    assert "dourmouse_user_session=" in set_cookie


def test_claim_flow_callback_needs_no_bind_cookie(server):
    """The bridge's consent runs in another browser by design."""
    srv, port = server
    _start(port)
    state = _state_of(srv)
    status, location, _, _ = _get(port, f"/api/auth/google/callback?code=c&state={state}")
    assert status == 302 and location == "/login?claimed=1"


# -- A-20: redeeming also needs the owner's app window when the owner gate is on

def test_claim_redeem_needs_the_owner_proof_when_the_owner_gate_is_enforced(server):
    """Closes the variant where the attacker starts the flow in THEIR browser,
    gets the owner to consent, and redeems with their own claim cookie."""
    srv, port = server
    srv.owner_gate_enforced = True
    srv.owner_secret = "o" * 40
    cookie = "dourmouse_claim=" + _cookie_value(_start(port), "dourmouse_claim")
    state = _state_of(srv)
    _get(port, f"/api/auth/google/callback?code=c&state={state}")

    status, _, set_cookie, _ = _get(port, "/api/auth/claim", cookie=cookie)
    assert status == 403 and not set_cookie
    assert len(srv.claim_pending) == 1  # not consumed by the refused attempt

    status, _, set_cookie, _ = _get(port, "/api/auth/claim", cookie=cookie + "; dourmouse_owner=" + "o" * 40)
    assert status == 200 and "dourmouse_user_session=" in set_cookie


def test_start_can_answer_json_for_the_sign_in_pages(server):
    """Finding #181 (U1-9): ?claim=1&format=json answers 200 JSON {ok, url} and still sets the claim cookie."""
    _srv, port = server
    status, location, cookie, body = _get(port, "/api/auth/google/start?claim=1&format=json")
    assert status == 200 and location is None
    data = json.loads(body)
    assert data["ok"] is True and data["url"].startswith("https://accounts.google.com/")
    assert "dourmouse_claim=" in (cookie or "")
