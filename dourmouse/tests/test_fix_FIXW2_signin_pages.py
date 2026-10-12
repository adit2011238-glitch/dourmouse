"""FIX-W2 A-20 UI: /login and /setup no longer generate the claim value. They ask for the bridge with
?claim=1 and poll /api/auth/claim with no code; the server's own cookie is the secret. The sequence the pages
perform is replayed over real HTTP, with a cookie-keeping client, to show Google sign-in still completes."""
from __future__ import annotations

import http.client
import json
import re
import threading
from pathlib import Path

import pytest

from dourmouse.dispatch import DispatchRegistry
from dourmouse.webui import run_server

UI = Path(__file__).resolve().parents[2] / "ui"
_IDENTITY = {"email": "page@example.com", "name": "Page User", "picture": "", "sub": "s1"}


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
    monkeypatch.setattr(google_auth, "authorization_url", lambda *a, **k: "https://accounts.google.com/o/oauth2/v2/auth?x=1")
    monkeypatch.setattr(google_auth, "exchange_code", lambda *a, **k: {"id_token": "t", "access_token": "a", "refresh_token": "r"})
    monkeypatch.setattr(google_auth, "verify_id_token", lambda *a, **k: dict(_IDENTITY))


@pytest.mark.parametrize("name", ["login.html", "setup.html"])
def test_the_pages_never_generate_or_send_a_claim_value(name):
    src = (UI / name).read_text(encoding="utf-8")
    assert "randomUUID" not in src and "Math.random" not in src
    assert "claim?code=" not in src and "claim=' + claim" not in src and 'claim=" + ' not in src
    assert "google/start?claim=1" in src
    assert re.search(r"fetch\(['\"]/api/auth/claim['\"]\)", src)


def test_login_page_still_opens_the_system_browser_and_polls():
    src = (UI / "login.html").read_text(encoding="utf-8")
    assert "open_external" in src and "startClaimPoll()" in src and "claiming=1" in src
    assert "showManualLink(googleUrl)" in src


def _req(port, path, cookie=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers={"Cookie": cookie} if cookie else {})
    r = conn.getresponse()
    body = r.read()
    out = (r.status, r.getheader("Location"), r.getheader("Set-Cookie"), body)
    conn.close()
    return out


def test_the_exact_page_sequence_completes_a_sign_in(server):
    """App window: GET start?claim=1 (keeps the cookie) -> consent happens in ANOTHER browser (callback with
    no cookies) -> App window polls /api/auth/claim with only its cookie and is signed in."""
    srv, port = server
    status, location, set_cookie, _ = _req(port, "/api/auth/google/start?claim=1")
    assert status == 302 and location.startswith("https://accounts.google.com")
    cookie = set_cookie.split(";")[0]
    assert cookie.startswith("dourmouse_claim=") and len(cookie) > 40
    with srv.oauth_lock:
        state = next(iter(srv.oauth_pending))

    assert _req(port, "/api/auth/claim", cookie=cookie)[0] == 404  # nothing parked yet: keep polling
    status, location, _, _ = _req(port, f"/api/auth/google/callback?code=c&state={state}")  # the system browser
    assert status == 302 and location == "/login?claimed=1"

    status, _, set_cookie, body = _req(port, "/api/auth/claim", cookie=cookie)
    assert status == 200 and json.loads(body)["me"]["email"] == _IDENTITY["email"]
    assert "dourmouse_user_session=" in set_cookie
    assert _req(port, "/api/auth/claim", cookie=cookie)[0] == 404  # single use


def test_polling_without_the_cookie_gets_nothing(server):
    srv, port = server
    _req(port, "/api/auth/google/start?claim=1")
    with srv.oauth_lock:
        state = next(iter(srv.oauth_pending))
    _req(port, f"/api/auth/google/callback?code=c&state={state}")
    status, _, set_cookie, _ = _req(port, "/api/auth/claim")
    assert status == 400 and not set_cookie
