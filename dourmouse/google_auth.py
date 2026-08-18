"""Google OAuth login + per-user identity (v5.15).

Any person can sign in with their own Google account (Authorization Code +
PKCE, stdlib only) and DourMouse then works on THAT account's linked
services — Gmail, Calendar, and Drive (all read-only except Gmail send).
Each signed-in user gets:

- a **user session** (HttpOnly cookie) that authorizes non-loopback access
  alongside (not instead of) the existing DOURMOUSE_ACCESS_TOKEN gate, and
- their **own OAuth tokens** stored per-user in the AuthStore, so the
  gmail/calendar agent tools act on the LOGGED-IN user's account — never a
  shared server inbox.

Honesty contract (Rule 2.2): with no `GOOGLE_CLIENT_ID` /
`GOOGLE_CLIENT_SECRET` in the environment everything reports NOT CONFIGURED
with the exact setup steps — no fake login button, no fabricated identity.
Identity is verified server-side via Google's tokeninfo endpoint (no local
crypto needed); `email_verified` must be true or the login is refused.

Rule 2.6: secrets come from env only, never logged; per-user tokens are
stored in the AuthStore DB under <workspace>/auth/ (600, WAL).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

#: Google OAuth endpoints (fixed, deterministic).
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"

#: Scopes: identity + the "anything linked to that account" surfaces DourMouse
#: can honestly serve today (inbox read, send, calendar read, Drive read).
#: v5.18 adds drive.readonly — the consent screen lists it once the user
#: grants the drive-scoped sign-in.
#:
#: v5.23: gmail.*/calendar.readonly/drive.readonly are RESTRICTED scopes —
#: Google refuses to show them on an unverified app (consent 500s after the
#: email step). Identity-only (openid/email/profile) works unverified. The
#: restricted surface stays available behind GOOGLE_OAUTH_FULL_SCOPES=1 once
#: the app is verified (or in Testing mode with the account as a test user).
#: v5.29: drive.file added so the full-scope sign-in can CREATE Docs and
#: Slides in the signed-in user's Drive (files the app creates — the minimal
#: write scope; drive.readonly alone 403s drive_create_doc / slides_create).
_IDENTITY_SCOPES = "openid email profile "
_FULL_SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly "
    "https://www.googleapis.com/auth/gmail.send "
    # v8.4: archive / trash / untrash. modify covers label changes and the
    # trash+untrash pair; it does NOT grant permanent deletion, which needs
    # the separate mail.google.com scope and is deliberately not requested.
    "https://www.googleapis.com/auth/gmail.modify "
    "https://www.googleapis.com/auth/calendar.readonly "
    "https://www.googleapis.com/auth/drive.readonly "
    "https://www.googleapis.com/auth/drive.file"
)


def requested_scopes() -> str:
    """The OAuth scope string for the consent URL.

    Identity-only by default (works on an unverified app); the restricted
    Gmail/Calendar/Drive scopes are appended only when the deploy opts in via
    GOOGLE_OAUTH_FULL_SCOPES=1 (app verified, or Testing mode + test user).
    """
    extra = os.environ.get("GOOGLE_OAUTH_FULL_SCOPES", "").strip().lower()
    if extra in ("1", "true", "yes", "on"):
        return _IDENTITY_SCOPES + _FULL_SCOPES
    return _IDENTITY_SCOPES


#: Backwards-compatible module-level reference (tests + status() use it).
SCOPES = requested_scopes()

_SESSION_TTL = timedelta(days=30)
_TOKEN_SKEW = timedelta(seconds=60)

#: Swappable in tests (hermetic HTTP, no network).
urlopen = urllib.request.urlopen


# -- configuration -------------------------------------------------------- #

def client_id() -> str:
    return os.environ.get("GOOGLE_CLIENT_ID", "").strip()


def client_secret() -> str:
    return os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()


def google_configured() -> bool:
    """True only when BOTH OAuth client credentials are set (deterministic)."""
    return bool(client_id()) and bool(client_secret())


def status() -> dict[str, Any]:
    """Honest capability report for /api/auth/status (Rule 2.2)."""
    if google_configured():
        full = requested_scopes() != _IDENTITY_SCOPES
        return {
            "configured": True,
            "detail": f"OAuth client {client_id()[:12]}…",
            "hint": "redirect URIs must be registered in Google Cloud Console",
            "scopes": "full" if full else "identity-only",
        }
    return {
        "configured": False,
        "detail": "no Google OAuth client set",
        "hint": (
            "Google Cloud Console → Credentials → OAuth client (Web) → set "
            "GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET in .env"
        ),
    }


# -- PKCE + authorization URL -------------------------------------------- #

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def code_challenge(verifier: str) -> str:
    """S256 PKCE challenge for a code_verifier (deterministic)."""
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def new_pkce() -> tuple[str, str]:
    """(code_verifier, code_challenge) — high-entropy, one use only."""
    verifier = secrets.token_urlsafe(48)
    return verifier, code_challenge(verifier)


def authorization_url(
    redirect_uri: str,
    state: str,
    challenge: str,
    *,
    access_type: str = "offline",
    prompt: str = "consent",
) -> str:
    """The Google consent URL. ``offline`` + ``consent`` guarantees a
    refresh_token on the first (and every) authorization, so per-user access
    survives the browser closing."""
    params = urllib.parse.urlencode(
        {
            "client_id": client_id(),
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": requested_scopes(),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "access_type": access_type,
            "prompt": prompt,
            "include_granted_scopes": "true",
        }
    )
    return f"{GOOGLE_AUTH_URL}?{params}"


# -- token endpoints ------------------------------------------------------ #

def _post_form(url: str, fields: dict[str, str], timeout: float = 20.0) -> dict[str, Any]:
    body = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def exchange_code(code: str, redirect_uri: str, verifier: str) -> dict[str, Any]:
    """Exchange the authorization code for tokens.

    Raises RuntimeError with the REAL Google message on failure (Rule 2.2) —
    never a fabricated token set.
    """
    fields = {
        "code": code,
        "client_id": client_id(),
        "client_secret": client_secret(),
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }
    try:
        tokens = _post_form(GOOGLE_TOKEN_URL, fields)
    except Exception as exc:  # noqa: BLE001 - surface the real transport error
        raise RuntimeError(f"GOOGLE AUTH: token exchange failed: {exc}") from exc
    if "error" in tokens:
        raise RuntimeError(
            f"GOOGLE AUTH: token exchange refused ({tokens.get('error')}: "
            f"{tokens.get('error_description', '')})"
        )
    tokens["_acquired_at"] = datetime.now(timezone.utc).isoformat()
    return tokens


def refresh_access_token(refresh_token: str) -> dict[str, Any]:
    """Exchange a refresh_token for a fresh access_token (per-user tokens
    expire hourly; refreshes are transparent to the tools)."""
    fields = {
        "refresh_token": refresh_token,
        "client_id": client_id(),
        "client_secret": client_secret(),
        "grant_type": "refresh_token",
    }
    try:
        tokens = _post_form(GOOGLE_TOKEN_URL, fields)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"GOOGLE AUTH: token refresh failed: {exc}") from exc
    if "error" in tokens:
        raise RuntimeError(
            f"GOOGLE AUTH: token refresh refused ({tokens.get('error')}: "
            f"{tokens.get('error_description', '')})"
        )
    tokens["_acquired_at"] = datetime.now(timezone.utc).isoformat()
    return tokens


def verify_id_token(id_token: str) -> dict[str, Any]:
    """Verify a Google id_token server-side via the tokeninfo endpoint.

    Returns the identity claims {email, name, picture, sub, email_verified}.
    A token Google rejects, or one without a verified email, raises
    RuntimeError — the login is refused, never half-trusted.
    """
    url = f"{GOOGLE_TOKENINFO_URL}?id_token={urllib.parse.quote(id_token)}"
    try:
        with urlopen(url, timeout=20.0) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        claims = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 - surface the real verification error
        raise RuntimeError(f"GOOGLE AUTH: id_token verification failed: {exc}") from exc
    if "error" in claims or not claims.get("email"):
        raise RuntimeError(
            f"GOOGLE AUTH: id_token rejected ({claims.get('error_description', claims.get('error', 'invalid token'))})"
        )
    # Defense in depth: tokeninfo does not bind the audience to our client,
    # so a valid Google-issued token for ANY client would otherwise verify.
    # The token was minted for our redirect, so its aud MUST be our client id.
    if str(claims.get("aud") or "") != client_id():
        raise RuntimeError(
            f"GOOGLE AUTH: id_token audience mismatch ({claims.get('aud')!r} "
            f"!= {client_id()!r}) — login refused"
        )
    if str(claims.get("email_verified", "false")).lower() != "true":
        raise RuntimeError(f"GOOGLE AUTH: email not verified for {claims.get('email')}")
    return {
        "email": str(claims["email"]).lower(),
        "name": str(claims.get("name") or ""),
        "picture": str(claims.get("picture") or ""),
        "sub": str(claims.get("sub") or ""),
    }


def revoke_token(refresh_token: str) -> None:
    """Best-effort revoke on logout (never raises — logout must not fail)."""
    try:
        body = urllib.parse.urlencode({"token": refresh_token}).encode("utf-8")
        request = urllib.request.Request(
            GOOGLE_REVOKE_URL, data=body, method="POST"
        )
        with urlopen(request, timeout=10.0):
            pass
    except Exception:  # noqa: BLE001,S110 - best-effort
        pass


# -- per-user token helpers ---------------------------------------------- #

def _token_expiry(tokens: dict[str, Any]) -> datetime:
    acquired = tokens.get("_acquired_at")
    if not acquired:
        return datetime.now(timezone.utc) - timedelta(seconds=1)
    try:
        start = datetime.fromisoformat(acquired)
    except ValueError:
        return datetime.now(timezone.utc) - timedelta(seconds=1)
    return start + timedelta(seconds=int(tokens.get("expires_in", 3600))) - _TOKEN_SKEW


# -- current-user (request-scoped) --------------------------------------- #

_thread = threading.local()

#: The AuthStore the running server mounted (bound by run_server). The agent
#: tools resolve per-user tokens through this so they always act on the real
#: store, not a throwaway.
_auth_store: Any | None = None


def bind_auth_store(store: Any | None) -> None:
    """Attach the server's AuthStore (called by run_server at mount time)."""
    global _auth_store
    _auth_store = store


def auth_store() -> Any | None:
    return _auth_store


def set_current_user(email: str | None) -> None:
    """Bind the active session user for THIS request thread (set by the web
    server around /api/chat so the agent tools read the right account)."""
    _thread.email = email


def current_user() -> str | None:
    return getattr(_thread, "email", None)


# -- AuthStore ----------------------------------------------------------- #

def default_auth_store() -> "AuthStore":
    """The persistent store the real serving path mounts (workspace/auth)."""
    raw = os.environ.get("DOURMOUSE_WORKSPACE")
    root = Path(raw).expanduser() if raw else Path(__file__).resolve().parent.parent / "workspace"
    return AuthStore(root / "auth" / "dourmouse_auth.db")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AuthStore:
    """SQLite store of Google-linked users and their login sessions.

    One store, per-user rows (``users.email`` primary key holds the OAuth
    tokens JSON) + a sessions table (sid -> identity). Thread-safe via
    per-operation connections (file mode) or one shared connection
    (in-memory, tests) — the same pattern as the cross-device StateStore.
    """

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self.path = Path(path) if path is not None else None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._db_path = str(self.path)
            self._conn: sqlite3.Connection | None = None
        else:
            self._db_path = ":memory:"
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._lock = threading.Lock()
        self._closed = False
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError("auth store is closed")
        if self._conn is not None:
            self._conn.row_factory = sqlite3.Row
            return self._conn
        connection = sqlite3.connect(self._db_path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS users ("
                " email TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',"
                " picture TEXT NOT NULL DEFAULT '', sub TEXT NOT NULL DEFAULT '',"
                " tokens TEXT NOT NULL DEFAULT '{}', created TEXT NOT NULL,"
                " updated TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS sessions ("
                " sid TEXT PRIMARY KEY, email TEXT NOT NULL, created TEXT NOT NULL)"
            )

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
                self._closed = True
                return
            if self.path is not None:
                try:
                    with self._connect() as connection:
                        connection.execute("PRAGMA wal_checkpoint(FULL)")
                except sqlite3.Error:
                    pass
            self._closed = True

    # -- users ----------------------------------------------------------- #

    def upsert_user(
        self,
        email: str,
        tokens: dict[str, Any],
        name: str = "",
        picture: str = "",
        sub: str = "",
    ) -> None:
        email = (email or "").strip().lower()
        if not email or "@" not in email:
            raise ValueError("auth: invalid email")
        now = _now()
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO users (email, name, picture, sub, tokens, created, updated)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(email) DO UPDATE SET name=excluded.name,"
                " picture=excluded.picture, sub=excluded.sub,"
                " tokens=excluded.tokens, updated=excluded.updated",
                (email, name[:120], picture[:400], sub[:120], json.dumps(tokens), now, now),
            )

    def user_tokens(self, email: str) -> dict[str, Any]:
        email = (email or "").strip().lower()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT tokens FROM users WHERE email=?", (email,)
            ).fetchone()
        if row is None:
            return {}
        try:
            parsed = json.loads(row["tokens"] or "{}")
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def user_profile(self, email: str) -> dict[str, Any]:
        email = (email or "").strip().lower()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT email, name, picture FROM users WHERE email=?", (email,)
            ).fetchone()
        if row is None:
            return {}
        return {"email": row["email"], "name": row["name"], "picture": row["picture"]}

    def access_token_for(self, email: str) -> str | None:
        """The current valid access token for a user, refreshing transparently
        when near expiry. Returns None honestly when there is no token."""
        email = (email or "").strip().lower()
        if not email:
            return None
        tokens = self.user_tokens(email)
        if not tokens.get("access_token"):
            return None
        if datetime.now(timezone.utc) < _token_expiry(tokens):
            return str(tokens["access_token"])
        refresh = tokens.get("refresh_token")
        if not refresh:
            return None  # expired and unrefresheable -> honest None
        try:
            fresh = refresh_access_token(str(refresh))
        except RuntimeError:
            return None  # real failure surfaced by the caller's fallback path
        merged = {**tokens, **fresh}
        self.upsert_user(email, merged)
        return str(merged.get("access_token") or "")

    # -- sessions -------------------------------------------------------- #

    def create_session(
        self, email: str, name: str = "", picture: str = ""
    ) -> str:
        sid = secrets.token_urlsafe(32)
        with self._lock, self._connect() as connection:
            # prune expired sessions opportunistically
            cutoff = (datetime.now(timezone.utc) - _SESSION_TTL).isoformat(timespec="seconds")
            connection.execute("DELETE FROM sessions WHERE created < ?", (cutoff,))
            connection.execute(
                "INSERT INTO sessions (sid, email, created) VALUES (?, ?, ?)",
                (sid, email.lower(), _now()),
            )
        return sid

    def session_email(self, sid: str) -> str | None:
        sid = (sid or "").strip()
        if not sid:
            return None
        cutoff = (datetime.now(timezone.utc) - _SESSION_TTL).isoformat(timespec="seconds")
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT email FROM sessions WHERE sid=? AND created >= ?",
                (sid, cutoff),
            ).fetchone()
        return row["email"] if row is not None else None

    def delete_session(self, sid: str) -> None:
        sid = (sid or "").strip()
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM sessions WHERE sid=?", (sid,))
