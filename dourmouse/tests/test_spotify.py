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

    def test_client_id_from_local_secrets(self, _workspace, monkeypatch):
        """Env wins, then the gitignored local_secrets module (Rule 2.2).
        Hermetic: inject a fake module instead of requiring the real
        gitignored file, so the test passes on fresh checkouts/CI."""
        import sys

        monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
        assert ss._client_id() == ""  # no env, no local_secrets -> empty

        fake = type(sys)("dourmouse.local_secrets")
        fake.SPOTIFY_CLIENT_ID = "fake-from-local-secrets"
        monkeypatch.setitem(sys.modules, "dourmouse.local_secrets", fake)
        assert ss._client_id() == "fake-from-local-secrets"

        monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
        monkeypatch.setenv("SPOTIFY_CLIENT_ID", "env-wins")
        assert ss._client_id() == "env-wins"


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

    def test_empty_success_body_is_not_an_error(self, _workspace, monkeypatch):
        """Regression: playback endpoints answer 204/empty on SUCCESS.
        The old code raised JSONDecodeError ("Expecting value") on an empty
        body and reported a successful pause/resume as a failure."""
        _set_client_id(monkeypatch)
        ss._save_tokens(
            {"access_token": "t", "refresh_token": "r", "expires_at": time.time() + 9999}
        )

        class _EmptyResp:
            def read(self):
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

        monkeypatch.setattr(
            ss.urllib.request, "urlopen",
            lambda req, **kw: _EmptyResp(),
        )
        data = ss._api("PUT", "/me/player/pause")
        assert data == {}  # success, not a JSONDecodeError

    def test_command_200_opaque_body_is_success(self, _workspace, monkeypatch):
        """Regression (found live): Spotify answers PUT commands with 200
        and an opaque NON-JSON token body (e.g. b'og7U813...'). That is
        SUCCESS for pause/resume/next — the old code raised
        JSONDecodeError and reported a working command as a failure."""
        _set_client_id(monkeypatch)
        ss._save_tokens(
            {"access_token": "t", "refresh_token": "r", "expires_at": time.time() + 9999}
        )

        class _OpaqueResp:
            def read(self):
                return b"og7U813vcVLnQIkWs4sb8eiwKPU"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

        monkeypatch.setattr(
            ss.urllib.request, "urlopen",
            lambda req, **kw: _OpaqueResp(),
        )
        data = ss._api("PUT", "/me/player/pause")
        assert data == {}  # command succeeded

    def test_get_opaque_body_is_honest_error(self, _workspace, monkeypatch):
        """A GET that returns non-JSON is a REAL problem and must surface,
        never be masked as an empty read (Rule 2.2)."""
        _set_client_id(monkeypatch)
        ss._save_tokens(
            {"access_token": "t", "refresh_token": "r", "expires_at": time.time() + 9999}
        )

        class _GarbageResp:
            def read(self):
                return b"not-json-at-all"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

        monkeypatch.setattr(
            ss.urllib.request, "urlopen",
            lambda req, **kw: _GarbageResp(),
        )
        with pytest.raises(ValueError):
            ss._api("GET", "/search")

    def test_403_surfaces_real_body(self, _workspace, monkeypatch):
        """Regression: a 403 without Premium in the body reports the REAL
        reason, not a guess that it needs Premium."""
        _set_client_id(monkeypatch)
        ss._save_tokens(
            {"access_token": "t", "refresh_token": "r", "expires_at": time.time() + 9999}
        )

        def fake_urlopen(req, **kw):
            exc = urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)
            exc.read = lambda: b'{"error": {"reason": "Restriction violated"}}'
            raise exc

        monkeypatch.setattr(ss.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(RuntimeError) as err:
            ss._api("PUT", "/me/player/pause")
        assert "Restriction violated" in str(err.value)
        assert "Premium" not in str(err.value)

    def test_403_premium_keeps_honest_hint(self, _workspace, monkeypatch):
        """A 403 that really says Premium keeps the actionable hint."""
        _set_client_id(monkeypatch)
        ss._save_tokens(
            {"access_token": "t", "refresh_token": "r", "expires_at": time.time() + 9999}
        )

        def fake_urlopen(req, **kw):
            exc = urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)
            exc.read = lambda: b'{"error": {"reason": "Premium required"}}'
            raise exc

        monkeypatch.setattr(ss.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(RuntimeError) as err:
            ss._api("PUT", "/me/player/pause")
        assert "Premium" in str(err.value)

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


# --------------------------------------------------------------------------- #
# v5.21 play-anything — JSON bodies + structured HUD data
# --------------------------------------------------------------------------- #
class TestPlayAnything:
    """The HUD music section's backend: play_uri must send the right JSON
    body (tracks in ``uris``, playlists/albums/artists in ``context_uri`` —
    the API rejects playlist URIs inside ``uris``), and the structured data
    helpers return rows the panel renders as clickable play buttons.
    """

    @staticmethod
    def _capture_api() -> tuple[list[dict[str, object]], dict]:
        calls: list[dict[str, object]] = []

        def fake_api(method: str, path: str, params=None, body=None):
            calls.append(
                {"method": method, "path": path, "params": params, "body": body}
            )
            return {}

        return calls, fake_api

    def test_play_track_sends_uris_body(self, _workspace, monkeypatch):
        _set_client_id(monkeypatch)
        ss._save_tokens(
            {"access_token": "t", "refresh_token": "r", "expires_at": time.time() + 9999}
        )
        calls, fake_api = self._capture_api()
        monkeypatch.setattr(ss, "_api", fake_api)
        text = ss.play_uri("spotify:track:abc123")
        assert "playback started" in text
        assert calls[-1]["path"] == "/me/player/play"
        assert calls[-1]["body"] == {"uris": ["spotify:track:abc123"]}

    def test_play_fabricated_playlist_uri_errors_honestly(self, _workspace, monkeypatch):
        """v5.22.4: Spotify answers a play command with a NON-EXISTENT context
        URI as 204 (looks like success) while playing NOTHING. play_uri must
        verify the context exists first and, on 404, fail loudly listing the
        account's real playlists — never claim 'playback started' for an
        LLM-fabricated URI (root cause of 'play my jazz playlist' reporting
        success with nothing playing)."""
        _set_client_id(monkeypatch)
        ss._save_tokens(
            {"access_token": "t", "refresh_token": "r", "expires_at": time.time() + 9999}
        )
        verified: list[str] = []

        def fake_api(method, path, params=None, body=None):
            if method == "GET" and path.startswith("/playlists/"):
                verified.append(path)
                raise RuntimeError("SPOTIFY API 404: playlist not found")
            if path == "/me/playlists":
                return {"items": [{"name": "jazz bar in nyc"}, {"name": "Chill"}]}
            raise AssertionError(f"play should never reach {method} {path}")

        monkeypatch.setattr(ss, "_api", fake_api)
        text = ss.play_uri("spotify:playlist:37i9dQZF1DX6VcZtOu48Rv")
        assert verified == ["/playlists/37i9dQZF1DX6VcZtOu48Rv"]
        assert "does not exist" in text
        assert "jazz bar in nyc" in text
        assert "Chill" in text
        assert "spotify_playlists" in text
        assert "playback started" not in text

    def test_play_verified_playlist_still_plays(self, _workspace, monkeypatch):
        """A real playlist passes the existence check and plays as a context
        (verification GET before the PUT, never after a success lie)."""
        _set_client_id(monkeypatch)
        ss._save_tokens(
            {"access_token": "t", "refresh_token": "r", "expires_at": time.time() + 9999}
        )
        calls, fake_api = self._capture_api()
        monkeypatch.setattr(ss, "_api", fake_api)
        text = ss.play_uri("spotify:playlist:xyz789")
        assert "playback started" in text
        # Existence GET first, then the play PUT with context_uri body.
        assert calls[0] == {"method": "GET", "path": "/playlists/xyz789",
                            "params": None, "body": None}
        assert calls[-1]["path"] == "/me/player/play"
        assert calls[-1]["body"] == {"context_uri": "spotify:playlist:xyz789"}

    def test_play_playlist_sends_context_uri_body(self, _workspace, monkeypatch):
        """Playlists are contexts, not uris — this is the bug that made the
        old HUD play impossible (query-string params, not a JSON body)."""
        _set_client_id(monkeypatch)
        ss._save_tokens(
            {"access_token": "t", "refresh_token": "r", "expires_at": time.time() + 9999}
        )
        calls, fake_api = self._capture_api()
        monkeypatch.setattr(ss, "_api", fake_api)
        text = ss.play_uri("spotify:playlist:xyz789")
        assert "playback started" in text
        assert calls[-1]["body"] == {"context_uri": "spotify:playlist:xyz789"}

    def test_play_rejects_non_spotify_uri(self, _workspace, monkeypatch):
        _set_client_id(monkeypatch)
        text = ss.play_uri("https://evil.example/")
        assert "ERROR" in text

    def test_play_self_heals_no_active_device(self, _workspace, monkeypatch):
        """NO_ACTIVE_DEVICE is the one honest failure play can self-heal:
        retry once naming an available device (active preferred) so a
        cold-but-open player still starts."""
        _set_client_id(monkeypatch)
        ss._save_tokens(
            {"access_token": "t", "refresh_token": "r", "expires_at": time.time() + 9999}
        )
        calls: list[tuple[str, dict | None]] = []
        fail = {"failures": 1}

        def fake_api(method, path, params=None, body=None):
            # The existence check (v5.22.4) GETs the track first — pass it.
            if path == "/tracks/abc":
                return {}
            if path == "/me/player/devices":
                return {"devices": [{"id": "mac-air", "is_active": False, "name": "MacBook"}]}
            if fail["failures"]:
                fail["failures"] -= 1
                raise RuntimeError("SPOTIFY API 404: NO_ACTIVE_DEVICE")
            calls.append((path, params))
            return {}

        monkeypatch.setattr(ss, "_api", fake_api)
        text = ss.play_uri("spotify:track:abc")
        assert "playback started" in text
        # The retry targeted the device explicitly via query params.
        assert calls == [("/me/player/play", {"device_id": "mac-air"})]

    def test_play_no_device_keeps_honest_error(self, _workspace, monkeypatch):
        """No devices at all -> the real error surfaces, never a lie."""
        _set_client_id(monkeypatch)
        ss._save_tokens(
            {"access_token": "t", "refresh_token": "r", "expires_at": time.time() + 9999}
        )

        def fake_api(method, path, params=None, body=None):
            # Existence check passes; the play then hits no devices.
            if path == "/tracks/abc":
                return {}
            if path == "/me/player/devices":
                return {"devices": []}
            raise RuntimeError("SPOTIFY API 404: NO_ACTIVE_DEVICE")

        monkeypatch.setattr(ss, "_api", fake_api)
        with pytest.raises(RuntimeError, match="NO_ACTIVE_DEVICE"):
            ss.play_uri("spotify:track:abc")

    def test_search_tracks_data_structured(self, _workspace, monkeypatch):
        _set_client_id(monkeypatch)
        ss._save_tokens(
            {"access_token": "t", "refresh_token": "r", "expires_at": time.time() + 9999}
        )
        monkeypatch.setattr(
            ss, "_api",
            lambda method, path, params=None, body=None: {
                "tracks": {"items": [
                    {
                        "name": "Around the World",
                        "artists": [{"name": "Daft Punk"}, {"name": "Bangalter"}],
                        "uri": "spotify:track:1",
                    },
                ]}
            },
        )
        rows = ss.search_tracks_data("daft punk", limit=8)
        assert rows == [
            {"name": "Around the World", "artists": "Daft Punk, Bangalter", "uri": "spotify:track:1"}
        ]

    def test_playlists_data_structured(self, _workspace, monkeypatch):
        _set_client_id(monkeypatch)
        ss._save_tokens(
            {"access_token": "t", "refresh_token": "r", "expires_at": time.time() + 9999}
        )
        monkeypatch.setattr(
            ss, "_api",
            lambda method, path, params=None, body=None: {
                "items": [
                    {"name": "Chill", "uri": "spotify:playlist:9", "tracks": {"total": 42}},
                ]
            },
        )
        rows = ss.playlists_data()
        assert rows == [{"name": "Chill", "uri": "spotify:playlist:9", "tracks": 42}]

    def test_playlists_data_missing_count_is_none_not_zero(self, _workspace, monkeypatch):
        """Honesty: when the API omits the tracks field, report None (the UI
        shows '?'), never a fabricated 0 — a playlist with tracks must not
        display as empty."""
        _set_client_id(monkeypatch)
        ss._save_tokens(
            {"access_token": "t", "refresh_token": "r", "expires_at": time.time() + 9999}
        )
        monkeypatch.setattr(
            ss, "_api",
            lambda method, path, params=None, body=None: {
                "items": [{"name": "Led Zeppelin - Greatest Hits", "uri": "spotify:playlist:Zep"}]
            },
        )
        rows = ss.playlists_data()
        assert rows == [{"name": "Led Zeppelin - Greatest Hits", "uri": "spotify:playlist:Zep", "tracks": None}]

    def test_bad_limit_falls_back_not_crash(self, _workspace, monkeypatch):
        """A non-numeric limit must not 500 the endpoint: helpers coerce it."""
        _set_client_id(monkeypatch)
        ss._save_tokens(
            {"access_token": "t", "refresh_token": "r", "expires_at": time.time() + 9999}
        )
        calls: list[int] = []

        def fake_api(method, path, params=None, body=None):
            calls.append(params["limit"] if params else 0)
            return {"tracks": {"items": []}}

        monkeypatch.setattr(ss, "_api", fake_api)
        assert ss.search_tracks_data("daft punk", limit="abc") == []
        assert calls[-1] == 8  # the coerced fallback

    def test_recently_played_data_structured(self, _workspace, monkeypatch):
        _set_client_id(monkeypatch)
        ss._save_tokens(
            {"access_token": "t", "refresh_token": "r", "expires_at": time.time() + 9999}
        )
        monkeypatch.setattr(
            ss, "_api",
            lambda method, path, params=None, body=None: {
                "items": [
                    {
                        "track": {
                            "name": "Get Lucky",
                            "artists": [{"name": "Daft Punk"}],
                            "uri": "spotify:track:7",
                        },
                        "played_at": "2026-08-09T15:44:00",
                    },
                ]
            },
        )
        rows = ss.recently_played_data()
        assert rows == [{"name": "Get Lucky", "artists": "Daft Punk", "uri": "spotify:track:7"}]

    def test_top_tracks_data_structured(self, _workspace, monkeypatch):
        _set_client_id(monkeypatch)
        ss._save_tokens(
            {"access_token": "t", "refresh_token": "r", "expires_at": time.time() + 9999}
        )
        monkeypatch.setattr(
            ss, "_api",
            lambda method, path, params=None, body=None: {
                "items": [
                    {
                        "name": "Around the World",
                        "artists": [{"name": "Daft Punk"}],
                        "uri": "spotify:track:2",
                    },
                ]
            },
        )
        rows = ss.top_tracks_data()
        assert rows == [{"name": "Around the World", "artists": "Daft Punk", "uri": "spotify:track:2"}]

    def test_top_tracks_data_bad_range_falls_back(self, _workspace, monkeypatch):
        """An invalid time_range silently coerces to medium_term (no crash)."""
        _set_client_id(monkeypatch)
        ss._save_tokens(
            {"access_token": "t", "refresh_token": "r", "expires_at": time.time() + 9999}
        )
        seen: list[str] = []

        def fake_api(method, path, params=None, body=None):
            seen.append(params["time_range"])
            return {"items": []}

        monkeypatch.setattr(ss, "_api", fake_api)
        assert ss.top_tracks_data(time_range="bogus") == []
        assert seen == ["medium_term"]

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
        # v13.2: ungated on explicit user request — playback control is
        # reversible/low-stakes, unlike an email send; the user asks for
        # it by name every time anyway, so the confirmation was pure
        # friction. See general_roster.py's own comment on both tools.
        gated = {
            t.name for t in music.tools
            if t.permission is Permission.REQUIRES_CONFIRMATION
        }
        assert not ({"spotify_playback_control", "spotify_play"} & gated)

    def test_connections_has_spotify_row(self, _workspace, monkeypatch):
        monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
        report = connections.check_connections()
        assert "spotify" in report
        assert report["spotify"]["ok"] is False
        assert "SPOTIFY_CLIENT_ID" in report["spotify"]["hint"]
