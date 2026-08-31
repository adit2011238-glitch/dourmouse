"""Tests for dourmouse/project_bookkeeper.py — the persisted, incrementally
updated project metadata layer on top of project_import.py.

Fixtures mirror the same real on-disk formats test_project_import.py uses
(Claude Code per-session JSONL rollout files; Codex CLI's ``threads``
table), plus the ``custom-title`` / content-block shapes those real files
also carry, since this module additionally extracts real title/prompt text
project_import.py itself never reads.
"""

from __future__ import annotations

import http.client
import json
import os
import sqlite3
import threading
import time

import pytest

from dourmouse.project_bookkeeper import (
    create_project,
    delete_project,
    get_bookkeeper,
    refresh,
)


def _write_claude_session(path, lines, mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _user_line(text, cwd="/Users/x/proj", branch="main", ts="2026-08-02T11:32:34Z"):
    return {
        "type": "user",
        "message": {"role": "user", "content": text},
        "timestamp": ts,
        "cwd": cwd,
        "gitBranch": branch,
    }


def _custom_title_line(title):
    return {"type": "custom-title", "customTitle": title}


def _titled_session(title, cwd="/Users/x/proj", branch="main"):
    """A realistic session: a custom-title line PLUS the real user turn
    that carries cwd/gitBranch — custom-title records don't carry cwd
    themselves (see project_import.py's own _session_cwd_and_branch,
    which only reads cwd off user/assistant records)."""
    return [_custom_title_line(title), _user_line("hi", cwd=cwd, branch=branch)]


def _make_codex_db(path, threads):
    con = sqlite3.connect(str(path))
    con.execute(
        """CREATE TABLE threads (
            id TEXT, cwd TEXT, git_branch TEXT, updated_at INTEGER,
            title TEXT, first_user_message TEXT, preview TEXT
        )"""
    )
    con.executemany(
        "INSERT INTO threads (id, cwd, git_branch, updated_at, title, first_user_message, preview)"
        " VALUES (?,?,?,?,?,?,?)",
        threads,
    )
    con.commit()
    con.close()


class TestRefreshClaudeContext:
    def test_custom_title_wins_over_first_user(self, tmp_path):
        claude_root = tmp_path / "projects"
        _write_claude_session(
            claude_root / "-Users-x-proj" / "s1.jsonl",
            [_custom_title_line("Fix the login bug"), _user_line("please help with login")],
        )
        store = tmp_path / "store.json"
        result = refresh(
            claude_root=claude_root, codex_db=tmp_path / "no_codex.sqlite", store_path=store
        )
        assert len(result["projects"]) == 1
        p = result["projects"][0]
        assert p["path"] == "/Users/x/proj"
        assert p["name"] == "proj"
        assert p["context"] == "Fix the login bug"
        assert p["context_source"] == "claude_code:custom_title"
        assert p["context_items"][0]["text"] == "Fix the login bug"
        assert p["context_items"][0]["field"] == "custom_title"
        assert result["context_method"] == "extractive"

    def test_falls_back_to_first_user_prompt_verbatim(self, tmp_path):
        claude_root = tmp_path / "projects"
        _write_claude_session(
            claude_root / "-Users-x-proj" / "s1.jsonl",
            [_user_line("please add dark mode to the settings screen")],
        )
        store = tmp_path / "store.json"
        result = refresh(
            claude_root=claude_root, codex_db=tmp_path / "no_codex.sqlite", store_path=store
        )
        p = result["projects"][0]
        assert p["context"] == "please add dark mode to the settings screen"
        assert p["context_source"] == "claude_code:first_user_prompt"

    def test_long_prompt_is_truncated_not_fabricated(self, tmp_path):
        claude_root = tmp_path / "projects"
        long_text = "x" * 500
        _write_claude_session(
            claude_root / "-Users-x-proj" / "s1.jsonl", [_user_line(long_text)]
        )
        store = tmp_path / "store.json"
        result = refresh(
            claude_root=claude_root, codex_db=tmp_path / "no_codex.sqlite", store_path=store
        )
        p = result["projects"][0]
        assert p["context"].startswith("x" * 50)
        assert p["context"].endswith("…")
        assert len(p["context"]) <= 160

    def test_no_readable_content_yields_honest_empty_context(self, tmp_path):
        claude_root = tmp_path / "projects"
        # A session with no custom-title and no real user text (only a
        # queue-operation record) — same edge case project_import.py's own
        # tests cover for path resolution; here it must not fabricate a
        # context either.
        _write_claude_session(
            claude_root / "-Users-x-mystery" / "s.jsonl",
            [{"type": "queue-operation", "operation": "enqueue"}],
        )
        store = tmp_path / "store.json"
        result = refresh(
            claude_root=claude_root, codex_db=tmp_path / "no_codex.sqlite", store_path=store
        )
        p = result["projects"][0]
        assert p["context"] == ""
        assert p["context_source"] == "none"
        assert p["context_items"] == []

    def test_multiple_sessions_newest_first_bounded_to_a_few(self, tmp_path):
        claude_root = tmp_path / "projects"
        base = claude_root / "-Users-x-proj"
        now = time.time()
        for i in range(5):
            _write_claude_session(
                base / f"s{i}.jsonl", [_user_line(f"task number {i}")], mtime=now - (5 - i) * 100
            )
        store = tmp_path / "store.json"
        result = refresh(
            claude_root=claude_root, codex_db=tmp_path / "no_codex.sqlite", store_path=store
        )
        p = result["projects"][0]
        # newest write (s4) should be the live context; at most 3 kept.
        assert p["context"] == "task number 4"
        assert len(p["context_items"]) <= 3


class TestRefreshCodexContext:
    def test_title_preferred_over_first_user_message_and_preview(self, tmp_path):
        codex_db = tmp_path / "state_5.sqlite"
        _make_codex_db(codex_db, [
            ("t1", "/Users/x/proj", "main", 1786899547, "Ship the release notes", "asked about release", "preview text"),
        ])
        store = tmp_path / "store.json"
        result = refresh(
            claude_root=tmp_path / "no_claude", codex_db=codex_db, store_path=store
        )
        p = result["projects"][0]
        assert p["context"] == "Ship the release notes"
        assert p["context_source"] == "codex_cli:title"

    def test_falls_back_to_first_user_message_then_preview(self, tmp_path):
        codex_db = tmp_path / "state_5.sqlite"
        _make_codex_db(codex_db, [
            ("t1", "/Users/x/a", "", 1786899547, "", "add tests for parser", ""),
            ("t2", "/Users/x/b", "", 1786899547, "", "", "just a preview line"),
        ])
        store = tmp_path / "store.json"
        result = refresh(claude_root=tmp_path / "no_claude", codex_db=codex_db, store_path=store)
        by_path = {p["path"]: p for p in result["projects"]}
        assert by_path["/Users/x/a"]["context"] == "add tests for parser"
        assert by_path["/Users/x/a"]["context_source"] == "codex_cli:first_user_message"
        assert by_path["/Users/x/b"]["context"] == "just a preview line"
        assert by_path["/Users/x/b"]["context_source"] == "codex_cli:preview"

    def test_no_jsonl_opened_for_codex_only_project(self, tmp_path):
        """Codex-only project: context must come purely from the threads
        table row, never by trying to open a nonexistent Claude directory."""
        codex_db = tmp_path / "state_5.sqlite"
        _make_codex_db(codex_db, [
            ("t1", "/Users/x/codexonly", "", 1786899547, "Codex-only task", "", ""),
        ])
        store = tmp_path / "store.json"
        result = refresh(
            claude_root=tmp_path / "no_claude_dir_at_all", codex_db=codex_db, store_path=store
        )
        p = result["projects"][0]
        assert p["sources"] == ["codex_cli"]
        assert p["context"] == "Codex-only task"


class TestIncrementalCheckpoint:
    def test_unchanged_project_is_not_reprocessed(self, tmp_path, monkeypatch):
        claude_root = tmp_path / "projects"
        session_path = claude_root / "-Users-x-proj" / "s1.jsonl"
        _write_claude_session(session_path, _titled_session("Original title"))
        store = tmp_path / "store.json"
        codex_db = tmp_path / "no_codex.sqlite"

        first = refresh(claude_root=claude_root, codex_db=codex_db, store_path=store)
        assert first["projects"][0]["context"] == "Original title"
        first_context_updated_at = first["projects"][0]["context_updated_at"]

        # Rewrite the SAME file's content without changing its mtime (or
        # bumping last_active at all) — a real checkpoint must skip
        # reopening this file, so the stale-on-disk text must NOT appear.
        stat_before = session_path.stat()
        session_path.write_text(
            "\n".join(json.dumps(line) for line in _titled_session("Should not be seen")) + "\n",
            encoding="utf-8",
        )
        os.utime(session_path, (stat_before.st_atime, stat_before.st_mtime))

        second = refresh(claude_root=claude_root, codex_db=codex_db, store_path=store)
        assert second["projects"][0]["context"] == "Original title"
        assert second["projects"][0]["context_updated_at"] == first_context_updated_at

    def test_new_session_bumps_last_active_and_is_reprocessed(self, tmp_path):
        claude_root = tmp_path / "projects"
        base = claude_root / "-Users-x-proj"
        now = time.time()
        _write_claude_session(base / "s1.jsonl", _titled_session("First"), mtime=now - 1000)
        store = tmp_path / "store.json"
        codex_db = tmp_path / "no_codex.sqlite"

        first = refresh(claude_root=claude_root, codex_db=codex_db, store_path=store)
        assert first["projects"][0]["context"] == "First"

        _write_claude_session(base / "s2.jsonl", _titled_session("Second, newer"), mtime=now)
        second = refresh(claude_root=claude_root, codex_db=codex_db, store_path=store)
        assert second["projects"][0]["context"] == "Second, newer"

    def test_store_file_is_real_and_inspectable(self, tmp_path):
        claude_root = tmp_path / "projects"
        _write_claude_session(
            claude_root / "-Users-x-proj" / "s1.jsonl", _titled_session("A title")
        )
        store = tmp_path / "store.json"
        refresh(claude_root=claude_root, codex_db=tmp_path / "no_codex.sqlite", store_path=store)
        assert store.exists()
        on_disk = json.loads(store.read_text())
        assert on_disk["version"] == 1
        assert on_disk["last_refreshed"] is not None
        rec = on_disk["projects"]["/Users/x/proj"]
        # the internal checkpoint field is real and visible on disk, even
        # though it is stripped from the public API response.
        assert "_checkpoint_last_active_epoch" in rec


class TestGetBookkeeper:
    def test_bootstraps_on_first_call_when_store_is_empty(self, tmp_path):
        claude_root = tmp_path / "projects"
        _write_claude_session(
            claude_root / "-Users-x-proj" / "s1.jsonl", _titled_session("Bootstrapped")
        )
        store = tmp_path / "store.json"
        assert not store.exists()
        result = get_bookkeeper(
            claude_root=claude_root, codex_db=tmp_path / "no_codex.sqlite", store_path=store
        )
        assert result["last_refreshed"] is not None
        assert result["projects"][0]["context"] == "Bootstrapped"

    def test_plain_get_does_not_pick_up_new_sessions_until_refresh(self, tmp_path):
        """GET serves the persisted record as-is — it must NOT silently
        rescan the filesystem on every call (that is the whole point of
        this module over calling project_import directly)."""
        claude_root = tmp_path / "projects"
        base = claude_root / "-Users-x-proj"
        _write_claude_session(base / "s1.jsonl", _titled_session("Before"))
        store = tmp_path / "store.json"
        codex_db = tmp_path / "no_codex.sqlite"

        first = get_bookkeeper(claude_root=claude_root, codex_db=codex_db, store_path=store)
        assert first["projects"][0]["context"] == "Before"

        _write_claude_session(base / "s2.jsonl", _titled_session("After"), mtime=time.time() + 10000)
        still_stale = get_bookkeeper(claude_root=claude_root, codex_db=codex_db, store_path=store)
        assert still_stale["projects"][0]["context"] == "Before"

        refreshed = refresh(claude_root=claude_root, codex_db=codex_db, store_path=store)
        assert refreshed["projects"][0]["context"] == "After"

    def test_response_shape_has_bookshelf_card_fields(self, tmp_path):
        claude_root = tmp_path / "projects"
        _write_claude_session(
            claude_root / "-Users-x-proj" / "s1.jsonl",
            [_user_line("a real prompt", cwd="/Users/x/proj", branch="main")],
        )
        store = tmp_path / "store.json"
        result = get_bookkeeper(
            claude_root=claude_root, codex_db=tmp_path / "no_codex.sqlite", store_path=store
        )
        p = result["projects"][0]
        for field in (
            "path", "name", "sources", "session_count", "session_counts",
            "last_active", "git_branch", "stat", "exists",
            "context", "context_source", "context_items", "context_updated_at",
        ):
            assert field in p, f"missing field: {field}"
        assert not any(k.startswith("_") for k in p), "internal checkpoint field leaked to public shape"

    def test_neither_source_configured_never_raises(self, tmp_path):
        store = tmp_path / "store.json"
        result = get_bookkeeper(
            claude_root=tmp_path / "no_claude", codex_db=tmp_path / "no_codex.sqlite",
            store_path=store,
        )
        assert result["claude_code"]["configured"] is False
        assert result["codex_cli"]["configured"] is False
        assert result["projects"] == []
        assert result["last_refreshed"] is not None


class TestBookkeeperEndpoints:
    """Live HTTP check that both routes are wired into webui.py (real
    server, self-contained — no coupling to concurrent edits elsewhere in
    the big test_webui.py module, same discipline as
    test_console_projects_import.py)."""

    def _start(self):
        from dourmouse.dispatch import DispatchRegistry
        from dourmouse.webui import run_server

        srv = run_server(DispatchRegistry(), port=0, client=None, config=None)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        return srv, thread

    def test_get_then_post_refresh_roundtrip(self, tmp_path, monkeypatch):
        import dourmouse.project_import as pi_module

        claude_root = tmp_path / "projects"
        base = claude_root / "-Users-x-proj"
        _write_claude_session(base / "s1.jsonl", _titled_session("Endpoint title"))
        monkeypatch.setattr(pi_module, "_claude_projects_root", lambda: claude_root)
        monkeypatch.setattr(pi_module, "_codex_state_db", lambda: tmp_path / "no_codex.sqlite")
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))

        srv, thread = self._start()
        try:
            port = srv.server_address[1]
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)

            conn.request("GET", "/api/projects/bookkeeper")
            resp = conn.getresponse()
            assert resp.status == 200
            data = json.loads(resp.read())
            assert data["last_refreshed"] is not None
            assert data["context_method"] == "extractive"
            assert len(data["projects"]) == 1
            assert data["projects"][0]["path"] == "/Users/x/proj"
            assert data["projects"][0]["context"] == "Endpoint title"

            # A new, newer session appears on disk; GET again must still be
            # the stale, persisted view...
            _write_claude_session(
                base / "s2.jsonl", _titled_session("Refreshed title"),
                mtime=time.time() + 10000,
            )
            conn.request("GET", "/api/projects/bookkeeper")
            resp = conn.getresponse()
            data = json.loads(resp.read())
            assert data["projects"][0]["context"] == "Endpoint title"

            # ...until POST /refresh is called explicitly.
            conn.request("POST", "/api/projects/bookkeeper/refresh", body=b"{}")
            resp = conn.getresponse()
            assert resp.status == 200
            data = json.loads(resp.read())
            assert data["ok"] is True
            assert data["projects"][0]["context"] == "Refreshed title"

            conn.request("GET", "/api/projects/bookkeeper")
            resp = conn.getresponse()
            data = json.loads(resp.read())
            assert data["projects"][0]["context"] == "Refreshed title"
            conn.close()
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=2)


class TestCreateAndDeleteProject:
    """v13.4 — explicit user request: "add the features to create and
    delete projects" on the PROJECTS bookshelf. Manual create/delete on
    top of the auto-discovered (refresh()-derived) project set, without
    either being silently wiped by, or silently wiping, the other."""

    def test_create_adds_a_real_manual_project(self, tmp_path):
        sp = tmp_path / "store.json"
        record = create_project("My Thing", str(tmp_path / "proj"), "a real note", store_path=sp)
        assert record["name"] == "My Thing"
        assert record["sources"] == ["manual"]
        view = get_bookkeeper(store_path=sp)
        assert any(p["name"] == "My Thing" for p in view["projects"])

    def test_create_requires_name_and_path(self, tmp_path):
        sp = tmp_path / "store.json"
        with pytest.raises(ValueError):
            create_project("", str(tmp_path), store_path=sp)
        with pytest.raises(ValueError):
            create_project("Name", "", store_path=sp)

    def test_create_refuses_duplicate_path(self, tmp_path):
        sp = tmp_path / "store.json"
        path = str(tmp_path / "dup")
        create_project("First", path, store_path=sp)
        with pytest.raises(ValueError):
            create_project("Second", path, store_path=sp)

    def test_manual_project_exists_flag_reflects_real_filesystem(self, tmp_path):
        sp = tmp_path / "store.json"
        real_dir = tmp_path / "is_real"
        real_dir.mkdir()
        rec_real = create_project("Real", str(real_dir), store_path=sp)
        rec_fake = create_project("Not Yet", str(tmp_path / "not_real_yet"), store_path=sp)
        assert rec_real["exists"] is True
        assert rec_fake["exists"] is False

    def test_delete_removes_a_manual_project(self, tmp_path):
        sp = tmp_path / "store.json"
        path = str(tmp_path / "gone")
        create_project("Gone Soon", path, store_path=sp)
        assert delete_project(path, store_path=sp) is True
        view = get_bookkeeper(store_path=sp)
        assert not any(p["path"] == path for p in view["projects"])

    def test_delete_unknown_path_returns_false_not_an_error(self, tmp_path):
        sp = tmp_path / "store.json"
        assert delete_project(str(tmp_path / "never-existed"), store_path=sp) is False

    def test_manual_project_survives_a_refresh_call(self, tmp_path, monkeypatch):
        """The real bug this whole feature would have hit without a
        SEPARATE manual_projects dict: refresh() rebuilds "projects" from
        a fresh scan every call -- a manually created project stored
        there would vanish on the very next refresh."""
        import dourmouse.project_import as project_import

        monkeypatch.setattr(
            project_import, "get_imported_projects",
            lambda claude_root=None, codex_db=None: {
                "claude_code": {"configured": False}, "codex_cli": {"configured": False}, "projects": [],
            },
        )
        sp = tmp_path / "store.json"
        create_project("Survivor", str(tmp_path / "survivor"), store_path=sp)
        refresh(store_path=sp)
        view = get_bookkeeper(store_path=sp)
        assert any(p["name"] == "Survivor" for p in view["projects"])

    def test_delete_of_an_auto_discovered_project_hides_it_and_a_refresh_does_not_resurrect_it(self, tmp_path, monkeypatch):
        """The real bug delete_project's hidden_paths mechanism exists to
        prevent: directly deleting from the "projects" dict would come
        right back on the next refresh(), since that dict is rebuilt from
        a fresh real scan every call."""
        import dourmouse.project_import as project_import

        auto_path = str(tmp_path / "auto_proj")
        fake_imported = {
            "claude_code": {"configured": True}, "codex_cli": {"configured": False},
            "projects": [{
                "path": auto_path, "title": "Auto Found", "sources": ["claude_code"],
                "session_count": 1, "session_counts": {"claude_code": 1},
                "last_active": "2026-08-30T00:00:00+00:00", "git_branch": None,
                "stat": "1 session", "exists": True,
            }],
        }
        monkeypatch.setattr(
            project_import, "get_imported_projects",
            lambda claude_root=None, codex_db=None: fake_imported,
        )
        sp = tmp_path / "store.json"
        refresh(store_path=sp)
        assert any(p["path"] == auto_path for p in get_bookkeeper(store_path=sp)["projects"])

        assert delete_project(auto_path, store_path=sp) is True
        assert not any(p["path"] == auto_path for p in get_bookkeeper(store_path=sp)["projects"])

        # The real regression check: refresh() re-scans and would put
        # auto_path right back into "projects" if hidden_paths weren't honored.
        refresh(store_path=sp)
        assert not any(p["path"] == auto_path for p in get_bookkeeper(store_path=sp)["projects"])

    def test_recreating_at_a_hidden_path_unhides_it(self, tmp_path):
        sp = tmp_path / "store.json"
        path = str(tmp_path / "reborn")
        create_project("First Life", path, store_path=sp)
        delete_project(path, store_path=sp)
        assert not any(p["path"] == path for p in get_bookkeeper(store_path=sp)["projects"])
        create_project("Second Life", path, store_path=sp)
        assert any(p["path"] == path and p["name"] == "Second Life"
                   for p in get_bookkeeper(store_path=sp)["projects"])
