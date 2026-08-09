"""Hermetic tests for Google OAuth login (v5.15).

No network: token exchange, id_token verification, and the Gmail/Calendar
REST calls run against faked endpoints via module-level ``urlopen`` swaps.
The web-server half spins up a real ``run_server`` with a fresh in-memory
AuthStore, exactly like the cross-device API tests.
"""

import base64
import hashlib
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime

import pytest

from dourmouse import google_auth
from dourmouse import google_services
from dourmouse.artifacts import ArtifactStore
from dourmouse.dispatch import DispatchRegistry
from dourmouse.message_bus import MessageBus
from dourmouse.state_store import StateStore
from dourmouse.webui import run_server

GOOGLE_CLIENT_ID = "test-client-123.apps.googleusercontent.com"
GOOGLE_CLIENT_SECRET = "test-secret"


@pytest.fixture()
def google_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", GOOGLE_CLIENT_ID)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", GOOGLE_CLIENT_SECRET)


@pytest.fixture()
def auth_store(tmp_path):
    store = google_auth.AuthStore(tmp_path / "auth.db")
    yield store
    store.close()


# -- AuthStore ----------------------------------------------------------- #

def test_auth_store_upsert_and_tokens(auth_store):
    auth_store.upsert_user("alice@example.com", {"access_token": "tok-a", "refresh_token": "r-a"})
    assert auth_store.user_profile("alice@example.com")["email"] == "alice@example.com"
    tokens = auth_store.user_tokens("alice@example.com")
    assert tokens["access_token"] == "tok-a"


def test_auth_store_session_lifecycle(auth_store):
    auth_store.upsert_user("bob@example.com", {"access_token": "x"})
    sid = auth_store.create_session("bob@example.com")
    assert auth_store.session_email(sid) == "bob@example.com"
    auth_store.delete_session(sid)
    assert auth_store.session_email(sid) is None


def test_auth_store_rejects_bad_email(auth_store):
    with pytest.raises(ValueError):
        auth_store.upsert_user("not-an-email", {"access_token": "x"})


def test_auth_store_persists(tmp_path):
    path = tmp_path / "auth.db"
    first = google_auth.AuthStore(path)
    first.upsert_user("carol@example.com", {"access_token": "t"})
    first.create_session("carol@example.com")
    first.close()
    reopened = google_auth.AuthStore(path)
    assert reopened.user_profile("carol@example.com")["email"] == "carol@example.com"
    reopened.close()


# -- PKCE + URL ---------------------------------------------------------- #

def test_pkce_challenge_is_deterministic():
    verifier, challenge = google_auth.new_pkce()
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    assert challenge == expected
    assert len(verifier) >= 32


def test_authorization_url_includes_pkce_and_scopes(google_env):
    url = google_auth.authorization_url("http://127.0.0.1:8765/cb", "state-1", "ch-1")
    assert GOOGLE_CLIENT_ID in url
    assert "code_challenge=ch-1" in url
    assert "code_challenge_method=S256" in url
    assert "state=state-1" in url
    assert "access_type=offline" in url
    assert "gmail.readonly" in url
    assert "calendar.readonly" in url
    assert "drive.readonly" in url


def test_google_configured_requires_both(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    assert not google_auth.google_configured()


# -- token exchange + id_token verification (faked endpoints) ------------ #

class _FakeGoogleTransport:
    """Swappable urlopen: serves token exchange + tokeninfo + gmail REST."""

    def __init__(self):
        self.exchanged = []
        self.verified = []

    def __call__(self, request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        # NB: tokeninfo must be matched BEFORE /token (its URL contains it).
        if "oauth2.googleapis.com/tokeninfo" in url:
            self.verified.append(url)
            return _FakeResponse({
                "email": "alice@example.com", "email_verified": "true",
                "name": "Alice", "sub": "sub-1", "picture": "",
                "aud": GOOGLE_CLIENT_ID,
            })
        if "/token" in url and "oauth2.googleapis.com" in url:
            self.exchanged.append(url)
            return _FakeResponse({"access_token": "at-1", "refresh_token": "rt-1",
                                  "id_token": "idtoken-1", "expires_in": 3600})
        if "gmail.googleapis.com" in url:
            return _FakeResponse({"messages": [{"id": "m1"}]})
        raise AssertionError(f"unexpected URL {url}")


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeRaw:
    """Raw-bytes response for Drive export / alt=media content. Positional
    ``read(n)`` like a real file object, so the bounded-reader exercises
    chunking the way it does against the live API."""

    def __init__(self, raw: bytes):
        self._raw = raw
        self._pos = 0

    def read(self, n: int = -1):
        if n is None or n < 0:
            n = len(self._raw) - self._pos
        chunk = self._raw[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_exchange_code_and_verify(google_env, monkeypatch):
    transport = _FakeGoogleTransport()
    monkeypatch.setattr(google_auth, "urlopen", transport)
    tokens = google_auth.exchange_code("code-1", "http://127.0.0.1:8765/cb", "verifier-1")
    assert tokens["access_token"] == "at-1"
    identity = google_auth.verify_id_token("idtoken-1")
    assert identity["email"] == "alice@example.com"
    assert len(transport.exchanged) == 1
    assert len(transport.verified) == 1


def test_verify_rejects_unverified_email(google_env, monkeypatch):
    def fake_urlopen(request, timeout=None):
        return _FakeResponse({"email": "mallory@example.com", "email_verified": "false",
                              "aud": GOOGLE_CLIENT_ID})
    monkeypatch.setattr(google_auth, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="email not verified"):
        google_auth.verify_id_token("idtoken-x")


def test_verify_rejects_aud_mismatch(google_env, monkeypatch):
    """Defense-in-depth: a valid Google token for ANOTHER client must not
    verify (tokeninfo does not bind the audience to our client)."""
    def fake_urlopen(request, timeout=None):
        return _FakeResponse({"email": "mallory@example.com", "email_verified": "true",
                              "aud": "someone-elses-client.apps.googleusercontent.com"})
    monkeypatch.setattr(google_auth, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="audience mismatch"):
        google_auth.verify_id_token("idtoken-x")


def test_exchange_refuses_error(google_env, monkeypatch):
    def fake_urlopen(request, timeout=None):
        return _FakeResponse({"error": "invalid_grant",
                              "error_description": "bad code"})
    monkeypatch.setattr(google_auth, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="invalid_grant"):
        google_auth.exchange_code("bad", "http://x/cb", "v")


# -- per-user gmail REST via a fake Google API --------------------------- #

def _make_fake_gmail(messages):
    """A urlopen that serves a tiny Gmail REST surface for one message."""

    def handler(request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        if "/messages?q=" in url or "/messages?" in url and "format=metadata" not in url:
            return _FakeResponse({"messages": [{"id": mid} for mid in messages]})
        if "format=metadata" in url:
            return _FakeResponse({
                "payload": {"headers": [
                    {"name": "From", "value": "boss@example.com"},
                    {"name": "Subject", "value": "Quarterly numbers"},
                    {"name": "Date", "value": "2026-08-09T09:00:00Z"},
                ]},
            })
        if "format=full" in url:
            return _FakeResponse({
                "payload": {"mimeType": "multipart/alternative", "parts": [
                    {"mimeType": "text/plain",
                     "body": {"data": base64.urlsafe_b64encode(b"hello body").decode()}},
                ]},
            })
        if url.endswith("/messages/send"):
            return _FakeResponse({"id": "sent-1"})
        raise AssertionError(f"unexpected gmail URL {url}")

    return handler


def test_gmail_search_oauth_path(google_env, monkeypatch, auth_store):
    # Far-future _acquired_at: the token must be FRESH so no refresh (real
    # network) is attempted during the test.
    auth_store.upsert_user("alice@example.com", {
        "access_token": "at-1", "refresh_token": "rt-1",
        "_acquired_at": "2030-01-01T00:00:00+00:00", "expires_in": 3600,
    })
    monkeypatch.setattr(google_services, "urlopen", _make_fake_gmail(["m1"]))
    # serve the AuthStore from the module (the running server mounts it)
    monkeypatch.setattr(google_auth, "_auth_store", auth_store)
    google_auth.set_current_user("alice@example.com")
    try:
        result = google_services.gmail_search("from:boss", max_results=5)
    finally:
        google_auth.set_current_user(None)
    assert "Quarterly numbers" in result
    assert "boss@example.com" in result


def test_gmail_search_falls_back_without_user(google_env, monkeypatch, auth_store):
    """No session user + no app password -> honest NOT CONFIGURED."""
    monkeypatch.delenv("GOOGLE_GMAIL_USER", raising=False)
    monkeypatch.delenv("GOOGLE_GMAIL_APP_PASSWORD", raising=False)
    # a real local_secrets.py must never leak live creds into the test
    monkeypatch.setattr(google_services, "_local_secrets", lambda: {})
    monkeypatch.setattr(google_services, "urlopen", _make_fake_gmail(["m1"]))
    google_auth.set_current_user(None)
    with pytest.raises(RuntimeError, match="NOT CONFIGURED"):
        google_services.gmail_search("anything")


def test_calendar_events_oauth_path(google_env, monkeypatch, auth_store):
    def handler(request, timeout=None):
        return _FakeResponse({"items": [
            {"summary": "Standup", "start": {"dateTime": "2026-08-10T09:30:00Z"}},
        ]})
    auth_store.upsert_user("alice@example.com", {
        "access_token": "at-1", "refresh_token": "rt-1",
        "_acquired_at": "2030-01-01T00:00:00+00:00", "expires_in": 3600,
    })
    monkeypatch.setattr(google_services, "urlopen", handler)
    monkeypatch.setattr(google_auth, "_auth_store", auth_store)
    google_auth.set_current_user("alice@example.com")
    try:
        result = google_services.calendar_events(max_results=5)
    finally:
        google_auth.set_current_user(None)
    assert "Standup" in result


def test_calendar_events_not_configured_without_user(google_env, monkeypatch, auth_store):
    monkeypatch.setattr(google_auth, "_auth_store", auth_store)
    google_auth.set_current_user(None)
    assert "NOT CONFIGURED" in google_services.calendar_events()


# -- v5.18: Google Drive (read-only, per-user OAuth) ---------------------- #


def _fresh_user(monkeypatch, auth_store, email="alice@example.com"):
    """Seed a signed-in user with a fresh (never-expiring) token and bind the
    store + thread-local — the shared preamble for OAuth-path tests."""
    auth_store.upsert_user(email, {
        "access_token": "at-1", "refresh_token": "rt-1",
        "_acquired_at": "2030-01-01T00:00:00+00:00", "expires_in": 3600,
    })
    monkeypatch.setattr(google_auth, "_auth_store", auth_store)
    google_auth.set_current_user(email)


def test_drive_search_oauth_path(google_env, monkeypatch, auth_store):
    def handler(request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        if "drive/v3/files" in url and "/export" not in url and "alt=media" not in url:
            return _FakeResponse({"files": [
                {"id": "file-1", "name": "Q3 report.docx",
                 "mimeType": "application/vnd.google-apps.document",
                 "modifiedTime": "2026-08-09T09:00:00.000Z", "size": "4096"},
            ]})
        raise AssertionError(f"unexpected URL {url}")

    _fresh_user(monkeypatch, auth_store)
    monkeypatch.setattr(google_services, "urlopen", handler)
    try:
        result = google_services.drive_search("q3 report")
    finally:
        google_auth.set_current_user(None)
    assert "Q3 report.docx" in result
    assert "file-1" in result
    assert "DRIVE FILES" in result


def test_drive_read_oauth_native_export(google_env, monkeypatch, auth_store):
    """Google-native files (Docs/Sheets) read through the export endpoint."""
    def handler(request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        if "files/file-1?fields=" in url:
            return _FakeResponse({
                "id": "file-1", "name": "Meeting notes",
                "mimeType": "application/vnd.google-apps.document",
                "modifiedTime": "2026-08-09T09:00:00.000Z", "size": "2048",
            })
        if "/export?mimeType=text/plain" in url:
            return _FakeRaw(b"action items:\n- ship drive tool")
        raise AssertionError(f"unexpected URL {url}")

    _fresh_user(monkeypatch, auth_store)
    monkeypatch.setattr(google_services, "urlopen", handler)
    try:
        result = google_services.drive_read("file-1")
    finally:
        google_auth.set_current_user(None)
    assert "Meeting notes" in result
    assert "ship drive tool" in result
    assert "TRUNCATED" not in result


def test_drive_read_oauth_binary_alt_media(google_env, monkeypatch, auth_store):
    """Non-native files read via alt=media with a size cap."""
    def handler(request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        if "files/file-2?fields=" in url:
            return _FakeResponse({
                "id": "file-2", "name": "notes.txt", "mimeType": "text/plain",
                "modifiedTime": "2026-08-09T09:00:00.000Z", "size": "12",
            })
        if "alt=media" in url:
            return _FakeRaw(b"hello drive body")
        raise AssertionError(f"unexpected URL {url}")

    _fresh_user(monkeypatch, auth_store)
    monkeypatch.setattr(google_services, "urlopen", handler)
    try:
        result = google_services.drive_read("file-2")
    finally:
        google_auth.set_current_user(None)
    assert "notes.txt" in result
    assert "hello drive body" in result


def test_drive_read_refuses_oversized_binary(google_env, monkeypatch, auth_store):
    def handler(request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        return _FakeResponse({
            "id": "big", "name": "movie.mov", "mimeType": "video/quicktime",
            "modifiedTime": "2026-08-09T09:00:00.000Z", "size": str(9_000_000_000),
        })

    _fresh_user(monkeypatch, auth_store)
    monkeypatch.setattr(google_services, "urlopen", handler)
    try:
        result = google_services.drive_read("big")
    finally:
        google_auth.set_current_user(None)
    assert "too large to read as text" in result
    assert "9,000,000,000 B" in result


def test_drive_reauth_guard_signed_in_user(google_env, monkeypatch, auth_store):
    """A signed-in user with no token gets the re-sign-in message — Drive
    has no legacy shared path to leak into."""
    monkeypatch.setattr(google_auth, "_auth_store", auth_store)
    google_auth.set_current_user("signed-in@example.com")
    try:
        assert "sign in again at /login" in google_services.drive_search("anything")
        assert "sign in again at /login" in google_services.drive_read("file-1")
    finally:
        google_auth.set_current_user(None)


def test_drive_not_configured_without_user(google_env, monkeypatch, auth_store):
    monkeypatch.setattr(google_auth, "_auth_store", auth_store)
    google_auth.set_current_user(None)
    assert "NOT CONFIGURED" in google_services.drive_search("anything")
    assert "NOT CONFIGURED" in google_services.drive_read("file-1")


def test_drive_search_escapes_quotes_in_query(google_env, monkeypatch, auth_store):
    """Drive q-syntax injection is neutralized: a query containing a single
    quote is escaped by doubling (O'Brien -> O''Brien), never spliced raw."""
    seen = []

    def handler(request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        seen.append(url)
        return _FakeResponse({"files": []})

    _fresh_user(monkeypatch, auth_store)
    monkeypatch.setattr(google_services, "urlopen", handler)
    try:
        google_services.drive_search("O'Brien")
    finally:
        google_auth.set_current_user(None)
    assert seen, "files.list must be called"
    assert "O%27%27Brien" in seen[0]  # O''Brien URL-encoded — doubled quote
    assert "O%27Brien" not in seen[0]  # never a single spliced quote


def test_drive_read_works_without_size_metadata(google_env, monkeypatch, auth_store):
    """The read cap does NOT depend on the metadata ``size`` field: a file
    whose metadata omits size (shortcuts, API omissions) still reads within
    the bounded fetch (reviewer-caught bypass)."""
    def handler(request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        if "files/file-3?fields=" in url:
            return _FakeResponse({
                "id": "file-3", "name": "mystery.bin", "mimeType": "application/octet-stream",
                "modifiedTime": "2026-08-09T09:00:00.000Z",  # no "size" key
            })
        if "alt=media" in url:
            return _FakeRaw(b"small content despite no size metadata")
        raise AssertionError(f"unexpected URL {url}")

    _fresh_user(monkeypatch, auth_store)
    monkeypatch.setattr(google_services, "urlopen", handler)
    try:
        result = google_services.drive_read("file-3")
    finally:
        google_auth.set_current_user(None)
    assert "small content despite no size metadata" in result


def test_drive_read_cap_enforced_on_the_fetch(google_env, monkeypatch, auth_store):
    """The bounded read itself refuses oversized responses even when the
    metadata lies or omits size (cap monkeypatched small for the test)."""
    def handler(request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        if "files/file-4?fields=" in url:
            return _FakeResponse({
                "id": "file-4", "name": "huge.bin", "mimeType": "application/octet-stream",
                "modifiedTime": "2026-08-09T09:00:00.000Z",
            })
        if "alt=media" in url:
            return _FakeRaw(b"x" * 5000)
        raise AssertionError(f"unexpected URL {url}")

    _fresh_user(monkeypatch, auth_store)
    monkeypatch.setattr(google_services, "urlopen", handler)
    monkeypatch.setattr(google_services, "_MAX_DRIVE_BYTES", 100)
    try:
        with pytest.raises(RuntimeError, match="exceeds the 100 B cap"):
            google_services.drive_read("file-4")
    finally:
        google_auth.set_current_user(None)


# -- full auth flow over HTTP -------------------------------------------- #

@pytest.fixture()
def server(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", GOOGLE_CLIENT_ID)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", GOOGLE_CLIENT_SECRET)
    registry = DispatchRegistry()
    srv = run_server(
        registry,
        host="127.0.0.1",
        port=0,
        client=None,
        config=None,
        live_polling=False,
        memory=None,
        bus=MessageBus(),
        reporting=False,
        neuro=False,
        artifacts=ArtifactStore(),
        freebuff_events=False,
        state=StateStore(),
        auth=google_auth.AuthStore(),
    )
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}", srv
    srv.shutdown()
    srv.server_close()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow 3xx so tests can assert on the redirect itself."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _opener():
    return urllib.request.build_opener(_NoRedirect())


def _get(base, path, cookie=None):
    request = urllib.request.Request(base + path)
    if cookie:
        request.add_header("Cookie", cookie)
    with _opener().open(request) as resp:
        return json.loads(resp.read().decode())


def test_auth_status_not_configured_when_no_client(server, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    base, _ = server
    status = _get(base, "/api/auth/status")
    assert status["configured"] is False
    assert status["me"] is None


def test_google_start_redirects_to_google(server):
    base, _ = server
    request = urllib.request.Request(base + "/api/auth/google/start")
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _opener().open(request)
    assert exc_info.value.code == 302
    location = exc_info.value.headers.get("Location", "")
    assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth")
    assert GOOGLE_CLIENT_ID in location
    assert "code_challenge=" in location


def test_google_callback_creates_session(server, monkeypatch):
    base, srv = server
    transport = _FakeGoogleTransport()
    monkeypatch.setattr(google_auth, "urlopen", transport)

    # plant a pending OAuth state exactly as /api/auth/google/start would
    import secrets
    state = secrets.token_urlsafe(24)
    verifier, challenge = google_auth.new_pkce()
    with srv.oauth_lock:
        srv.oauth_pending[state] = {
            "verifier": verifier,
            "redirect_uri": "http://127.0.0.1:0/api/auth/google/callback",
            "redirect_to": "/",
            "created": datetime.now().isoformat(),
        }
    request = urllib.request.Request(
        base + f"/api/auth/google/callback?code=code-1&state={state}"
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _opener().open(request)
    assert exc_info.value.code == 302
    cookie = exc_info.value.headers.get("Set-Cookie", "")
    assert "dourmouse_user_session=" in cookie
    sid = cookie.split("dourmouse_user_session=")[1].split(";")[0]
    # the session authorizes API access via the cookie
    assert _get(base, "/api/auth/me", f"dourmouse_user_session={sid}")["me"]["email"] \
        == "alice@example.com"


def test_google_callback_rejects_unknown_state(server):
    base, _ = server
    request = urllib.request.Request(
        base + "/api/auth/google/callback?code=x&state=bogus-state"
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request)
    assert exc.value.code == 400


def test_logout_clears_session(server, monkeypatch):
    base, srv = server
    transport = _FakeGoogleTransport()
    monkeypatch.setattr(google_auth, "urlopen", transport)
    srv.auth.upsert_user("dave@example.com", {"access_token": "at-1"})
    sid = srv.auth.create_session("dave@example.com")
    # me before logout
    assert _get(base, "/api/auth/me", f"dourmouse_user_session={sid}")["me"]["email"] \
        == "dave@example.com"
    request = urllib.request.Request(
        base + "/api/auth/logout",
        data=b"{}",
        headers={"Content-Type": "application/json", "Cookie": f"dourmouse_user_session={sid}"},
        method="POST",
    )
    with urllib.request.urlopen(request) as resp:
        assert json.loads(resp.read())["ok"] is True
    assert srv.auth.session_email(sid) is None


def test_logout_revokes_refresh_token(server, monkeypatch):
    """Logout best-effort revokes the user's Google refresh token so a stolen
    token cannot outlive the session (reviewer-caught dead code)."""
    base, srv = server
    revoked = []

    def fake_urlopen(request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        if "oauth2.googleapis.com/revoke" in url:
            revoked.append(url)
            return _FakeResponse({})
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(google_auth, "urlopen", fake_urlopen)
    srv.auth.upsert_user("dave@example.com", {
        "access_token": "at-1", "refresh_token": "rt-revoke-me"})
    sid = srv.auth.create_session("dave@example.com")
    request = urllib.request.Request(
        base + "/api/auth/logout",
        data=b"{}",
        headers={"Content-Type": "application/json", "Cookie": f"dourmouse_user_session={sid}"},
        method="POST",
    )
    with urllib.request.urlopen(request) as resp:
        assert json.loads(resp.read())["ok"] is True
    assert revoked, "logout must best-effort revoke the refresh token"
    assert srv.auth.session_email(sid) is None


def test_google_callback_denied_redirects_to_login(server):
    """Google refused consent (?error=access_denied) -> friendly 302 to
    /login, and the state is consumed (single-use) even on denial."""
    base, srv = server
    import secrets

    state = secrets.token_urlsafe(24)
    with srv.oauth_lock:
        srv.oauth_pending[state] = {
            "verifier": "v",
            "redirect_uri": "http://127.0.0.1:0/cb",
            "redirect_to": "/",
            "created": datetime.now().isoformat(),
        }
    request = urllib.request.Request(
        base + f"/api/auth/google/callback?error=access_denied&state={state}"
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _opener().open(request)
    assert exc_info.value.code == 302
    assert exc_info.value.headers.get("Location") == "/login?reason=denied"
    with srv.oauth_lock:
        assert state not in srv.oauth_pending


def test_google_start_honors_host_port(server):
    """The redirect_uri sent to Google carries the Host header's port, not
    the internal server_port (reviewer-caught: proxy deployments otherwise
    hit redirect_uri_mismatch)."""
    base, _ = server
    request = urllib.request.Request(base + "/api/auth/google/start")
    request.add_header("Host", "127.0.0.1:9999")
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _opener().open(request)
    assert exc_info.value.code == 302
    location = exc_info.value.headers.get("Location", "")
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A9999%2Fapi%2Fauth%2Fgoogle%2Fcallback" in location


def test_prune_abandoned_oauth_pending(server, monkeypatch):
    """Abandoned login flows (older than the TTL) are pruned on the next
    start — the dict can never grow unboundedly (reviewer-caught leak)."""
    from datetime import timedelta

    base, srv = server
    now = datetime.now()
    with srv.oauth_lock:
        srv.oauth_pending["stale-1"] = {"created": (now - timedelta(minutes=30)).isoformat()}
        srv.oauth_pending["stale-2"] = {"created": (now - timedelta(hours=2)).isoformat()}
        srv.oauth_pending["fresh"] = {"created": now.isoformat()}
    request = urllib.request.Request(base + "/api/auth/google/start")
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _opener().open(request)
    assert exc_info.value.code == 302
    with srv.oauth_lock:
        remaining = set(srv.oauth_pending)
    assert "stale-1" not in remaining
    assert "stale-2" not in remaining
    assert "fresh" in remaining


def test_signed_in_user_never_falls_back_to_shared_inbox(google_env, monkeypatch, auth_store):
    """SECURITY: a signed-in user whose token is missing/expired gets an
    honest re-sign-in message — NEVER the server owner's shared App-Password
    inbox, even when the owner's credentials are configured (reviewer-caught
    cross-account leak)."""
    monkeypatch.setenv("GOOGLE_GMAIL_USER", "owner@gmail.com")
    monkeypatch.setenv("GOOGLE_GMAIL_APP_PASSWORD", "owner-app-password")
    monkeypatch.setattr(
        google_services, "_local_secrets",
        lambda: {"user": "owner@gmail.com", "password": "owner-app-password"})
    # the auth store has NO tokens for this user -> access_token_for -> None
    monkeypatch.setattr(google_auth, "_auth_store", auth_store)
    google_auth.set_current_user("signed-in@example.com")
    try:
        result = google_services.gmail_search("anything")
    finally:
        google_auth.set_current_user(None)
    assert "sign in again at /login" in result
    assert "owner@gmail.com" not in result
    assert "NOT CONFIGURED" not in result  # never the owner-account error path


def test_signed_in_user_send_and_calendar_also_guarded(google_env, monkeypatch, auth_store):
    """Same guard on the send and calendar surfaces: no token -> reauth
    message, never the shared SMTP/owner path."""
    monkeypatch.setenv("GOOGLE_GMAIL_USER", "owner@gmail.com")
    monkeypatch.setenv("GOOGLE_GMAIL_APP_PASSWORD", "owner-app-password")
    monkeypatch.setattr(
        google_services, "_local_secrets",
        lambda: {"user": "owner@gmail.com", "password": "owner-app-password"})
    monkeypatch.setattr(google_auth, "_auth_store", auth_store)
    google_auth.set_current_user("signed-in@example.com")
    try:
        assert "sign in again at /login" in google_services.gmail_send("x@y.com", "s", "b")
        assert "sign in again at /login" in google_services.calendar_events()
    finally:
        google_auth.set_current_user(None)


def test_read_inbox_oauth_path(google_env, monkeypatch, auth_store):
    """The mail agent's read_inbox reads the SIGNED-IN user's own mailbox
    via Gmail REST (same per-user guarantee as gmail_search)."""
    def handler(request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        if "/messages?maxResults=" in url and "format=full" not in url:
            return _FakeResponse({"messages": [{"id": "m1"}, {"id": "m2"}]})
        if "format=full" in url:
            return _FakeResponse({
                "snippet": "hello snippet",
                "payload": {"headers": [
                    {"name": "From", "value": "boss@example.com"},
                    {"name": "Subject", "value": "Re: numbers"},
                    {"name": "Date", "value": "2026-08-09T09:00:00Z"},
                ]},
            })
        raise AssertionError(f"unexpected URL {url}")

    from dourmouse import live_feeds

    auth_store.upsert_user("alice@example.com", {
        "access_token": "at-1", "refresh_token": "rt-1",
        "_acquired_at": "2030-01-01T00:00:00+00:00", "expires_in": 3600,
    })
    monkeypatch.setattr(google_services, "urlopen", handler)
    monkeypatch.setattr(google_auth, "_auth_store", auth_store)
    google_auth.set_current_user("alice@example.com")
    try:
        messages = live_feeds.read_inbox(max_items=5)
    finally:
        google_auth.set_current_user(None)
    assert len(messages) == 2
    assert messages[0]["subject"] == "Re: numbers"
    assert messages[0]["snippet"] == "hello snippet"


def test_read_inbox_signed_in_user_never_owner_fallback(google_env, monkeypatch, auth_store):
    """SECURITY: the mail agent's read_inbox must NOT fall back to the
    owner's shared App-Password inbox for a signed-in user (reviewer-caught
    path outside google_services: live_feeds.read_inbox + the LiveRuntime
    mail poll)."""
    from dourmouse import live_feeds

    monkeypatch.setenv("GOOGLE_GMAIL_USER", "owner@gmail.com")
    monkeypatch.setenv("GOOGLE_GMAIL_APP_PASSWORD", "owner-app-password")
    monkeypatch.setattr(
        google_services, "_local_secrets",
        lambda: {"user": "owner@gmail.com", "password": "owner-app-password"})
    monkeypatch.setattr(google_auth, "_auth_store", auth_store)
    google_auth.set_current_user("signed-in@example.com")
    try:
        with pytest.raises(RuntimeError, match="sign in again at /login"):
            live_feeds.read_inbox(max_items=5)
    finally:
        google_auth.set_current_user(None)


def test_chat_request_with_user_session_streams(server, monkeypatch):
    """Regression for the v5.16 ``_handle_chat`` split: a signed-in user's
    /api/chat request streams SSE (200, never 401) through the split
    handler and the honest no-LLM failure surfaces as an SSE error — the
    try/finally split preserves the request pipeline, gate wiring and
    thread binding."""
    from dourmouse import dispatch, google_auth

    base, srv = server
    srv.auth.upsert_user("erin@example.com", {"access_token": "at-1"})
    sid = srv.auth.create_session("erin@example.com")

    def _no_llm(*args, **kwargs):
        raise ValueError("no LLM configured in tests")

    monkeypatch.setattr(dispatch, "load_llm_config", _no_llm)
    body = json.dumps({"prompt": "hello"}).encode()
    request = urllib.request.Request(
        base + "/api/chat",
        data=body,
        headers={"Content-Type": "application/json",
                 "Cookie": f"dourmouse_user_session={sid}"},
    )
    with urllib.request.urlopen(request, timeout=30) as resp:
        assert resp.status == 200
        assert resp.headers.get("Content-Type", "").startswith("text/event-stream")
        raw = resp.read().decode("utf-8", errors="replace")
    assert "no LLM configured in tests" in raw  # the REAL failure surfaced
    assert google_auth.current_user() is None  # caller-side sanity guard


def test_read_inbox_no_user_uses_imap_fallback(google_env, monkeypatch, auth_store):
    """With NO user signed in, the IMAP owner path still applies (the
    owner's own server/mail poll) — the guard must not break the legacy
    flow. A stubbed IMAP connection proves the code REACHED the IMAP path
    (its NOT CONFIGURED error, not the reauth guard's)."""
    import imaplib

    from dourmouse import live_feeds

    monkeypatch.setenv("GOOGLE_GMAIL_USER", "owner@gmail.com")
    monkeypatch.setenv("GOOGLE_GMAIL_APP_PASSWORD", "owner-app-password")
    monkeypatch.setattr(
        google_services, "_local_secrets",
        lambda: {"user": "owner@gmail.com", "password": "owner-app-password"})
    monkeypatch.setattr(google_auth, "_auth_store", auth_store)
    google_auth.set_current_user(None)

    class _FakeIMAP:
        def __init__(self, *args, **kwargs):
            pass

        def login(self, *args, **kwargs):
            raise RuntimeError("NOT CONFIGURED: no real IMAP server in tests")

        def logout(self, *args, **kwargs):
            pass

    monkeypatch.setattr(imaplib, "IMAP4_SSL", _FakeIMAP)
    with pytest.raises(RuntimeError, match="NOT CONFIGURED"):
        live_feeds.read_inbox(max_items=5)
