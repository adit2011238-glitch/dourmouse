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
        assert store.search("value one") == []  # old body gone

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
        assert _fts_query("sneaky \" OR *") == '"sneaky" AND "OR"'
        assert _fts_query("  ") == ""
        assert _fts_query("nvidia gpu") == '"nvidia" AND "gpu"'


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
