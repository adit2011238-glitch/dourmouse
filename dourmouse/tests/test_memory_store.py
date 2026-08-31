"""Long-term memory store tests (Phase A1: SQLite FTS5 retrieval).

Exercises the REAL MemoryStore (dourmouse/memory_store.py) against tmp
databases — upsert semantics, FTS5-ranked search, ingestion from session
ledgers and vaults — plus the remember/recall TOOL wiring through the real
memory subagent (Rule 2.2: honest NOT CONFIGURED when FTS5 is unavailable).
"""

from __future__ import annotations

import json

import pytest

from dourmouse.general_roster import (
    _recall_tool,
    _remember_tool,
    build_general_registry,
)
from dourmouse.memory_store import (
    MemoryStore,
    MemoryStoreUnavailable,
    RemoteMemoryStore,
    RemoteMemoryStoreUnavailable,
    _fts_query,
)


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "mem" / "test.db")
    yield s
    s.close()


# --------------------------------------------------------------------------- #
# Store unit tests
# --------------------------------------------------------------------------- #

class TestMemoryStore:
    def test_remember_and_count(self, store):
        store.remember("agent", "meeting", "sync is Tuesdays at 10am")
        store.remember("agent", "prefs", "prefers dark mode")
        assert store.count() == 2

    def test_upsert_updates_in_place(self, store):
        store.remember("agent", "key", "value one")
        store.remember("agent", "key", "value two")
        assert store.count() == 1  # same (source, title) upserts
        hits = store.search("value two")
        assert hits and hits[0]["title"] == "key"
        # Old body gone — asserted directly (FTS is OR-matched, so a stale-
        # body term alone no longer proves absence; the row itself does).
        assert store.get("agent", "key")["body"] == "value two"

    def test_search_ranks_relevant_over_noise(self, store):
        store.remember("agent", "a", "the nvidia gpu launch was delayed")
        store.remember("agent", "b", "grocery list milk eggs")
        hits = store.search("nvidia gpu", limit=5)
        assert len(hits) == 1
        assert hits[0]["title"] == "a"
        assert "nvidia" in hits[0]["snippet"].lower()

    def test_search_returns_sources_and_snippets(self, store):
        store.remember("vault", "notes/alpha.md", "ATLAS research on quantum")
        hits = store.search("quantum")
        assert hits[0]["source"] == "vault"
        assert hits[0]["title"] == "notes/alpha.md"
        assert "quantum" in hits[0]["snippet"]

    def test_search_empty_or_nonexistent(self, store):
        assert store.search("") == []
        assert store.search("zzz-nonexistent-qq") == []

    def test_search_limit_clamped(self, store):
        for i in range(5):
            store.remember("agent", f"t{i}", f"common word number {i}")
        assert len(store.search("common", limit=3)) == 3
        # Prove the hard 50-cap: 51 matching rows still return only 50.
        for i in range(51):
            store.remember("agent", f"bulk{i}", f"bulkterm value {i}")
        assert len(store.search("bulkterm", limit=100)) == 50

    def test_delete_removes(self, store):
        store.remember("agent", "gone", "delete me please")
        assert store.delete("agent", "gone") is True
        assert store.delete("agent", "gone") is False
        assert store.search("delete me") == []
        assert store.count() == 0

    def test_remember_requires_title_and_body(self, store):
        with pytest.raises(ValueError):
            store.remember("agent", "", "body")
        with pytest.raises(ValueError):
            store.remember("agent", "title", "   ")

    def test_ingest_session_file(self, store, tmp_path):
        session = tmp_path / "sessions" / "s1.jsonl"
        session.parent.mkdir()
        session.write_text(
            "\n".join(
                [
                    json.dumps({"turn": 1, "user": "remember the budget is 42", "final_text": "Noted."}),
                    json.dumps({"turn": 2, "user": "what else", "final_text": "That is all."}),
                ]
            )
            + "\n"
        )
        added = store.ingest_session_file(session)
        assert added == 2
        hits = store.search("budget is 42")
        assert hits and hits[0]["source"] == "session:s1"

    def test_ingest_vault(self, store, tmp_path):
        vault = tmp_path / "vault"
        (vault / "sub").mkdir(parents=True)
        (vault / "alpha.md").write_text("meeting notes about the atlas rollout")
        (vault / "sub" / "beta.md").write_text("unrelated")
        added = store.ingest_vault(vault)
        assert added == 2
        hits = store.search("atlas rollout")
        assert hits and hits[0]["source"] == "vault"
        assert hits[0]["title"] == "alpha.md"

    def test_fts_query_escapes_user_input(self):
        # A bare MATCH string with FTS5 syntax must NOT inject query grammar.
        assert _fts_query("sneaky \" OR *") == '"sneaky" OR "OR"'
        assert _fts_query("  ") == ""
        assert _fts_query("nvidia gpu") == '"nvidia" OR "gpu"'

    def test_fts_query_multi_term_recall(self):
        # OR semantics: bm25 ranks rows matching MORE terms first, so a
        # single matching term still surfaces a hit instead of the all-or-
        # nothing AND behavior that returned zero hits for most recall
        # queries (e.g. distilled "economics franchise context project").
        assert _fts_query("economics franchise context project") == (
            '"economics" OR "franchise" OR "context" OR "project"'
        )


# --------------------------------------------------------------------------- #
# Tool wiring through the real memory subagent
# --------------------------------------------------------------------------- #

class TestMemoryTools:
    def test_remember_tool_stores_real_fact(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_MEMORY_DB", str(tmp_path / "mem.db"))
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
        result = _remember_tool(
            {"source": "agent", "title": "plan", "body": "Phase A first"}
        )
        assert "MEMORY STORED" in result
        assert "[agent] plan" in result

    def test_recall_tool_finds_remembered_fact(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_MEMORY_DB", str(tmp_path / "mem.db"))
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
        _remember_tool({"title": "budget", "body": "the cap is fifty dollars"})
        result = _recall_tool({"query": "cap fifty"})
        assert "MEMORY RECALL RESULTS" in result
        assert "budget" in result

    def test_recall_no_match_is_honest(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_MEMORY_DB", str(tmp_path / "mem.db"))
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
        result = _recall_tool({"query": "zzz-nothing-here"})
        assert "no matches" in result

    def test_remember_missing_fields_errors(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_MEMORY_DB", str(tmp_path / "mem.db"))
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
        result = _remember_tool({"title": "", "body": "x"})
        assert "ERROR" in result

    def test_tools_registered_on_memory_subagent(self):
        registry = build_general_registry()
        names = {t.name for t in registry.get_subagent("memory").tools}
        assert {"remember", "recall", "search_vault", "read_note", "write_note"} <= names


# --------------------------------------------------------------------------- #
# Honest degradation when FTS5 is unavailable
# --------------------------------------------------------------------------- #

class TestMemoryStoreUnavailable:
    def test_unavailable_raises_and_tool_reports_not_configured(self, tmp_path, monkeypatch):
        """Simulate a Python build without FTS5: the store must fail loudly
        and the TOOL must report NOT CONFIGURED — never a silent fake."""

        import sqlite3
        import dourmouse.memory_store as ms

        # sqlite3.Connection does not allow attribute assignment, so patch the
        # schema init to fail exactly the way a missing-FTS5 build does.
        def _no_fts5(self):
            raise sqlite3.OperationalError("no such module: fts5")

        monkeypatch.setattr(ms.MemoryStore, "_init_schema", _no_fts5)
        monkeypatch.setenv("DOURMOUSE_MEMORY_DB", str(tmp_path / "nofs.db"))
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
        with pytest.raises(MemoryStoreUnavailable, match="FTS5 is not available"):
            MemoryStore(tmp_path / "nofs.db")
        result = _remember_tool({"title": "t", "body": "b"})
        assert "NOT CONFIGURED" in result


class TestConcurrentProcessSafety:
    """Real bug found live (2026-08-31): the shared RAG database is now
    genuinely opened by multiple separate PROCESSES at once (the live
    webui server plus a long-running dourmouse/bulk_ingest.py scan) — a
    manual upload arriving mid-scan raised MemoryStoreUnavailable at the
    FTS5 schema probe itself. WAL mode + a real busy_timeout fix real
    multi-process contention; these tests verify the mechanism is
    actually enabled, and that two real, separate connections to the same
    file can both write without either raising "database is locked"."""

    def test_wal_mode_and_busy_timeout_are_actually_set(self, tmp_path):
        store = MemoryStore(tmp_path / "wal_test.db")
        mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
        timeout_ms = store._conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert mode.lower() == "wal"
        assert timeout_ms >= 30000
        store.close()

    def test_two_real_separate_connections_can_both_write(self, tmp_path):
        # Two genuinely independent MemoryStore instances (own sqlite3
        # connection each) against the SAME file -- the real shape of the
        # live server + bulk_ingest.py collision, without needing an
        # actual second OS process to reproduce it.
        db_path = tmp_path / "shared.db"
        store_a = MemoryStore(db_path)
        store_b = MemoryStore(db_path)
        try:
            store_a.remember("proc_a", "note one", "content from process a")
            store_b.remember("proc_b", "note two", "content from process b")
            hits_a = store_a.search("content", limit=10)
            hits_b = store_b.search("content", limit=10)
            assert len(hits_a) == 2
            assert len(hits_b) == 2
        finally:
            store_a.close()
            store_b.close()


class TestRemoteMemoryStore:
    """Real HTTP round trip against a real dourmouse.webui server — the
    remote-RAG contract behind "move the actual rag to [another machine]"
    (2026-08-31). RemoteMemoryStore never opens the SQLite file itself;
    every call is a real request to the OTHER machine's own
    /api/memory/search and /api/memory/remember, which run the exact same
    MemoryStore that machine already uses locally."""

    @pytest.fixture
    def server(self, tmp_path):
        import threading

        from dourmouse.tests.test_webui import _echo_registry
        from dourmouse.webui import run_server

        srv = run_server(_echo_registry(), port=0, client=None, config=None,
                          memory=MemoryStore(tmp_path / "remote_side.db"))
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        port = srv.server_address[1]
        yield port
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)

    def test_remember_and_search_round_trip_over_real_http(self, server):
        port = server
        remote = RemoteMemoryStore(f"http://127.0.0.1:{port}")
        result = remote.remember("laptop_file", "notes.txt", "the quick brown fox")
        assert "MEMORY STORED" in result
        hits = remote.search("fox", limit=5)
        assert any(h["title"] == "notes.txt" for h in hits)
        remote.close()

    def test_bearer_token_is_actually_sent_when_configured(self, server, monkeypatch):
        """Real gap found before this ever ran cross-machine: a remote
        machine with DOURMOUSE_ACCESS_TOKEN set (the desktop deployment
        already has one) rejects any non-loopback caller with no Bearer
        header (webui.py's _authorized()). A same-host test server can't
        exercise the REJECTION path (127.0.0.1 is always loopback-
        exempt, by design), so this checks the request the client
        actually SENDS instead — the real fix, not an end-to-end replay
        of something the test harness can't reproduce honestly."""
        import urllib.request

        seen_auth = {}
        real_urlopen = urllib.request.urlopen

        def spy(req, *a, **k):
            seen_auth["value"] = req.get_header("Authorization")
            return real_urlopen(req, *a, **k)

        monkeypatch.setattr(urllib.request, "urlopen", spy)
        port = server
        remote = RemoteMemoryStore(f"http://127.0.0.1:{port}", token="secret-token-123")
        remote.count()
        assert seen_auth["value"] == "Bearer secret-token-123"

    def test_no_auth_header_sent_when_no_token_configured(self, server, monkeypatch):
        import urllib.request

        seen_auth = {}
        real_urlopen = urllib.request.urlopen

        def spy(req, *a, **k):
            seen_auth["value"] = req.get_header("Authorization")
            return real_urlopen(req, *a, **k)

        monkeypatch.setattr(urllib.request, "urlopen", spy)
        port = server
        remote = RemoteMemoryStore(f"http://127.0.0.1:{port}")
        remote.count()
        assert seen_auth["value"] is None

    def test_count_reflects_real_remote_state(self, server):
        port = server
        remote = RemoteMemoryStore(f"http://127.0.0.1:{port}")
        assert remote.count() == 0
        remote.remember("src", "t1", "body one")
        remote.remember("src", "t2", "body two")
        assert remote.count() == 2

    def test_remember_requires_title_and_body_same_as_local_store(self, server):
        port = server
        remote = RemoteMemoryStore(f"http://127.0.0.1:{port}")
        with pytest.raises(ValueError):
            remote.remember("src", "", "")

    def test_unreachable_server_raises_the_real_honest_error(self):
        # An address nothing is listening on -- a genuinely unreachable
        # remote, not a mocked failure.
        remote = RemoteMemoryStore("http://127.0.0.1:1")
        with pytest.raises(RemoteMemoryStoreUnavailable):
            remote.search("anything")

    def test_count_with_source_is_honestly_unsupported_not_silently_wrong(self, server):
        port = server
        remote = RemoteMemoryStore(f"http://127.0.0.1:{port}")
        with pytest.raises(NotImplementedError):
            remote.count(source="laptop_file")


class TestOpenMemoryStoreUsesRemoteWhenConfigured:
    def test_remote_url_env_selects_remote_store(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_MEMORY_REMOTE_URL", "http://100.98.97.23:8765")
        from dourmouse.general_roster import _open_memory_store

        store = _open_memory_store()
        assert isinstance(store, RemoteMemoryStore)
        assert store.base_url == "http://100.98.97.23:8765"

    def test_remote_token_env_is_passed_through(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_MEMORY_REMOTE_URL", "http://100.98.97.23:8765")
        monkeypatch.setenv("DOURMOUSE_MEMORY_REMOTE_TOKEN", "real-desktop-token")
        from dourmouse.general_roster import _open_memory_store

        store = _open_memory_store()
        assert store.token == "real-desktop-token"

    def test_remote_without_token_env_has_none(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_MEMORY_REMOTE_URL", "http://100.98.97.23:8765")
        monkeypatch.delenv("DOURMOUSE_MEMORY_REMOTE_TOKEN", raising=False)
        from dourmouse.general_roster import _open_memory_store

        store = _open_memory_store()
        assert store.token is None

    def test_unset_env_keeps_local_store(self, monkeypatch, tmp_path):
        monkeypatch.delenv("DOURMOUSE_MEMORY_REMOTE_URL", raising=False)
        monkeypatch.setenv("DOURMOUSE_MEMORY_DB", str(tmp_path / "local.db"))
        from dourmouse.general_roster import _open_memory_store

        store = _open_memory_store()
        assert isinstance(store, MemoryStore)
        store.close()
