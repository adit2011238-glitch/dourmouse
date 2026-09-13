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
    @staticmethod
    def _remove_builtin_module(monkeypatch):
        # These tests predate the builtin-shared-OAuth-client fallback
        # (client_id()/client_secret() = env or _builtin_oauth()[0/1]) and
        # mean to test the ENV-only path in isolation. Real bug this
        # closes: on a checkout where dourmouse/_builtin_oauth.py
        # genuinely exists (true here since Phase G), these two tests
        # started intermittently failing depending on test ORDER —
        # whichever test ran first and legitimately imported the real
        # builtin module left it cached as an attribute on the `dourmouse`
        # package object for the rest of the process, so "no env" no
        # longer meant "unconfigured" the way these tests assumed. See
        # TestBuiltinOauthFallback._install_fake_builtin_module's own
        # comment for the exact `from package import submodule` lookup
        # path responsible.
        import sys

        import dourmouse

        monkeypatch.setitem(sys.modules, "dourmouse._builtin_oauth", None)
        monkeypatch.delattr(dourmouse, "_builtin_oauth", raising=False)

    def test_not_configured_without_env(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
        monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
        self._remove_builtin_module(monkeypatch)
        assert google_auth.google_configured() is False
        payload = google_auth.status()
        assert payload["configured"] is False
        assert "GOOGLE_CLIENT_ID" in payload["hint"]

    def test_configured_only_with_both(self, monkeypatch):
        self._remove_builtin_module(monkeypatch)
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


class TestBuiltinOauthFallback:
    """v14 (user-directed, 2026-09-12): "commercial, for other people to
    use" — the ONE shared Dourmouse OAuth client, read from a gitignored
    sibling module (dourmouse/_builtin_oauth.py, see google_auth.py's
    own comment on _builtin_oauth() for why it's never committed) so an
    end user needs zero Google Cloud Console setup. A plain env var
    still wins when set — a power user's own client always overrides
    the shared one."""

    def _install_fake_builtin_module(self, monkeypatch, client_id: str, client_secret: str):
        import sys
        import types

        import dourmouse

        fake = types.ModuleType("dourmouse._builtin_oauth")
        fake.GOOGLE_CLIENT_ID = client_id
        fake.GOOGLE_CLIENT_SECRET = client_secret
        monkeypatch.setitem(sys.modules, "dourmouse._builtin_oauth", fake)
        # Real bug this closes (live-caught running the FULL suite, not
        # this file alone — it passed in isolation and only failed as
        # part of the whole run): `from dourmouse import _builtin_oauth
        # as mod` resolves via getattr(sys.modules["dourmouse"],
        # "_builtin_oauth") FIRST — Python sets that attribute as a side
        # effect the first time the submodule is ever really imported,
        # and a plain `monkeypatch.setitem(sys.modules, ...)` never
        # touches it. Once a REAL dourmouse/_builtin_oauth.py exists on
        # disk (true for this checkout since Phase G), some earlier test
        # in the full run legitimately imports it for real, caches the
        # REAL module as this attribute, and every later test in the
        # same process silently keeps seeing the REAL credentials no
        # matter what this method installs into sys.modules. Patching
        # the attribute directly closes the actual lookup path.
        monkeypatch.setattr(dourmouse, "_builtin_oauth", fake, raising=False)

    def _remove_builtin_module(self, monkeypatch):
        """Simulate NO dourmouse/_builtin_oauth.py existing at all,
        regardless of whether the real one has already been imported
        earlier in this same test process (see the comment on
        _install_fake_builtin_module above for why both halves matter).
        """
        import sys

        import dourmouse

        monkeypatch.setitem(sys.modules, "dourmouse._builtin_oauth", None)
        monkeypatch.delattr(dourmouse, "_builtin_oauth", raising=False)

    def test_falls_back_to_the_builtin_module_when_no_env_is_set(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
        monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
        self._install_fake_builtin_module(monkeypatch, "builtin-id", "builtin-secret")
        assert google_auth.client_id() == "builtin-id"
        assert google_auth.client_secret() == "builtin-secret"
        assert google_auth.google_configured() is True

    def test_env_var_wins_over_the_builtin_module(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "power-user-id")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "power-user-secret")
        self._install_fake_builtin_module(monkeypatch, "builtin-id", "builtin-secret")
        assert google_auth.client_id() == "power-user-id"
        assert google_auth.client_secret() == "power-user-secret"

    def test_missing_builtin_module_is_honestly_empty_not_a_crash(self, monkeypatch):
        """A checkout with no dourmouse/_builtin_oauth.py at all (it's
        gitignored, template-only) must import-fail cleanly, not raise up
        into a real request. Explicitly simulated (this checkout may or
        may not actually have the real, gitignored file on disk) so the
        test is deterministic either way — see _remove_builtin_module."""
        monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
        monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
        self._remove_builtin_module(monkeypatch)
        assert google_auth.client_id() == ""
        assert google_auth.client_secret() == ""
        assert google_auth.google_configured() is False


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

    def test_a_session_created_today_is_still_valid_years_from_now(self, tmp_path, monkeypatch):
        """v13.10 ("stay logged in forever" feature, explicit user request):
        _SESSION_TTL was extended from 30 days to 3650 days (10 years) so a
        real desktop app's sign-in survives being quit and reopened
        indefinitely for all practical purposes, without a literal
        unbounded/no-expiry special case anywhere in the date arithmetic."""
        import datetime as _dt

        store = AuthStore(tmp_path / "auth.db")
        sid = store.create_session("u@example.com")
        assert store.session_email(sid) == "u@example.com"

        real_datetime = google_auth.datetime

        class _FutureDatetime(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return real_datetime.now(tz) + _dt.timedelta(days=365 * 3)

        monkeypatch.setattr(google_auth, "datetime", _FutureDatetime)
        assert store.session_email(sid) == "u@example.com"
        store.close()


class TestSessionTtlSeconds:
    def test_matches_the_real_configured_ttl(self):
        assert google_auth.session_ttl_seconds() == int(
            google_auth._SESSION_TTL.total_seconds()
        )

    def test_is_a_real_long_lived_duration_not_the_old_thirty_days(self):
        """Guards against silently reverting to the old 30-day (2,592,000s)
        behavior this feature explicitly replaced."""
        thirty_days_seconds = 30 * 24 * 60 * 60
        assert google_auth.session_ttl_seconds() > thirty_days_seconds

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


class TestIdentityResolvesOutsideARequestThread:
    """Regression tests for "it said it couldn't access my Google Drive".

    Three separate real bugs, none of them auth: the stored token already
    carried drive.readonly, drive.file, gmail and calendar scopes plus a
    working refresh token the whole time.
    """

    def test_single_stored_user_resolves_without_a_thread_local(self, tmp_path, monkeypatch):
        """Agent turns, scheduled jobs and the MCP bridge all run outside an
        HTTP request thread, so the thread-local is never set and every
        Google tool reported NOT CONFIGURED."""
        from dourmouse import google_auth

        store = google_auth.AuthStore(tmp_path / "auth.db")
        store.upsert_user("solo@example.com", {"access_token": "t"})
        monkeypatch.setattr(google_auth, "default_auth_store", lambda: store)
        monkeypatch.setattr(google_auth, "_auth_store", None, raising=False)
        google_auth.set_current_user(None)

        assert google_auth.current_user() == "solo@example.com"

    def test_two_stored_users_stay_ambiguous_rather_than_guessing(self, tmp_path, monkeypatch):
        """Guessing which account an agent meant would be a real cross-account
        privacy failure, so more than one linked identity must fall back to
        None and let the caller report NOT CONFIGURED honestly."""
        from dourmouse import google_auth

        store = google_auth.AuthStore(tmp_path / "auth.db")
        store.upsert_user("a@example.com", {"access_token": "t"})
        store.upsert_user("b@example.com", {"access_token": "t"})
        monkeypatch.setattr(google_auth, "default_auth_store", lambda: store)
        monkeypatch.setattr(google_auth, "_auth_store", None, raising=False)
        google_auth.set_current_user(None)

        assert google_auth.current_user() is None

    def test_thread_local_still_wins_over_the_stored_fallback(self, tmp_path, monkeypatch):
        """A request thread serving a known session must never be overridden."""
        from dourmouse import google_auth

        store = google_auth.AuthStore(tmp_path / "auth.db")
        store.upsert_user("stored@example.com", {"access_token": "t"})
        monkeypatch.setattr(google_auth, "default_auth_store", lambda: store)
        monkeypatch.setattr(google_auth, "_auth_store", None, raising=False)
        google_auth.set_current_user("request@example.com")
        try:
            assert google_auth.current_user() == "request@example.com"
        finally:
            google_auth.set_current_user(None)

    def test_auth_store_falls_back_when_nothing_is_mounted(self, tmp_path, monkeypatch):
        """Only the serving process mounts a store. Everything else saw None
        and reported "your Google session is missing or expired"."""
        from dourmouse import google_auth

        store = google_auth.AuthStore(tmp_path / "auth.db")
        monkeypatch.setattr(google_auth, "default_auth_store", lambda: store)
        monkeypatch.setattr(google_auth, "_auth_store", None, raising=False)

        assert google_auth.auth_store() is store


class TestDriveToolsReachTheAgentThatGetsDriveQuestions:
    def test_docs_agent_can_actually_search_drive(self):
        """The planner routes a Drive question to `docs` -- that is the agent
        whose description is Sheets, Drive and Slides -- but drive_search and
        drive_read lived only on `mail`, beside the Gmail tools they share
        OAuth plumbing with. So `docs` genuinely had no way to search Drive
        and honestly said so, one agent away from the capability.
        """
        from dourmouse.general_roster import build_general_registry

        registry = build_general_registry()
        docs = registry.get_subagent("docs")
        assert docs is not None
        names = {t.name for t in docs.tools}
        assert "drive_search" in names
        assert "drive_read" in names
