"""Real process-restart persistence for AuthStore.

Every existing test in test_persistent_login.py and test_google_auth.py
creates exactly ONE AuthStore(tmp_path/'auth.db') instance and reads it back
with that SAME object -- none of them proves data survives an actual app
restart (quit -> new process -> a fresh AuthStore instance opened against
the SAME on-disk sqlite file). A single long-lived Python object reading its
own writes proves nothing about on-disk durability: WAL data sitting
uncheckpointed, or a stale in-memory fallback, would pass every existing
test and still lose the user's login on a real quit/relaunch.

This file simulates that restart directly: instance A writes a user +
session and is explicitly closed (as the real app does on quit), then a
SECOND, independent AuthStore B opens the identical path (as the real app
does on the next launch) and must resolve what A wrote.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dourmouse.google_auth import AuthStore, session_ttl_seconds


class TestRestartSurvival:
    def test_second_instance_resolves_session_written_by_first(self, tmp_path):
        db_path = tmp_path / "auth.db"

        # -- "process 1": user logs in, session gets created, app quits -- #
        store_a = AuthStore(db_path)
        store_a.upsert_user(
            "restart@example.com",
            {"access_token": "tok-restart"},
            name="Restart User",
        )
        sid = store_a.create_session("restart@example.com", name="Restart User")
        store_a.close()

        # -- "process 2": fresh instance, same on-disk file, no shared state -- #
        store_b = AuthStore(db_path)
        try:
            assert store_b.session_email(sid) == "restart@example.com"
            assert store_b.user_tokens("restart@example.com")["access_token"] == "tok-restart"
            assert store_b.user_profile("restart@example.com")["name"] == "Restart User"
        finally:
            store_b.close()

    def test_expiry_still_holds_from_the_second_instance(self, tmp_path, monkeypatch):
        """A session already past session_ttl_seconds() when A wrote it must
        still read back as expired from B -- the TTL check is a property of
        the stored `created` timestamp, not of which Python object is asking."""
        db_path = tmp_path / "auth.db"

        store_a = AuthStore(db_path)
        store_a.upsert_user("stale@example.com", {"access_token": "t"})
        sid = store_a.create_session("stale@example.com")
        # Backdate the session's `created` column past the real TTL directly
        # in the sessions table -- create_session() always stamps "now".
        too_old = (
            datetime.now(timezone.utc)
            - timedelta(seconds=session_ttl_seconds() + 3600)
        ).isoformat(timespec="seconds")
        with store_a._lock, store_a._connect() as connection:
            connection.execute(
                "UPDATE sessions SET created=? WHERE sid=?", (too_old, sid)
            )
        store_a.close()

        store_b = AuthStore(db_path)
        try:
            assert store_b.session_email(sid) is None  # expired, honestly so
        finally:
            store_b.close()

    def test_fresh_session_survives_restart_within_ttl(self, tmp_path):
        """Sanity companion to the expiry test: a session well within TTL
        (e.g. the real 10-year "stay logged in forever" window) resolves
        correctly from a second instance, proving the restart path isn't
        just accidentally always expiring/always passing."""
        db_path = tmp_path / "auth.db"
        assert session_ttl_seconds() > 60 * 60 * 24 * 365  # sanity: multi-year TTL

        store_a = AuthStore(db_path)
        store_a.upsert_user("fresh@example.com", {"access_token": "t"})
        sid = store_a.create_session("fresh@example.com")
        store_a.close()

        store_b = AuthStore(db_path)
        try:
            assert store_b.session_email(sid) == "fresh@example.com"
        finally:
            store_b.close()
