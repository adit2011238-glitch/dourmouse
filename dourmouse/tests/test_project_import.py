"""Tests for dourmouse/project_import.py (real projects, discovered from
Claude Code's and Codex CLI's own on-disk session history, for the
PROJECTS bookshelf).

Fixtures mirror the REAL on-disk formats verified live on this desktop:
Claude Code's per-session JSONL rollout files under
``<root>/<sanitized-cwd>/<sessionId>.jsonl`` (each record carrying its own
``cwd``/``gitBranch``), and Codex CLI's ``threads`` table in
``state_5.sqlite`` (one row per session, ``cwd``/``git_branch``/
``updated_at`` already denormalized).
"""

from __future__ import annotations

import http.client
import json
import os
import sqlite3
import threading
import time

import pytest

from dourmouse.project_import import (
    discover_claude_code_projects,
    discover_codex_projects,
    get_imported_projects,
)


def _write_claude_session(path, lines, mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _user_line(text, cwd="/Users/x/proj", branch="main"):
    return {
        "type": "user",
        "message": {"role": "user", "content": text},
        "timestamp": "2026-08-02T11:32:34.625Z",
        "cwd": cwd,
        "gitBranch": branch,
    }


class TestDiscoverClaudeCodeProjects:
    def test_missing_root_is_honest_not_configured(self, tmp_path):
        result = discover_claude_code_projects(root=tmp_path / "nope")
        assert result == {"configured": False, "root": str(tmp_path / "nope"), "records": []}

    def test_one_project_one_session(self, tmp_path):
        root = tmp_path / "projects"
        _write_claude_session(root / "-Users-x-proj" / "s1.jsonl", [_user_line("hi")])
        result = discover_claude_code_projects(root=root)
        assert result["configured"] is True
        assert len(result["records"]) == 1
        rec = result["records"][0]
        assert rec["path"] == "/Users/x/proj"
        assert rec["tool"] == "claude_code"
        assert rec["session_count"] == 1
        assert rec["git_branch"] == "main"
        assert rec["path_is_real"] is True
        assert rec["last_active"] is not None

    def test_multiple_sessions_summed_and_newest_mtime_wins(self, tmp_path):
        root = tmp_path / "projects"
        base = root / "-Users-x-proj"
        now = time.time()
        _write_claude_session(base / "s1.jsonl", [_user_line("q1")], mtime=now - 1000)
        _write_claude_session(base / "s2.jsonl", [_user_line("q2")], mtime=now)
        result = discover_claude_code_projects(root=root)
        rec = result["records"][0]
        assert rec["session_count"] == 2
        assert abs(rec["last_active"] - now) < 2

    def test_empty_project_dir_is_not_a_project(self, tmp_path):
        root = tmp_path / "projects"
        (root / "-Users-x-empty").mkdir(parents=True)
        result = discover_claude_code_projects(root=root)
        assert result["records"] == []

    def test_malformed_line_does_not_abort_the_file(self, tmp_path):
        root = tmp_path / "projects"
        path = root / "-p" / "s.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(
            "{not valid json\n" + json.dumps(_user_line("real")) + "\n",
            encoding="utf-8",
        )
        result = discover_claude_code_projects(root=root)
        assert result["records"][0]["path"] == "/Users/x/proj"

    def test_no_cwd_ever_found_falls_back_to_delabeled_dirname(self, tmp_path):
        root = tmp_path / "projects"
        _write_claude_session(
            root / "-Users-x-mystery" / "s.jsonl",
            [{"type": "queue-operation", "operation": "enqueue"}],
        )
        result = discover_claude_code_projects(root=root)
        rec = result["records"][0]
        assert rec["path_is_real"] is False
        assert rec["path"] == "Users-x-mystery"

    def test_cwd_disagreement_uses_majority_vote(self, tmp_path):
        root = tmp_path / "projects"
        base = root / "-Users-x-proj"
        _write_claude_session(base / "s1.jsonl", [_user_line("a", cwd="/real/path")])
        _write_claude_session(base / "s2.jsonl", [_user_line("b", cwd="/real/path")])
        _write_claude_session(base / "s3.jsonl", [_user_line("c", cwd="/stale/path")])
        result = discover_claude_code_projects(root=root)
        assert result["records"][0]["path"] == "/real/path"

    def test_compaction_continuation_session_still_counted(self, tmp_path):
        """A real, human-initiated session whose first 'user' turn is
        Claude Code's own context-compaction summary carries no
        origin.kind=='human' marker — verified live on this desktop. This
        module must not silently drop it (unlike history_import.py's
        per-conversation filter, which is a deliberate design choice here,
        not an oversight — see the module docstring)."""
        root = tmp_path / "projects"
        _write_claude_session(
            root / "-p" / "s.jsonl",
            [{
                "type": "user",
                "message": {
                    "role": "user",
                    "content": "This session is being continued from a previous conversation...",
                },
                "timestamp": "2026-08-02T11:32:34.625Z",
                "cwd": "/Users/x/proj",
                "origin": None,
            }],
        )
        result = discover_claude_code_projects(root=root)
        assert result["records"][0]["session_count"] == 1


def _make_codex_db(path, threads):
    con = sqlite3.connect(str(path))
    con.execute(
        """CREATE TABLE threads (
            id TEXT, cwd TEXT, git_branch TEXT, updated_at INTEGER
        )"""
    )
    con.executemany("INSERT INTO threads VALUES (?,?,?,?)", threads)
    con.commit()
    con.close()


class TestDiscoverCodexProjects:
    def test_missing_db_is_honest_not_configured(self, tmp_path):
        result = discover_codex_projects(db_path=tmp_path / "nope.sqlite")
        assert result == {"configured": False, "db": str(tmp_path / "nope.sqlite"), "records": []}

    def test_db_without_threads_table_is_honest_not_a_crash(self, tmp_path):
        db = tmp_path / "empty.sqlite"
        sqlite3.connect(str(db)).close()
        result = discover_codex_projects(db_path=db)
        assert result["configured"] is False
        assert result["records"] == []

    def test_two_threads_same_cwd_aggregated(self, tmp_path):
        db = tmp_path / "state_5.sqlite"
        _make_codex_db(db, [
            ("t1", "/Users/x/proj", "main", 1786824754),
            ("t2", "/Users/x/proj", "main", 1786899547),
        ])
        result = discover_codex_projects(db_path=db)
        assert result["configured"] is True
        assert len(result["records"]) == 1
        rec = result["records"][0]
        assert rec["path"] == "/Users/x/proj"
        assert rec["session_count"] == 2
        assert rec["last_active"] == 1786899547.0
        assert rec["git_branch"] == "main"

    def test_distinct_cwds_become_distinct_records(self, tmp_path):
        db = tmp_path / "state_5.sqlite"
        _make_codex_db(db, [
            ("t1", "/Users/x/a", "", 1786824754),
            ("t2", "/Users/x/b", "", 1786824754),
        ])
        result = discover_codex_projects(db_path=db)
        assert {r["path"] for r in result["records"]} == {"/Users/x/a", "/Users/x/b"}

    def test_thread_without_cwd_is_not_a_project(self, tmp_path):
        db = tmp_path / "state_5.sqlite"
        _make_codex_db(db, [("t1", "", "", 1786824754)])
        result = discover_codex_projects(db_path=db)
        assert result["records"] == []


class TestGetImportedProjects:
    def test_combines_and_dedupes_by_real_path(self, tmp_path):
        claude_root = tmp_path / "projects"
        _write_claude_session(
            claude_root / "-Users-x-proj" / "s1.jsonl",
            [_user_line("hi", cwd="/Users/x/proj", branch="main")],
        )
        codex_db = tmp_path / "state_5.sqlite"
        _make_codex_db(codex_db, [("t1", "/Users/x/proj", "main", 1786899547)])

        result = get_imported_projects(claude_root=claude_root, codex_db=codex_db)
        assert result["claude_code"]["configured"] is True
        assert result["codex_cli"]["configured"] is True
        assert len(result["projects"]) == 1
        p = result["projects"][0]
        assert p["path"] == "/Users/x/proj"
        assert p["title"] == "proj"
        assert p["sources"] == ["claude_code", "codex_cli"]
        assert p["session_counts"] == {"claude_code": 1, "codex_cli": 1}
        assert p["session_count"] == 2
        assert p["stat"] == "2 sessions"
        assert p["last_active"] is not None
        assert p["git_branch"] == "main"

    def test_distinct_projects_stay_separate(self, tmp_path):
        claude_root = tmp_path / "projects"
        _write_claude_session(
            claude_root / "-Users-x-a" / "s1.jsonl", [_user_line("a", cwd="/Users/x/a")]
        )
        codex_db = tmp_path / "state_5.sqlite"
        _make_codex_db(codex_db, [("t1", "/Users/x/b", "", 1786899547)])
        result = get_imported_projects(claude_root=claude_root, codex_db=codex_db)
        paths = {p["path"] for p in result["projects"]}
        assert paths == {"/Users/x/a", "/Users/x/b"}

    def test_sorted_newest_active_first(self, tmp_path):
        claude_root = tmp_path / "projects"
        now = time.time()
        _write_claude_session(
            claude_root / "-Users-x-old" / "s.jsonl", [_user_line("a", cwd="/Users/x/old")],
            mtime=now - 10000,
        )
        _write_claude_session(
            claude_root / "-Users-x-new" / "s.jsonl", [_user_line("a", cwd="/Users/x/new")],
            mtime=now,
        )
        result = get_imported_projects(claude_root=claude_root, codex_db=tmp_path / "nope.sqlite")
        paths = [p["path"] for p in result["projects"]]
        assert paths == ["/Users/x/new", "/Users/x/old"]

    def test_exists_flag_reflects_real_disk_state(self, tmp_path):
        real_dir = tmp_path / "real_project"
        real_dir.mkdir()
        claude_root = tmp_path / "projects"
        _write_claude_session(
            claude_root / "-p1" / "s.jsonl", [_user_line("a", cwd=str(real_dir))]
        )
        _write_claude_session(
            claude_root / "-p2" / "s.jsonl", [_user_line("a", cwd="/Users/x/long_gone")]
        )
        result = get_imported_projects(claude_root=claude_root, codex_db=tmp_path / "nope.sqlite")
        by_path = {p["path"]: p for p in result["projects"]}
        assert by_path[str(real_dir)]["exists"] is True
        assert by_path["/Users/x/long_gone"]["exists"] is False

    def test_neither_source_configured_never_raises(self, tmp_path):
        result = get_imported_projects(
            claude_root=tmp_path / "no_claude", codex_db=tmp_path / "no_codex.sqlite"
        )
        assert result["claude_code"]["configured"] is False
        assert result["codex_cli"]["configured"] is False
        assert result["projects"] == []


class TestProjectsImportedEndpoint:
    """Live HTTP check that GET /api/projects/imported is wired into
    webui.py and returns the real extracted shape (self-contained: starts
    its own server rather than sharing test_webui.py's fixtures, so this
    file has no coupling to concurrent edits elsewhere in that big test
    module)."""

    def test_endpoint_returns_real_extraction(self, tmp_path, monkeypatch):
        import dourmouse.project_import as pi_module
        from dourmouse.dispatch import DispatchRegistry
        from dourmouse.webui import run_server

        claude_root = tmp_path / "projects"
        _write_claude_session(
            claude_root / "-Users-x-proj" / "s1.jsonl",
            [_user_line("hi", cwd="/Users/x/proj", branch="main")],
        )
        monkeypatch.setattr(pi_module, "_claude_projects_root", lambda: claude_root)
        monkeypatch.setattr(pi_module, "_codex_state_db", lambda: tmp_path / "no_codex.sqlite")
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))

        srv = run_server(DispatchRegistry(), port=0, client=None, config=None)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            port = srv.server_address[1]
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/api/projects/imported")
            resp = conn.getresponse()
            assert resp.status == 200
            data = json.loads(resp.read())
            conn.close()
            assert data["claude_code"]["configured"] is True
            assert data["codex_cli"]["configured"] is False
            assert len(data["projects"]) == 1
            assert data["projects"][0]["path"] == "/Users/x/proj"
            assert data["projects"][0]["sources"] == ["claude_code"]
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=2)
