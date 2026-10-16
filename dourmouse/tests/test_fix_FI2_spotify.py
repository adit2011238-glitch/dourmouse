"""FI2: Spotify findings (P3-63, P3-64, P3-65, P3-66). No Spotify endpoint is contacted:
urlopen and the token POST are replaced, and the callback server is a real loopback one."""

from __future__ import annotations

import io
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from dourmouse import spotify_services as ss


@pytest.fixture(autouse=True)
def _workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(ss, "_cache", {}, raising=False)
    return tmp_path


def _http_error(code: int, body: bytes = b"{}") -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.spotify.com/v1/x", code, "err", {}, io.BytesIO(body))


def _linked(expires_at: float = 0.0) -> None:
    ss._save_tokens({"access_token": "old", "refresh_token": "rt", "expires_at": expires_at})


# ---------------------------------------------------------------- P3-64

def test_persistent_401_refreshes_once_then_reports(monkeypatch):
    _linked(expires_at=time.time() + 3600)
    posts, opens = [], []
    monkeypatch.setattr(ss, "_post_form", lambda url, body: posts.append(1) or
                        {"access_token": "new", "expires_in": 3600})

    def always_401(req, timeout=0):
        opens.append(1)
        raise _http_error(401)

    monkeypatch.setattr(ss.urllib.request, "urlopen", always_401)
    with pytest.raises(RuntimeError, match="SPOTIFY AUTH FAILED"):
        ss._api("GET", "/me")
    assert len(posts) == 1 and len(opens) == 2  # one refresh, one retry, then stop


def test_401_then_success_after_refresh_still_works(monkeypatch):
    _linked(expires_at=time.time() + 3600)
    monkeypatch.setattr(ss, "_post_form", lambda url, body: {"access_token": "new", "expires_in": 3600})
    calls = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": 1}'

    def flaky(req, timeout=0):
        calls.append(1)
        if len(calls) == 1:
            raise _http_error(401)
        return Resp()

    monkeypatch.setattr(ss.urllib.request, "urlopen", flaky)
    assert ss._api("GET", "/me") == {"ok": 1}


@pytest.mark.parametrize("exc", [
    urllib.error.URLError("dns down"),
    urllib.error.HTTPError("https://accounts.spotify.com/api/token", 400, "bad", {},
                           io.BytesIO(b'{"error":"invalid_grant"}')),
    TimeoutError("slow"),
])
def test_post_form_failures_are_runtime_errors(monkeypatch, exc):
    def boom(req, timeout=0):
        raise exc

    monkeypatch.setattr(ss.urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError, match="SPOTIFY"):
        ss._post_form(ss._TOKEN_URL, b"x=1")


def test_revoked_refresh_token_surfaces_as_runtime_error_from_access_token(monkeypatch):
    _linked(expires_at=0.0)

    def boom(req, timeout=0):
        raise _http_error(400, b'{"error":"invalid_grant"}')

    monkeypatch.setattr(ss.urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError, match="invalid_grant"):
        ss._access_token()


# ---------------------------------------------------------------- P3-66

def test_playback_state_does_not_hide_not_linked():
    out = None
    with pytest.raises(RuntimeError, match="NOT LINKED"):
        out = ss.playback_state()
    assert out is None


def test_playback_state_does_not_hide_an_auth_failure(monkeypatch):
    _linked(expires_at=time.time() + 3600)
    monkeypatch.setattr(ss, "_post_form", lambda url, body: {"access_token": "n", "expires_in": 3600})
    monkeypatch.setattr(ss.urllib.request, "urlopen",
                        lambda req, timeout=0: (_ for _ in ()).throw(_http_error(401)))
    with pytest.raises(RuntimeError, match="SPOTIFY AUTH FAILED"):
        ss.playback_state()


def test_playback_state_still_reports_a_device_problem(monkeypatch):
    monkeypatch.setattr(ss, "_api", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("SPOTIFY API 404: Player command failed: No active device")))
    assert "not available" in ss.playback_state()


@pytest.mark.parametrize("tracks,expect", [
    ({"total": 12}, "12 tracks"),
    ({}, "? tracks"),
    (None, "? tracks"),
    ({"total": 0}, "0 tracks"),
])
def test_list_playlists_never_invents_zero(monkeypatch, tracks, expect):
    item = {"name": "Mix", "uri": "spotify:playlist:1", "tracks": tracks}
    if tracks is None:
        item.pop("tracks")
        item["tracks"] = None
    monkeypatch.setattr(ss, "_api", lambda *a, **k: {"items": [item]})
    assert expect in ss.list_playlists()


# ---------------------------------------------------------------- P3-65 / P3-63

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _get(port: int, query: str) -> int:
    try:
        return urllib.request.urlopen(f"http://127.0.0.1:{port}/callback?{query}", timeout=3).status
    except urllib.error.HTTPError as exc:
        return exc.code


def _start_login(monkeypatch, port, **env):
    monkeypatch.setattr(ss, "_REDIRECT_PORT", port)
    monkeypatch.setattr(ss.webbrowser, "open", lambda url: True)
    monkeypatch.setattr(ss, "_populate_account_id", lambda: None)
    monkeypatch.setattr(ss, "_post_form", lambda url, body: {
        "access_token": "at", "refresh_token": "rt", "expires_in": 3600})
    for k, v in env.items():
        monkeypatch.setattr(ss, k, v)
    ss._CallbackHandler.expected_state = ""
    result: dict[str, str] = {}
    t = threading.Thread(target=lambda: result.update(text=ss.spotify_login()), daemon=True)
    t.start()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not ss._CallbackHandler.expected_state:
        time.sleep(0.05)
    time.sleep(0.2)  # server is bound before expected_state is read by tests that follow
    return t, result


def test_stray_callback_with_wrong_state_does_not_abort_the_login(monkeypatch):
    port = _free_port()
    t, result = _start_login(monkeypatch, port)
    state = ss._CallbackHandler.expected_state
    assert state
    assert _get(port, "code=x&state=WRONG") == 400            # a web page poking the port
    assert _get(port, "code=x") == 400                         # no state at all
    assert _get(port, f"code=thecode&state={state}") == 200    # the real approval still works
    t.join(timeout=20)
    assert "LINKED" in result["text"]
    assert ss._load_tokens()["refresh_token"] == "rt"


def test_error_callback_is_terminal_not_a_180_second_wait(monkeypatch):
    port = _free_port()
    t, result = _start_login(monkeypatch, port)
    state = ss._CallbackHandler.expected_state
    t0 = time.monotonic()
    _get(port, f"error=access_denied&state={state}")
    t.join(timeout=20)
    assert time.monotonic() - t0 < 15
    assert "access_denied" in result["text"] and "Nothing was linked" in result["text"]
    assert ss._load_tokens() == {}


def test_error_callback_with_wrong_state_is_ignored(monkeypatch):
    port = _free_port()
    t, result = _start_login(monkeypatch, port, _LOGIN_TIMEOUT=4)
    assert _get(port, "error=access_denied&state=WRONG") == 400
    t.join(timeout=20)
    assert "TIMED OUT" in result["text"]


def test_token_file_is_private():
    import os
    import stat

    ss._save_tokens({"refresh_token": "r"})
    assert stat.S_IMODE(os.stat(ss._tokens_path()).st_mode) == 0o600


def test_background_login_reports_a_bind_failure_instead_of_claiming_a_tab_opened(monkeypatch):
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen(1)
    port = held.getsockname()[1]
    opened = []
    monkeypatch.setattr(ss, "_REDIRECT_PORT", port)
    monkeypatch.setattr(ss.webbrowser, "open", lambda url: opened.append(url) or True)
    try:
        msg = ss.spotify_login(background=True)
    finally:
        held.close()
    assert "STARTED" not in msg and "opened" not in msg.lower().replace("nothing was opened", "")
    assert "127.0.0.1" in msg and str(port) in msg
    assert opened == []  # nothing was opened


def test_foreground_login_reports_a_bind_failure_instead_of_raising(monkeypatch):
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen(1)
    monkeypatch.setattr(ss, "_REDIRECT_PORT", held.getsockname()[1])
    monkeypatch.setattr(ss.webbrowser, "open", lambda url: True)
    try:
        msg = ss.spotify_login()
    finally:
        held.close()
    assert msg.startswith("SPOTIFY LOGIN FAILED")


def test_background_login_failure_is_visible_in_status(monkeypatch):
    port = _free_port()
    monkeypatch.setattr(ss, "_REDIRECT_PORT", port)
    monkeypatch.setattr(ss.webbrowser, "open", lambda url: True)
    monkeypatch.setattr(ss, "_LOGIN_TIMEOUT", 3)
    msg = ss.spotify_login(background=True)
    assert "STARTED" in msg
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and "TIMED OUT" not in str(ss.status().get("last_login", "")):
        time.sleep(0.2)
    assert "TIMED OUT" in ss.status()["last_login"]


# ---------------------------------------------------------------- H-FI2-2: a transient read failure must not erase the link

def test_populate_account_id_does_not_rewrite_tokens_it_could_not_read(monkeypatch):
    _linked(expires_at=time.time() + 3600)
    before = ss._tokens_path().read_bytes()
    monkeypatch.setattr(ss, "_api", lambda *a, **k: {"id": "u1", "display_name": "Me"})
    monkeypatch.setattr(ss, "_load_tokens", lambda: {})  # the file was briefly unreadable
    ss._populate_account_id()
    assert ss._tokens_path().read_bytes() == before  # the refresh token is still there


def test_populate_account_id_still_stamps_the_name():
    _linked(expires_at=time.time() + 3600)
    import unittest.mock as um

    with um.patch.object(ss, "_api", lambda *a, **k: {"id": "u1", "display_name": "Me"}):
        ss._populate_account_id()
    assert ss.linked_account() == "Me" and ss._load_tokens()["refresh_token"] == "rt"
