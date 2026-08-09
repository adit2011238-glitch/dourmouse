"""Spotify integration (v5.7) — stdlib-only OAuth2 (PKCE) music control.

Brings the user's Spotify account into Dourmouse the same way Gmail was
brought in: no external SDK, no client-side secrets beyond a Client ID, and
an honest NOT CONFIGURED contract (Rule 2.2) whenever something is missing.

Why PKCE (Authorization Code + PKCE, the public-client flow): Spotify issues
refresh tokens that NEVER expire, and PKCE needs only the **Client ID** — no
client secret to leak or rotate. One-time linking is a local browser dance:

1. User creates a Spotify app at developer.spotify.com (free) and adds the
   redirect URI ``http://127.0.0.1:8766/callback`` (SPOTIFY_REDIRECT_PORT).
2. SPOTIFY_CLIENT_ID goes in .env or dourmouse/local_secrets.py.
3. ``spotify_login()`` (CLI: ``python -m dourmouse.spotify_services --login``,
   or the ``spotify_link`` tool / HUD [LOGIN] button) opens the browser,
   serves a loopback callback on 127.0.0.1:<port>, exchanges the code, and
   saves the refresh token to <workspace>/spotify_tokens.json (gitignored).
4. From then on the API client auto-refreshes the access token silently.

Honesty contract: missing Client ID -> NOT CONFIGURED with the exact fix;
no linked account -> LINK REQUIRED; API errors surface their real message.
Playback CONTROL (play/pause/skip/seek/shuffle) requires Spotify Premium and
is confirmation-gated at the roster level — a human approves every action
that changes playback on the user's account (Rule 2.9).

Env / secrets:
- SPOTIFY_CLIENT_ID           (env wins) or local_secrets.SPOTIFY_CLIENT_ID
- SPOTIFY_REDIRECT_PORT       (default 8766; must match the app's registered
  redirect URI http://127.0.0.1:<port>/callback)
- DOURMOUSE_WORKSPACE         token file location (default <project>/workspace)
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_WORKSPACE_ENV = "DOURMOUSE_WORKSPACE"

_AUTH_URL = "https://accounts.spotify.com/authorize"
_TOKEN_URL = "https://accounts.spotify.com/api/token"
_API_URL = "https://api.spotify.com/v1"
_REDIRECT_PORT = int(os.environ.get("SPOTIFY_REDIRECT_PORT", "8766"))
_LOGIN_TIMEOUT = 180  # seconds a login waits for the browser approval

# Scopes: read-only history/taste plus playback state/control and playlists.
_SCOPES = (
    "user-read-currently-playing user-read-playback-state "
    "user-modify-playback-state user-read-recently-played "
    "user-top-read playlist-read-private user-read-private"
)


# --------------------------------------------------------------------------- #
# Configuration — env wins, then the gitignored local_secrets.py (v5.1)
# --------------------------------------------------------------------------- #
def _client_id() -> str:
    env = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    if env:
        return env
    import importlib

    try:
        local_secrets = importlib.import_module("dourmouse.local_secrets")
        return str(getattr(local_secrets, "SPOTIFY_CLIENT_ID", "") or "").strip()
    except Exception:  # noqa: BLE001 - missing/broken file = no fallback
        return ""


def spotify_configured() -> bool:
    """True when a Client ID is set (deterministic, Rule 2.8)."""
    return bool(_client_id())


def _workspace_root() -> Path:
    raw = os.environ.get(_WORKSPACE_ENV)
    return (Path(raw).expanduser() if raw else _PROJECT_ROOT / "workspace")


def _tokens_path() -> Path:
    return _workspace_root() / "spotify_tokens.json"


def _redirect_uri() -> str:
    return f"http://127.0.0.1:{_REDIRECT_PORT}/callback"


# --------------------------------------------------------------------------- #
# Token store — access token + never-expiring refresh token
# --------------------------------------------------------------------------- #
def _load_tokens() -> dict[str, Any]:
    path = _tokens_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_tokens(tokens: dict[str, Any]) -> None:
    path = _tokens_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tokens, indent=2))


def _clear_tokens() -> None:
    try:
        _tokens_path().unlink(missing_ok=True)
    except OSError:
        pass


def linked_account() -> str | None:
    """The Spotify display name / id of the linked account, or None."""
    return str(_load_tokens().get("display_name") or _load_tokens().get("user_id") or "").strip() or None


def _access_token() -> str:
    tokens = _load_tokens()
    if not tokens.get("refresh_token"):
        return ""
    if float(tokens.get("expires_at", 0)) <= time.time() + 60:
        _refresh_access_token(tokens)
        tokens = _load_tokens()
    return str(tokens.get("access_token") or "")


def _refresh_access_token(tokens: dict[str, Any]) -> None:
    """Exchange the stored refresh token for a fresh access token (PKCE)."""
    client_id = _client_id()
    refresh = str(tokens.get("refresh_token") or "")
    if not client_id or not refresh:
        return
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": client_id,
        }
    ).encode()
    data = _post_form(_TOKEN_URL, body)
    now = time.time()
    tokens.update(
        {
            "access_token": str(data.get("access_token") or ""),
            "expires_at": now + float(data.get("expires_in", 3600)),
            "scope": str(data.get("scope") or tokens.get("scope") or ""),
        }
    )
    _save_tokens(tokens)


# --------------------------------------------------------------------------- #
# HTTP plumbing (stdlib only)
# --------------------------------------------------------------------------- #
def _post_form(url: str, body: bytes) -> dict[str, Any]:
    """POST a form body; returns parsed JSON. Raises RuntimeError on failure."""
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _api(method: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call the Spotify Web API with automatic one-shot refresh-on-401.

    Raises RuntimeError with the REAL error on failure — never fabricates.
    """
    token = _access_token()
    if not token:
        raise RuntimeError(
            "NOT LINKED: no Spotify account linked yet — run spotify_login "
            "(python -m dourmouse.spotify_services --login) or the spotify_link "
            "tool, or press [LOGIN] in the Spotify panel. Nothing was fetched."
        )
    url = _API_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"}, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        return _handle_http_error(exc, method, path, params)


def _handle_http_error(
    exc: Any, method: str, path: str, params: dict[str, Any] | None,
) -> dict[str, Any]:

    if exc.code == 401:
        # Access token expired or was revoked: refresh ONCE and retry.
        tokens = _load_tokens()
        if tokens.get("refresh_token"):
            _refresh_access_token(tokens)
            return _api(method, path, params)
        raise RuntimeError(
            "SPOTIFY AUTH FAILED: access token rejected and no refresh token "
            "exists — run spotify_login to link the account again."
        ) from exc
    if exc.code == 403:
        raise RuntimeError(
            "SPOTIFY 403: this action needs Spotify Premium (playback control) "
            "or the linked account lacks permission. Nothing was changed."
        ) from exc
    body = ""
    try:
        body = exc.read().decode("utf-8", errors="replace")[:300]
    except Exception:  # noqa: BLE001, S110 - surface real API errors instead
        pass
    raise RuntimeError(f"SPOTIFY API {exc.code}: {body or exc.reason}") from exc


# --------------------------------------------------------------------------- #
# PKCE login — local callback server + browser
# --------------------------------------------------------------------------- #
class _CallbackHandler(BaseHTTPRequestHandler):
    """Loopback-only callback endpoint; captures ?code= and ?state=."""

    captured_code: str | None = None
    captured_state: str | None = None
    expected_state: str = ""
    _port: int = _REDIRECT_PORT

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        _CallbackHandler.captured_code = (qs.get("code") or [""])[0] or None
        _CallbackHandler.captured_state = (qs.get("state") or [""])[0] or None
        body = (
            b"<html><body style='font-family:monospace;background:#0a111a;color:#6bb3f5'>"
            b"<h2>Dourmouse // Spotify linked</h2>"
            b"<p>You can close this tab and return to Dourmouse.</p></body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter logs
        pass


def _pkce_pair() -> tuple[str, str]:
    """(code_verifier, code_challenge) per RFC 7636 — S256, no base64 padding."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _start_callback_server(port: int) -> ThreadingHTTPServer:
    _CallbackHandler.captured_code = None
    _CallbackHandler.captured_state = None
    server = ThreadingHTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.timeout = 5.0  # let handle_request() return periodically to poll
    return server


def _exchange_code(code: str, verifier: str, client_id: str) -> dict[str, Any]:
    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _redirect_uri(),
            "client_id": client_id,
            "code_verifier": verifier,
        }
    ).encode()
    data = _post_form(_TOKEN_URL, body)
    if not data.get("access_token") or not data.get("refresh_token"):
        raise RuntimeError(
            f"SPOTIFY LOGIN FAILED: token response missing fields "
            f"({sorted(data)}) — check SPOTIFY_CLIENT_ID and the app's "
            "redirect URI in the Spotify Developer Dashboard."
        )
    return data


def spotify_login(background: bool = False) -> str:
    """One-time account linking: browser + loopback callback + token save.

    ``background=True`` (the tool/HUD path) spawns a daemon thread and
    returns immediately; the callback completes and saves tokens whenever the
    user approves in the browser. Foreground mode (CLI) waits up to
    ``_LOGIN_TIMEOUT`` seconds and reports the outcome.
    """
    client_id = _client_id()
    if not client_id:
        return (
            "NOT CONFIGURED: set SPOTIFY_CLIENT_ID in .env or "
            "dourmouse/local_secrets.py (create a free app at "
            "developer.spotify.com and add redirect "
            f"{_redirect_uri()} to its Redirect URIs). Nothing was opened."
        )
    if background:

        def _work() -> None:
            try:
                spotify_login(background=False)
            except Exception:  # noqa: BLE001, S110 - background link failures
                pass  # surface via status()/panel

        threading.Thread(target=_work, daemon=True, name="spotify-login").start()
        return (
            "SPOTIFY LINK STARTED: a browser tab opened — approve the app "
            f"there (timeout {_LOGIN_TIMEOUT}s). The Spotify panel updates "
            "automatically once linked."
        )

    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)
    _CallbackHandler.expected_state = state
    auth_params = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": _redirect_uri(),
            "scope": _SCOPES,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    server = _start_callback_server(_REDIRECT_PORT)
    url = f"{_AUTH_URL}?{auth_params}"
    webbrowser.open(url)
    deadline = time.monotonic() + _LOGIN_TIMEOUT
    try:
        while time.monotonic() < deadline:
            server.handle_request()
            if _CallbackHandler.captured_code:
                break
        if not _CallbackHandler.captured_code:
            return (
                "SPOTIFY LOGIN TIMED OUT: no browser approval received. "
                "Re-run spotify_login. Nothing was linked."
            )
        if _CallbackHandler.captured_state != state:
            return "SPOTIFY LOGIN FAILED: state mismatch (CSRF guard). Retry."
        data = _exchange_code(
            _CallbackHandler.captured_code, verifier, client_id
        )
        _save_tokens(
            {
                "access_token": data.get("access_token"),
                "refresh_token": data.get("refresh_token"),
                "expires_at": time.time() + float(data.get("expires_in", 3600)),
                "scope": data.get("scope", ""),
            }
        )
        _populate_account_id()
        return "SPOTIFY LINKED: account connected — playback tools are live."
    finally:
        server.server_close()


def _populate_account_id() -> None:
    """Stamp the linked account's display name/id into the token store."""
    try:
        me = _api("GET", "/me")
    except Exception:  # noqa: BLE001 - cosmetic; never fail a successful link
        return
    tokens = _load_tokens()
    tokens["user_id"] = me.get("id") or tokens.get("user_id")
    tokens["display_name"] = me.get("display_name") or tokens.get("display_name")
    _save_tokens(tokens)


# --------------------------------------------------------------------------- #
# Read tools
# --------------------------------------------------------------------------- #
def now_playing() -> str:
    """What is currently playing on the linked account (or nothing)."""
    data = _api("GET", "/me/player/currently-playing")
    if not data or not data.get("item"):
        return "SPOTIFY: nothing is currently playing."
    item = data.get("item") or {}
    artists = ", ".join(a.get("name", "") for a in (item.get("artists") or [])[:4])
    name = item.get("name") or "?"
    is_playing = bool(data.get("is_playing"))
    progress = data.get("progress_ms") or 0
    duration = item.get("duration_ms") or 0
    return (
        f"SPOTIFY NOW PLAYING: {'▶' if is_playing else '⏸'} {name} — {artists}"
        f" ({_fmt_ms(progress)} / {_fmt_ms(duration)})"
    )


def playback_state() -> str:
    """Current playback state: device, shuffle, repeat, volume, track."""
    try:
        data = _api("GET", "/me/player")
    except RuntimeError:
        return "SPOTIFY: playback not available on this account/device."
    if not data:
        return "SPOTIFY: no active playback device found."
    device = data.get("device") or {}
    item = data.get("item") or {}
    artists = ", ".join(a.get("name", "") for a in (item.get("artists") or [])[:4])
    lines = [
        (
            f"SPOTIFY PLAYBACK: device={device.get('name') or '?'} "
            f"(volume {device.get('volume_percent')}%)"
        ),
        f"  shuffle={data.get('shuffle_state')} repeat={data.get('repeat_state')}",
        (
            f"  {'▶' if data.get('is_playing') else '⏸'} "
            f"{item.get('name') or '(nothing selected)'} — {artists}"
        ),
    ]
    return "\n".join(lines)


def playback_control(action: str) -> str:
    """Control playback: next|previous|pause|resume|shuffle|volume.

    Confirmation-gated at the roster level (changes the user's playback).
    Volume takes an integer 0-100 (e.g. ``volume 60``).
    """
    action = (action or "").strip().lower()
    if action in ("next", "previous", "pause", "resume"):
        endpoint = {
            "next": ("/me/player/next", "POST"),
            "previous": ("/me/player/previous", "POST"),
            "pause": ("/me/player/pause", "PUT"),
            "resume": ("/me/player/play", "PUT"),
        }[action]
        _api(endpoint[1], endpoint[0])
        return f"SPOTIFY: {action} — done."
    if action.startswith("volume "):
        try:
            volume = int(action.split(" ", 1)[1])
        except ValueError:
            return "ERROR: volume must be 0-100 (e.g. 'volume 60')."
        _api("PUT", "/me/player/volume", {"volume_percent": max(0, min(100, volume))})
        return f"SPOTIFY: volume set to {max(0, min(100, volume))}%."
    return (
        "ERROR: playback_control action must be one of: "
        "next | previous | pause | resume | volume <0-100>. "
        "Use spotify_play to play specific music."
    )


def play_uri(uri: str) -> str:
    """Start playback of a track/album/playlist URI on an active device."""
    uri = (uri or "").strip()
    if not uri.startswith("spotify:"):
        return "ERROR: play needs a spotify: URI (use spotify_search to find one)."
    data = _api("PUT", "/me/player/play", {"uris": [uri]})
    if data:
        return f"SPOTIFY: error starting playback: {data}."
    return "SPOTIFY: playback started."


def search_tracks(query: str, limit: int = 5) -> str:
    """Search Spotify (tracks first, then albums/artists)."""
    query = (query or "").strip()
    if not query:
        return "ERROR: search needs a query (e.g. 'daft punk')."
    limit = max(1, min(int(limit), 10))
    data = _api(
        "GET", "/search",
        {"q": query, "type": "track,album,artist", "limit": limit},
    )
    rows = []
    tracks = data.get("tracks", {}).get("items", [])
    for t in tracks[:limit]:
        artists = ", ".join(a.get("name", "") for a in (t.get("artists") or [])[:3])
        rows.append(f"- {t.get('name')} — {artists} ({t.get('uri')})")
    if not rows:
        return f"SPOTIFY SEARCH: no tracks matched {query!r}."
    return "SPOTIFY SEARCH RESULTS:\n" + "\n".join(rows)


def top_tracks(time_range: str = "medium_term", limit: int = 5) -> str:
    """The user's most-played tracks (short|medium|long_term)."""
    if time_range not in ("short_term", "medium_term", "long_term"):
        return "ERROR: time_range must be short_term | medium_term | long_term."
    limit = max(1, min(int(limit), 20))
    data = _api(
        "GET", "/me/top/tracks",
        {"time_range": time_range, "limit": limit},
    )
    items = data.get("items", [])
    if not items:
        return "SPOTIFY TOP TRACKS: none yet for that range."
    rows = []
    for i, t in enumerate(items, 1):
        artists = ", ".join(a.get("name", "") for a in (t.get("artists") or [])[:3])
        rows.append(f"{i}. {t.get('name')} — {artists}")
    return f"SPOTIFY TOP TRACKS ({time_range}):\n" + "\n".join(rows)


def recently_played(limit: int = 10) -> str:
    """The user's recently played tracks, newest first."""
    limit = max(1, min(int(limit), 25))
    data = _api("GET", "/me/player/recently-played", {"limit": limit})
    items = data.get("items", [])
    if not items:
        return "SPOTIFY: no recently played tracks found."
    rows = []
    for i, entry in enumerate(items, 1):
        t = entry.get("track") or {}
        artists = ", ".join(a.get("name", "") for a in (t.get("artists") or [])[:3])
        played = (entry.get("played_at") or "")[:16].replace("T", " ")
        rows.append(f"{i}. {t.get('name')} — {artists} ({played})")
    return "SPOTIFY RECENTLY PLAYED:\n" + "\n".join(rows)


def list_playlists(limit: int = 20) -> str:
    """The user's playlists (name + track count)."""
    limit = max(1, min(int(limit), 50))
    data = _api("GET", "/me/playlists", {"limit": limit})
    items = data.get("items", [])
    if not items:
        return "SPOTIFY: no playlists found on this account."
    rows = []
    for p in items:
        rows.append(f"- {p.get('name')} ({p.get('tracks', {}).get('total', 0)} tracks) {p.get('uri')}")
    return "SPOTIFY PLAYLISTS:\n" + "\n".join(rows)


def _fmt_ms(ms: int) -> str:
    total = max(0, int(ms)) // 1000
    return f"{total // 60}:{total % 60:02d}"


# --------------------------------------------------------------------------- #
# Status + CLI
# --------------------------------------------------------------------------- #
def status() -> dict[str, Any]:
    """Honest capability report for connections/SETUP/panel (Rule 2.2)."""
    if not spotify_configured():
        return {
            "configured": False,
            "linked": False,
            "detail": "no SPOTIFY_CLIENT_ID set",
            "hint": "developer.spotify.com -> create app -> set SPOTIFY_CLIENT_ID",
        }
    account = linked_account()
    return {
        "configured": True,
        "linked": account is not None,
        "detail": (
            f"linked as {account}" if account else "client id set, not linked yet"
        ),
        "hint": "run spotify_login (or press [LOGIN] in the panel)",
    }


def _main(argv: list[str] | None = None) -> int:
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    if "--login" in argv:
        print(spotify_login())
        s = status()
        return 0 if s["linked"] else 1
    if "--check" in argv:
        s = status()
        print(
            f"SPOTIFY: {'CONFIGURED ' + s['detail'] if s['configured'] else 'NOT CONFIGURED — ' + s['hint']}"
        )
        print("SETUP: 1) developer.spotify.com -> create app")
        print(f"       2) add redirect {_redirect_uri()} to its Redirect URIs")
        print("       3) set SPOTIFY_CLIENT_ID in .env or dourmouse/local_secrets.py")
        print("       4) run 'python -m dourmouse.spotify_services --login'")
        return 0 if s["linked"] else 1
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
