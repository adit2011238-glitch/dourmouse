"""Fix agent FD1: P3-35, the Codex importer handles odd paths and reports schema drift instead of hiding it."""

from __future__ import annotations

import sqlite3

import pytest

from dourmouse.history_import import import_codex_history
from dourmouse.memory_store import MemoryStore


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "m.db")
    yield s
    s.close()


def _make_db(path, columns=None):
    cols = columns or ("id, title, first_user_message, preview, cwd, model, git_branch, created_at, updated_at, "
                       "archived")
    con = sqlite3.connect(path)
    con.execute(f"CREATE TABLE threads ({cols})")
    return con


def test_a_db_path_with_question_mark_and_hash_is_opened(store, tmp_path):
    d = tmp_path / "odd dir? #1 %20"
    d.mkdir()
    db = d / "state_5.sqlite"
    con = _make_db(db)
    con.execute("INSERT INTO threads VALUES ('abc123','A title','hi','p','/w','m','main',1700000000,1700000001,0)")
    con.commit()
    con.close()
    r = import_codex_history(store, db)
    assert r["configured"] is True and r["scanned"] == 1 and r["imported"] == 1, r
    assert "error" not in r


def test_schema_drift_is_reported_as_an_error_not_as_not_configured(store, tmp_path):
    db = tmp_path / "state_6.sqlite"
    con = _make_db(db, columns="id, title")  # a column Codex renamed away
    con.commit()
    con.close()
    r = import_codex_history(store, db)
    assert r["configured"] is True
    assert "no such column" in r["error"]
    assert r["imported"] == 0


def test_missing_db_is_still_plain_not_configured(store, tmp_path):
    r = import_codex_history(store, tmp_path / "nope.sqlite")
    assert r["configured"] is False and "error" not in r
