"""Tests for dourmouse/history_import.py (roadmap: import Claude Code +
Codex CLI history, "already on disk, no API needed").

Fixtures mirror the REAL formats read from disk before this module was
written: Claude Code's JSONL rollout lines (type: user/assistant/
custom-title/last-prompt) and Codex CLI's ``threads`` table in
``state_5.sqlite``. The Codex fixture reproduces a real observed edge
case verbatim: two live threads on the desktop shared the identical
title "Write a Python function fib(n). Code only." — proving titles
alone are not a safe uniqueness key.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from dourmouse.history_import import (
    import_all_history,
    import_claude_code_history,
    import_codex_history,
)
from dourmouse.memory_store import MemoryStore


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "mem" / "test.db")
    yield s
    s.close()


def _write_claude_session(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


def _user_line(text, ts="2026-08-02T11:32:34.625Z", cwd="/Users/x/proj", branch="main",
                origin="human"):
    return {
        "type": "user",
        "message": {"role": "user", "content": text},
        "timestamp": ts,
        "cwd": cwd,
        "gitBranch": branch,
        # Real Claude Code field: {"kind": "human"} on a genuine top-level
        # conversation, absent on Claude Code's own internal/orchestration
        # sub-runs. Defaults to human so every existing test represents a
        # real conversation without having to say so at every call site.
        **({"origin": {"kind": origin}} if origin else {}),
    }


def _assistant_line(text, ts="2026-08-02T11:32:40.000Z"):
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        "timestamp": ts,
    }


class TestClaudeCodeImport:
    def test_basic_session_with_custom_title(self, store, tmp_path):
        root = tmp_path / "projects" / "-Users-x-proj"
        _write_claude_session(
            root / "abc12345-0000-0000-0000-000000000001.jsonl",
            [
                _user_line("refer to all code and files in atlas v13"),
                {"type": "custom-title", "customTitle": "Atlas v13 due diligence chat"},
                _assistant_line("Need location. ATLAS v13 files not in current dir."),
                {"type": "last-prompt", "lastPrompt": "refer to all code and files in atlas v13"},
            ],
        )
        result = import_claude_code_history(store, root=tmp_path / "projects")
        assert result == {
            "scanned": 1, "imported": 1, "skipped": 0, "skipped_by_reason": {},
            "root": str(tmp_path / "projects"),
        }
        hits = store.search("Atlas v13")
        assert hits
        fact = store.get(*[hits[0]["source"], hits[0]["title"]])
        assert fact["title"].startswith("Atlas v13 due diligence chat [abc12345]")
        assert "proj" in fact["body"]
        assert "main" in fact["body"]
        assert "Need location" in fact["body"]

    def test_falls_back_to_first_user_message_without_custom_title(self, store, tmp_path):
        root = tmp_path / "projects" / "-p"
        _write_claude_session(
            root / "s2.jsonl",
            [_user_line("how do I list files on windows"), _assistant_line("Open File Explorer.")],
        )
        import_claude_code_history(store, root=tmp_path / "projects")
        facts = store.all_facts()
        assert any("how do I list files on windows" in f["title"] for f in facts)

    def test_empty_session_is_skipped_not_imported(self, store, tmp_path):
        root = tmp_path / "projects" / "-p"
        _write_claude_session(root / "empty.jsonl", [{"type": "queue-operation", "operation": "enqueue"}])
        result = import_claude_code_history(store, root=tmp_path / "projects")
        assert result["scanned"] == 1
        assert result["imported"] == 0
        assert result["skipped"] == 1
        assert store.count() == 0

    def test_malformed_line_does_not_abort_the_whole_file(self, store, tmp_path):
        root = tmp_path / "projects" / "-p"
        path = root / "s3.jsonl"
        root.mkdir(parents=True)
        path.write_text(
            json.dumps(_user_line("real question")) + "\n"
            + "{not valid json\n"
            + json.dumps(_assistant_line("real answer")) + "\n",
            encoding="utf-8",
        )
        result = import_claude_code_history(store, root=tmp_path / "projects")
        assert result["imported"] == 1
        fact = store.all_facts()[0]
        assert "real question" in fact["body"]
        assert "real answer" in fact["body"]

    def test_rerunning_is_idempotent(self, store, tmp_path):
        root = tmp_path / "projects" / "-p"
        _write_claude_session(root / "s4.jsonl", [_user_line("same session every time")])
        import_claude_code_history(store, root=tmp_path / "projects")
        n1 = store.count()
        import_claude_code_history(store, root=tmp_path / "projects")
        n2 = store.count()
        assert n1 == n2 == 1

    def test_project_label_derived_from_sanitized_dirname(self, store, tmp_path):
        root = tmp_path / "projects" / "-Users-aditagrawal-Claude-code"
        _write_claude_session(root / "s5.jsonl", [_user_line("hello")])
        import_claude_code_history(store, root=tmp_path / "projects")
        fact = store.all_facts()[0]
        assert "Users-aditagrawal-Claude-code" in fact["body"]
        assert not fact["body"].startswith("Project: -Users")  # leading '-' stripped

    def test_missing_root_returns_zeros_not_a_crash(self, store, tmp_path):
        result = import_claude_code_history(store, root=tmp_path / "does_not_exist")
        assert result == {
            "scanned": 0, "imported": 0, "skipped": 0, "skipped_by_reason": {},
            "root": str(tmp_path / "does_not_exist"),
        }

    def test_orchestration_sub_sessions_are_not_imported_as_user_history(self, store, tmp_path):
        """Verified live on 81 real files: 30 of them are Claude Code's OWN
        internal sub-agent runs, each recorded as a full session file whose
        "first user message" is actually a system-style instruction the
        user never typed. Real string observed on disk, used verbatim."""
        root = tmp_path / "projects" / "-p"
        _write_claude_session(
            root / "worker.jsonl",
            [_user_line(
                "You are one independent worker inside DourMouse's ALL-HANDS "
                "run. You receive a goal and must complete it.",
                origin=None,
            )],
        )
        result = import_claude_code_history(store, root=tmp_path / "projects")
        assert result["scanned"] == 1
        assert result["imported"] == 0
        assert result["skipped"] == 1
        assert result["skipped_by_reason"] == {"orchestration": 1}
        assert store.count() == 0

    def test_human_and_orchestration_sessions_side_by_side(self, store, tmp_path):
        """The filter must not over-fire on real conversations sitting in
        the same import batch as orchestration noise."""
        root = tmp_path / "projects" / "-p"
        _write_claude_session(root / "real.jsonl", [_user_line("what does this function do")])
        _write_claude_session(
            root / "worker.jsonl",
            [_user_line("You are one independent worker inside an ALL-HANDS run.", origin=None)],
        )
        result = import_claude_code_history(store, root=tmp_path / "projects")
        assert result["scanned"] == 2
        assert result["imported"] == 1
        assert result["skipped_by_reason"] == {"orchestration": 1}
        assert store.count() == 1
        assert "what does this function do" in store.all_facts()[0]["title"]

    def test_multiple_sessions_all_scanned(self, store, tmp_path):
        base = tmp_path / "projects" / "-p"
        for i in range(3):
            _write_claude_session(base / f"s{i}.jsonl", [_user_line(f"question number {i}")])
        result = import_claude_code_history(store, root=tmp_path / "projects")
        assert result["scanned"] == 3
        assert result["imported"] == 3
        assert store.count() == 3


def _make_codex_db(path, threads):
    con = sqlite3.connect(str(path))
    con.execute(
        """CREATE TABLE threads (
            id TEXT, title TEXT, first_user_message TEXT, preview TEXT,
            cwd TEXT, model TEXT, git_branch TEXT,
            created_at INTEGER, updated_at INTEGER, archived INTEGER
        )"""
    )
    con.executemany(
        "INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?,?)",
        threads,
    )
    con.commit()
    con.close()


class TestCodexImport:
    def test_two_threads_sharing_an_identical_title_both_survive(self, store, tmp_path):
        """The exact edge case found live: two real Codex threads on the
        desktop had the SAME title. Without the id in the fact title, the
        second import would upsert over the first and silently lose it."""
        db = tmp_path / "state_5.sqlite"
        same_title = "Write a Python function fib(n). Code only."
        _make_codex_db(db, [
            ("01a0070e-aaaa", same_title, same_title, same_title,
             r"C:\dourmouse", "gpt-5.6-sol", None, 1786824754, 1786824775, 0),
            ("01a00b83-bbbb", same_title, same_title, same_title,
             r"C:\dourmouse", "gpt-5.6-sol", None, 1786899547, 1786899568, 0),
        ])
        result = import_codex_history(store, db_path=db)
        assert result["scanned"] == 2
        assert result["imported"] == 2
        assert store.count() == 2  # NOT 1 — this is the bug this test guards

    def test_thread_fact_has_readable_body(self, store, tmp_path):
        db = tmp_path / "state_5.sqlite"
        _make_codex_db(db, [
            ("01aaa", "Fix the auth bug", "Fix the auth bug", "Fix the auth bug",
             r"C:\dourmouse", "gpt-5.6-sol", "main", 1786824754, 1786824775, 0),
        ])
        import_codex_history(store, db_path=db)
        fact = store.all_facts()[0]
        assert "C:\\dourmouse" in fact["body"]
        assert "gpt-5.6-sol" in fact["body"]
        assert "main" in fact["body"]
        assert "Fix the auth bug" in fact["body"]

    def test_archived_thread_is_imported_and_noted(self, store, tmp_path):
        db = tmp_path / "state_5.sqlite"
        _make_codex_db(db, [
            ("01aaa", "old thread", "old thread", "old thread",
             "", "", None, 1786824754, 1786824775, 1),
        ])
        import_codex_history(store, db_path=db)
        fact = store.all_facts()[0]
        assert "archived" in fact["body"].lower()

    def test_missing_db_is_honest_not_configured(self, store, tmp_path):
        result = import_codex_history(store, db_path=tmp_path / "nope.sqlite")
        assert result["configured"] is False
        assert result["imported"] == 0

    def test_db_without_threads_table_is_honest_not_a_crash(self, store, tmp_path):
        db = tmp_path / "empty.sqlite"
        sqlite3.connect(str(db)).close()
        result = import_codex_history(store, db_path=db)
        assert result["configured"] is False
        assert result["imported"] == 0

    def test_rerunning_is_idempotent(self, store, tmp_path):
        db = tmp_path / "state_5.sqlite"
        _make_codex_db(db, [
            ("01aaa", "same thread", "same thread", "same thread",
             "", "", None, 1786824754, 1786824775, 0),
        ])
        import_codex_history(store, db_path=db)
        n1 = store.count()
        import_codex_history(store, db_path=db)
        n2 = store.count()
        assert n1 == n2 == 1

    def test_blank_thread_is_skipped(self, store, tmp_path):
        db = tmp_path / "state_5.sqlite"
        _make_codex_db(db, [("01aaa", None, None, None, "", "", None, 0, 0, 0)])
        result = import_codex_history(store, db_path=db)
        assert result["scanned"] == 1
        assert result["imported"] == 0
        assert result["skipped"] == 1


class TestImportAll:
    def test_combines_both_sources(self, store, tmp_path):
        claude_root = tmp_path / "projects"
        _write_claude_session(
            claude_root / "-p" / "s1.jsonl", [_user_line("claude side question")]
        )
        codex_db = tmp_path / "state_5.sqlite"
        _make_codex_db(codex_db, [
            ("01aaa", "codex side task", "codex side task", "codex side task",
             "", "", None, 1786824754, 1786824775, 0),
        ])
        result = import_all_history(store, claude_root=claude_root, codex_db=codex_db)
        assert result["claude"]["imported"] == 1
        assert result["codex"]["imported"] == 1
        assert store.count() == 2

    def test_missing_sources_never_raise(self, store, tmp_path):
        result = import_all_history(
            store,
            claude_root=tmp_path / "no_claude",
            codex_db=tmp_path / "no_codex.sqlite",
        )
        assert result["claude"]["imported"] == 0
        assert result["codex"]["imported"] == 0
        assert result["codex"]["configured"] is False
