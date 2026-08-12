"""Unit tests for the Google OAuth machinery (v5.15) — hermetic, no network.

Covers the parts of dourmouse/google_auth.py that a real sign-in depends on:
PKCE construction, honest configuration reporting, the authorization URL
shape, token exchange + id_token verification error surfacing (Rule 2.2),
revocation best-effort, and the per-user AuthStore (users + sessions +
transparent refresh). Network calls are monkeypatched out; only the
deterministic logic is exercised.
"""

import json

import pytest

from dourmouse import google_auth
from dourmouse.google_auth import AuthStore


# -- PKCE ------------------------------------------------------------------ #

class TestPkce:
    def test_code_challenge_deterministic(self):
        assert google_auth.code_challenge("abc") == google_auth.code_challenge("abc")

    def test_code_challenge_differs_per_verifier(self):
        assert google_auth.code_challenge("abc") != google_auth.code_challenge("abd")

    def test_new_pkce_is_high_entropy_and_matching(self):
        a, ca = google_auth.new_pkce()
        b, cb = google_auth.new_pkce()
        assert a != b and ca != cb
        assert google_auth.code_challenge(a) == ca
        # url-safe base64 without padding, 32+ bytes of entropy
        assert len(a) >= 43

    def test_challenge_is_b64url(self):
        v, c = google_auth.new_pkce()
        assert "=" not in c and "+" not in c and "/" not in c


# -- configuration honesty (Rule 2.2) -------------------------------------- #

class TestConfiguration:
    def test_not_configured_without_env(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
        monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
        assert google_auth.google_configured() is False
        payload = google_auth.status()
        assert payload["configured"] is False
        assert "GOOGLE_CLIENT_ID" in payload["hint"]

    def test_configured_only_with_both(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "id-123")
        monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
        assert google_auth.google_configured() is False
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret-456")
        assert google_auth.google_configured() is True
        assert google_auth.status()["configured"] is True

    def test_authorization_url_shape(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "id-123")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "s")
        url = google_auth.authorization_url(
            "http://127.0.0.1:8765/api/auth/google/callback",
            state="st", challenge="ch",
        )
        assert url.startswith(google_auth.GOOGLE_AUTH_URL)
        assert "client_id=id-123" in url
        assert "code_challenge=ch" in url
        assert "code_challenge_method=S256" in url
        assert "access_type=offline" in url
        assert "prompt=consent" in url
        assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A8765%2Fapi%2Fauth%2Fgoogle%2Fcallback" in url


# -- token exchange + verification ----------------------------------------- #

def _fake_urlopen(payload, status_error: str | None = None):
    """A urlopen stand-in returning a parsed-JSON body (or raising)."""
    import urllib.error

    def _open(request, timeout=20.0):
        if status_error is not None:
            raise urllib.error.HTTPError(request.full_url, 400, status_error, None, None)
        class _Resp:
            def read(self):
                return json.dumps(payload).encode()
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False
        return _Resp()

    return _open


class TestExchange:
    def test_exchange_returns_tokens_with_timestamp(self, monkeypatch):
        monkeypatch.setattr(google_auth, "urlopen",
                            _fake_urlopen({"access_token": "at", "refresh_token": "rt",
                                           "id_token": "it", "expires_in": 3600}))
        tokens = google_auth.exchange_code("code", "http://127.0.0.1:8765/cb", "verifier")
        assert tokens["access_token"] == "at"
        assert "_acquired_at" in tokens

    def test_exchange_surfaces_google_error(self, monkeypatch):
        # Rule 2.2: the REAL Google refusal text is passed through.
        monkeypatch.setattr(
            google_auth, "urlopen",
            _fake_urlopen({"error": "invalid_grant",
                           "error_description": "code was already redeemed"}),
        )
        with pytest.raises(RuntimeError, match="invalid_grant"):
            google_auth.exchange_code("code", "http://127.0.0.1:8765/cb", "verifier")

    def test_exchange_surfaces_transport_error(self, monkeypatch):
        monkeypatch.setattr(google_auth, "urlopen",
                            _fake_urlopen({}, status_error="connection reset"))
        with pytest.raises(RuntimeError, match="token exchange failed"):
            google_auth.exchange_code("code", "http://127.0.0.1:8765/cb", "verifier")


class TestVerifyIdToken:
    def test_verified_token_passes(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-1")
        claims = {"email": "User@Example.com", "name": "User",
                  "picture": "p", "sub": "s1", "aud": "client-1",
                  "email_verified": "true"}
        monkeypatch.setattr(google_auth, "urlopen", _fake_urlopen(claims))
        identity = google_auth.verify_id_token("token")
        assert identity["email"] == "user@example.com"  # normalized lower
        assert identity["name"] == "User"

    def test_audience_mismatch_refused(self, monkeypatch):
        # A valid Google token minted for ANOTHER client must not verify.
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-1")
        claims = {"email": "u@example.com", "aud": "some-other-client",
                  "email_verified": "true"}
        monkeypatch.setattr(google_auth, "urlopen", _fake_urlopen(claims))
        with pytest.raises(RuntimeError, match="audience mismatch"):
            google_auth.verify_id_token("token")

    def test_unverified_email_refused(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-1")
        claims = {"email": "u@example.com", "aud": "client-1",
                  "email_verified": "false"}
        monkeypatch.setattr(google_auth, "urlopen", _fake_urlopen(claims))
        with pytest.raises(RuntimeError, match="not verified"):
            google_auth.verify_id_token("token")

    def test_google_rejection_surfaces(self, monkeypatch):
        monkeypatch.setattr(
            google_auth, "urlopen",
            _fake_urlopen({"error": "invalid_token",
                           "error_description": "token expired"}),
        )
        with pytest.raises(RuntimeError, match="expired"):
            google_auth.verify_id_token("bad")


class TestRevoke:
    def test_revoke_never_raises(self, monkeypatch):
        import urllib.error
        def _boom(request, timeout=10.0):
            raise urllib.error.URLError("network down")
        monkeypatch.setattr(google_auth, "urlopen", _boom)
        google_auth.revoke_token("refresh-token")  # must not raise

    def test_revoke_posts_token(self, monkeypatch):
        seen = {}
        def _capture(request, timeout=10.0):
            seen["body"] = request.data.decode()
            class _Resp:
                def __enter__(self):
                    return self
                def __exit__(self, *exc):
                    return False
            return _Resp()
        monkeypatch.setattr(google_auth, "urlopen", _capture)
        google_auth.revoke_token("rt-1")
        assert "token=rt-1" in seen["body"]


# -- per-user AuthStore ---------------------------------------------------- #

class TestAuthStore:
    def test_upsert_and_read_tokens(self, tmp_path):
        store = AuthStore(tmp_path / "auth.db")
        store.upsert_user("Me@Example.com", {"access_token": "at", "refresh_token": "rt"},
                          name="Me", sub="s1")
        tokens = store.user_tokens("me@example.com")  # case-insensitive
        assert tokens["access_token"] == "at"
        assert store.user_profile("me@example.com")["name"] == "Me"
        store.close()

    def test_upsert_updates_in_place(self, tmp_path):
        store = AuthStore(tmp_path / "auth.db")
        store.upsert_user("u@example.com", {"access_token": "old"})
        store.upsert_user("u@example.com", {"access_token": "new"})
        assert store.user_tokens("u@example.com")["access_token"] == "new"
        store.close()

    def test_unknown_user_returns_empty(self, tmp_path):
        store = AuthStore(tmp_path / "auth.db")
        assert store.user_tokens("nobody@example.com") == {}
        assert store.user_profile("nobody@example.com") == {}
        store.close()

    def test_invalid_email_rejected(self, tmp_path):
        store = AuthStore(tmp_path / "auth.db")
        with pytest.raises(ValueError):
            store.upsert_user("not-an-email", {})
        store.close()

    def test_session_lifecycle(self, tmp_path):
        store = AuthStore(tmp_path / "auth.db")
        sid = store.create_session("u@example.com")
        assert store.session_email(sid) == "u@example.com"
        assert store.session_email("bogus") is None
        store.delete_session(sid)
        assert store.session_email(sid) is None
        store.close()

    def test_access_token_returns_valid_directly(self, tmp_path, monkeypatch):
        import time
        from datetime import datetime, timedelta, timezone
        monkeypatch.setattr(google_auth, "client_id", lambda: "c")
        monkeypatch.setattr(google_auth, "client_secret", lambda: "s")
        store = AuthStore(tmp_path / "auth.db")
        tokens = {"access_token": "fresh", "expires_in": 3600,
                  "_acquired_at": datetime.now(timezone.utc).isoformat()}
        store.upsert_user("u@example.com", tokens)
        assert store.access_token_for("u@example.com") == "fresh"
        store.close()

    def test_access_token_refreshes_when_expired(self, tmp_path, monkeypatch):
        from datetime import datetime, timedelta, timezone
        store = AuthStore(tmp_path / "auth.db")
        old = {"access_token": "stale", "refresh_token": "rt", "expires_in": 60,
               "_acquired_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()}
        store.upsert_user("u@example.com", old)
        monkeypatch.setattr(
            google_auth, "refresh_access_token",
            lambda rt: {"access_token": "refreshed", "refresh_token": rt, "expires_in": 3600},
        )
        assert store.access_token_for("u@example.com") == "refreshed"
        # The refreshed token is persisted for the next call.
        assert store.user_tokens("u@example.com")["access_token"] == "refreshed"
        store.close()

    def test_second_account_coexists_independently(self, tmp_path):
        """v5.22.9: two Google accounts on ONE store never see each other's
        data — the "second freebuff account / another gmail" case. Each
        email has its own tokens, and each session resolves to exactly the
        account that created it."""
        store = AuthStore(tmp_path / "auth.db")
        store.upsert_user("first@gmail.com", {"access_token": "tok-A"}, name="First")
        store.upsert_user("second@gmail.com", {"access_token": "tok-B"}, name="Second")
        assert store.user_tokens("first@gmail.com")["access_token"] == "tok-A"
        assert store.user_tokens("second@gmail.com")["access_token"] == "tok-B"
        # Sessions are bound to the account that created them.
        sid_a = store.create_session("first@gmail.com")
        sid_b = store.create_session("second@gmail.com")
        assert store.session_email(sid_a) == "first@gmail.com"
        assert store.session_email(sid_b) == "second@gmail.com"
        # Signing one out never touches the other's session.
        store.delete_session(sid_a)
        assert store.session_email(sid_a) is None
        assert store.session_email(sid_b) == "second@gmail.com"
        store.close()

    def test_expired_without_refresh_returns_none(self, tmp_path, monkeypatch):
        from datetime import datetime, timedelta, timezone
        store = AuthStore(tmp_path / "auth.db")
        old = {"access_token": "stale", "expires_in": 60,
               "_acquired_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()}
        store.upsert_user("u@example.com", old)
        assert store.access_token_for("u@example.com") is None  # honest None
        store.close()
