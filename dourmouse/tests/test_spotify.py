"""Tests for the Spotify integration (v5.7).

Covers the OAuth2 PKCE math (RFC 7636 S256), the one-time login flow end to
end against a REAL loopback callback server (no browser, no network beyond
127.0.0.1), automatic refresh-on-401, the honest NOT CONFIGURED / NOT LINKED
contract, tool text formatting, and the roster wiring (music subagent with
confirmation-gated control tools).
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Self

import pytest

from dourmouse import connections
from dourmouse import spotify_services as ss
from dourmouse.dispatch import Permission
from dourmouse.general_roster import build_general_registry


def _free_port() -> int:
    """A currently-free loopback port (best effort; the callback server binds
    immediately after, so the race window is tiny)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(autouse=True)
def _workspace(tmp_path, monkeypatch):
    """Hermetic: token store lives in a tmp workspace; never the real one."""
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    monkeypatch.setattr(ss, "SPOTIFY_CLIENT_ID", None, raising=False)
    return tmp_path


def _set_client_id(monkeypatch, value: str = "test-client-id") -> None:
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", value)


# --------------------------------------------------------------------------- #
# PKCE math (RFC 7636)
# --------------------------------------------------------------------------- #
class TestPkce:
    def test_verifier_and_challenge_relationship(self):
        import base64
        import hashlib

        verifier, challenge = ss._pkce_pair()
        assert 43 <= len(verifier) <= 128
        # S256 challenge = base64url(sha256(verifier)), no padding.
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        assert challenge == expected
        assert "=" not in verifier
        assert "=" not in challenge

    def test_two_pairs_differ(self):
        a = ss._pkce_pair()
        b = ss._pkce_pair()
        assert a != b


# --------------------------------------------------------------------------- #
# Honest gating — no Client ID / no linked account
# --------------------------------------------------------------------------- #
class TestHonesty:
    def test_not_configured_without_client_id(self, monkeypatch):
        monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
        assert ss.spotify_configured() is False
        st = ss.status()
        assert st["configured"] is False and st["linked"] is False
        assert "NOT CONFIGURED" in ss.spotify_login()

    def test_login_never_opens_browser_when_unconfigured(self, monkeypatch):
        opened: list[str] = []
        monkeypatch.setattr(ss.webbrowser, "open", lambda url: opened.append(url) or True)
        assert "NOT CONFIGURED" in ss.spotify_login()
        assert opened == []

    def test_not_linked_without_tokens(self, _workspace, monkeypatch):
        _set_client_id(monkeypatch)
        assert ss.spotify_configured() is True
        st = ss.status()
        assert st["configured"] is True and st["linked"] is False
        with pytest.raises(RuntimeError, match="NOT LINKED"):
            ss.now_playing()

    def test_client_id_from_local_secrets(self, _workspace, monkeypatch, tmp_path):
        monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
        secrets_file = (
            Path(__file__).resolve().parent.parent / "local_secrets.py"
        )
        assert ss._client_id() == ""  # placeholder is empty
        assert secrets_file.is_file()


# --------------------------------------------------------------------------- #
# One-time login — end to end against a real loopback callback server
# --------------------------------------------------------------------------- #
class TestLoginFlow:
    def test_full_login_saves_tokens(self, _workspace, monkeypatch):
        _set_client_id(monkeypatch)
        # A free loopback port for the callback server.
        sock = _free_port()
        monkeypatch.setattr(ss, "_REDIRECT_PORT", sock)

        exchanged: dict[str, str] = {}

        def fake_post_form(url: str, body: bytes) -> dict[str, object]:
            parsed = urllib.parse.parse_qs(body.decode())
            exchanged["grant_type"] = parsed.get("grant_type", [""])[0]
            exchanged["code_verifier"] = parsed.get("code_verifier", [""])[0]
            return {
                "access_token": "at-123",
                "refresh_token": "rt-abc",
                "expires_in": 3600,
                "scope": "user-read-currently-playing",
            }

        monkeypatch.setattr(ss, "_post_form", fake_post_form)
        opened: list[str] = []
        monkeypatch.setattr(ss.webbrowser, "open", lambda url: opened.append(url) or True)

        def fake_populate() -> None:
            tokens = ss._load_tokens()
            tokens["display_name"] = "Test User"
            tokens["user_id"] = "testuser"
            ss._save_tokens(tokens)

        monkeypatch.setattr(ss, "_populate_account_id", fake_populate)

        result: dict[str, str] = {}

        def _run() -> None:
            result["text"] = ss.spotify_login()

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        # Wait for the server to be up + expected_state set, then fire the
        # callback the way the browser would.
        deadline = time.monotonic() + 30
        state = ""
        while time.monotonic() < deadline:
            state = ss._CallbackHandler.expected_state
            if state:
                break
            time.sleep(0.05)
        assert state, "login never started its callback server"

        fired = False
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{sock}/callback?code=thecode&state={state}",
                    timeout=2,
                ).read()
                fired = True
                break
            except (OSError, urllib.error.URLError):
                time.sleep(0.1)
        assert fired, "could not reach the callback server"
        thread.join(timeout=30)

        assert "LINKED" in result["text"]
        assert exchanged["grant_type"] == "authorization_code"
        assert exchanged["code_verifier"]  # PKCE verifier sent
        assert opened and "accounts.spotify.com/authorize" in opened[0]
        tokens = ss._load_tokens()
        assert tokens["refresh_token"] == "rt-abc"
        assert tokens["access_token"] == "at-123"
        assert ss.linked_account() == "Test User"
        assert ss.status()["linked"] is True

    def test_state_mismatch_rejected(self, _workspace, monkeypatch):
        """CSRF guard: a callback with the wrong state must not link."""
        _set_client_id(monkeypatch)
        sock = _free_port()
        monkeypatch.setattr(ss, "_REDIRECT_PORT", sock)
        monkeypatch.setattr(
            ss, "_post_form",
            lambda url, body: {"access_token": "a", "refresh_token": "r", "expires_in": 3600},
        )
        monkeypatch.setattr(ss.webbrowser, "open", lambda url: True)
        monkeypatch.setattr(ss, "_populate_account_id", lambda: None)

        result: dict[str, str] = {}

        def _run() -> None:
            result["text"] = ss.spotify_login()

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not ss._CallbackHandler.expected_state:
            time.sleep(0.05)
        fired = False
        while time.monotonic() < deadline and not fired:
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{sock}/callback?code=thecode&state=WRONG_STATE",
                    timeout=2,
                ).read()
                fired = True
            except (OSError, urllib.error.URLError):
                time.sleep(0.1)
        assert fired, "could not reach the callback server"
        thread.join(timeout=30)
        assert "state mismatch" in result["text"]
        assert ss._load_tokens() == {}  # nothing saved


# --------------------------------------------------------------------------- #
# API client — refresh on 401, formatting, errors
# --------------------------------------------------------------------------- #
class _FakeResp:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class TestApiClient:
    def test_refresh_on_401(self, _workspace, monkeypatch):
        _set_client_id(monkeypatch)
        ss._save_tokens(
            {
                "access_token": "old-token",
                "refresh_token": "rt-abc",
                "expires_at": time.time() + 9999,  # not expired; server says 401
            }
        )
        calls: list[dict[str, object]] = []

        def fake_urlopen(req: urllib.request.Request, **kw: object) -> object:
            url = req.full_url
            calls.append({"url": url, "auth": req.headers.get("Authorization", "")})
            if "/api/token" in url:
                return _FakeResp({"access_token": "new-token", "expires_in": 3600})
            if req.headers.get("Authorization") == "Bearer old-token":
                raise urllib.error.HTTPError(url, 401, "unauthorized", {}, None)
            return _FakeResp({"me": "ok"})

        monkeypatch.setattr(ss.urllib.request, "urlopen", fake_urlopen)
        data = ss._api("GET", "/me")
        assert data == {"me": "ok"}
        assert ss._load_tokens()["access_token"] == "new-token"
        # The retry used the refreshed token.
        auths = [c["auth"] for c in calls if "/me" in c["url"]]
        assert "Bearer new-token" in auths

    def test_now_playing_formatting(self, _workspace, monkeypatch):
        _set_client_id(monkeypatch)
        ss._save_tokens(
            {"access_token": "t", "refresh_token": "r", "expires_at": time.time() + 9999}
        )
        monkeypatch.setattr(
            ss, "_api",
            lambda method, path, params=None: {
                "is_playing": True,
                "progress_ms": 65_000,
                "item": {
                    "name": "Get Lucky",
                    "artists": [{"name": "Daft Punk"}, {"name": "Pharrell"}],
                    "duration_ms": 369_000,
                },
            },
        )
        text = ss.now_playing()
        assert "Get Lucky" in text and "Daft Punk" in text
        assert "1:05" in text and "6:09" in text

    def test_search_formatting(self, _workspace, monkeypatch):
        _set_client_id(monkeypatch)
        ss._save_tokens(
            {"access_token": "t", "refresh_token": "r", "expires_at": time.time() + 9999}
        )
        monkeypatch.setattr(
            ss, "_api",
            lambda method, path, params=None: {
                "tracks": {"items": [
                    {"name": "Around the World", "artists": [{"name": "Daft Punk"}], "uri": "spotify:track:1"},
                ]}
            },
        )
        text = ss.search_tracks("daft punk")
        assert "Around the World" in text and "spotify:track:1" in text

    def test_control_action_validation(self, _workspace, monkeypatch):
        _set_client_id(monkeypatch)
        text = ss.playback_control("bogus-action")
        assert "ERROR" in text

    def test_api_without_refresh_is_honest(self, _workspace, monkeypatch):
        """No refresh token = refuse BEFORE any request, with the exact fix."""
        _set_client_id(monkeypatch)
        ss._save_tokens(
            {"access_token": "t", "refresh_token": "", "expires_at": time.time() + 9999}
        )
        attempted: list[str] = []

        def fake_urlopen(req: urllib.request.Request, **kw: object) -> object:
            attempted.append(req.full_url)
            raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, None)

        monkeypatch.setattr(ss.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(RuntimeError, match="NOT LINKED"):
            ss._api("GET", "/me")
        assert attempted == []  # no network call without a usable token


# --------------------------------------------------------------------------- #
# Roster + connections wiring
# --------------------------------------------------------------------------- #
class TestWiring:
    def test_music_subagent_registered(self):
        reg = build_general_registry()
        music = reg.get_subagent("music")
        assert music is not None
        names = {t.name for t in music.tools}
        assert {
            "spotify_link", "spotify_now_playing", "spotify_playback_state",
            "spotify_playback_control", "spotify_play", "spotify_search",
            "spotify_top_tracks", "spotify_recently_played", "spotify_playlists",
        } <= names
        gated = {
            t.name for t in music.tools
            if t.permission is Permission.REQUIRES_CONFIRMATION
        }
        assert {"spotify_playback_control", "spotify_play"} <= gated

    def test_connections_has_spotify_row(self, _workspace, monkeypatch):
        monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
        report = connections.check_connections()
        assert "spotify" in report
        assert report["spotify"]["ok"] is False
        assert "SPOTIFY_CLIENT_ID" in report["spotify"]["hint"]
