"""FS1 A-3: a token refresh must not wipe the user's name, picture and sub."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dourmouse import google_auth
from dourmouse.google_auth import AuthStore


def test_refresh_keeps_the_profile(tmp_path, monkeypatch):
    store = AuthStore(tmp_path / "auth.db")
    expired = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    store.upsert_user(
        "ann@example.com",
        {"access_token": "old", "refresh_token": "r1", "expires_at": expired},
        name="Ann", picture="https://pic", sub="12345",
    )
    monkeypatch.setattr(
        google_auth, "refresh_access_token",
        lambda refresh: {"access_token": "new", "expires_in": 3600},
    )
    assert store.access_token_for("ann@example.com") == "new"
    profile = store.user_profile("ann@example.com")
    assert profile["name"] == "Ann"
    assert profile["picture"] == "https://pic"
    tokens = store.user_tokens("ann@example.com")
    assert tokens["access_token"] == "new" and tokens["refresh_token"] == "r1"
    with store._lock, store._connect() as conn:
        assert conn.execute("SELECT sub FROM users WHERE email=?", ("ann@example.com",)).fetchone()[0] == "12345"
