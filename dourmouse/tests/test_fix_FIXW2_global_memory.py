"""FIX-W2 R2-3: GlobalMemory must keep recording after the embedding model changes."""
from __future__ import annotations

import json

import pytest

from dourmouse import global_memory as gm


@pytest.fixture
def mem(tmp_path):
    m = gm.GlobalMemory(tmp_path / "g.db")
    yield m
    m.close()


def _embed_as(monkeypatch, model, vec):
    monkeypatch.setattr(gm, "EMBED_MODEL", model)
    monkeypatch.setattr(gm, "embed_text", lambda text, **k: list(vec))


def test_switching_the_embed_model_does_not_freeze_the_store(mem, monkeypatch):
    _embed_as(monkeypatch, "nomic-embed-text", [1.0, 0.0, 0.0])
    assert mem.add("old model row", screen="s")
    _embed_as(monkeypatch, "big-embed", [0.0, 1.0, 0.0, 0.0])
    assert mem.add("new model row", screen="s") is True
    assert mem.add("second new model row", screen="s") is True
    hits = mem.search("anything")
    assert {h["text"] for h in hits} == {"new model row", "second new model row"}


def test_rows_of_another_model_with_the_same_dimension_are_not_mixed_in(mem, monkeypatch):
    _embed_as(monkeypatch, "model-a", [1.0, 0.0])
    assert mem.add("from a", screen="s")
    _embed_as(monkeypatch, "model-b", [1.0, 0.0])
    assert mem.add("from b", screen="s")
    assert [h["text"] for h in mem.search("q")] == ["from b"]
    assert mem.last_search_skipped == 1


def test_a_wrong_sized_vector_for_the_same_model_is_still_refused_and_says_so(mem, monkeypatch, capsys):
    _embed_as(monkeypatch, "m", [1.0, 0.0, 0.0])
    assert mem.add("first", screen="s")
    assert mem.add("wrong", screen="s", vector=[1.0, 0.0]) is False
    assert "GLOBAL_MEMORY" in capsys.readouterr().out
    assert mem._conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0] == 1


def test_legacy_untagged_rows_stay_searchable(mem, monkeypatch):
    _embed_as(monkeypatch, "m", [1.0, 0.0])
    mem._conn.execute("INSERT INTO memory VALUES ('legacy','legacy text','s','','{}',?,0)", (json.dumps([1.0, 0.0]),))
    mem._conn.commit()
    assert [h["id"] for h in mem.search("q")] == ["legacy"]
    assert mem.add("new", screen="s")  # a legacy row of this size does not block a new one
    assert {h["text"] for h in mem.search("q")} == {"legacy text", "new"}


def test_an_old_database_without_the_model_table_is_upgraded(tmp_path, monkeypatch):
    import sqlite3

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE memory (id TEXT PRIMARY KEY, text TEXT NOT NULL, screen TEXT NOT NULL, "
                 "session_id TEXT, metadata TEXT, embedding TEXT NOT NULL, ts REAL NOT NULL)")
    conn.execute("INSERT INTO memory VALUES ('x','t','s','','{}','[1.0, 0.0]',0)")
    conn.commit()
    conn.close()
    m = gm.GlobalMemory(db)
    try:
        _embed_as(monkeypatch, "m", [1.0, 0.0])
        assert m.add("fresh", screen="s")
        assert len(m.search("q")) == 2
    finally:
        m.close()
