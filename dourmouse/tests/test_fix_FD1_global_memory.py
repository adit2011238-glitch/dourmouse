"""Fix agent FD1: P5-19 (non-object corpus entry crashes the ingest) and P5-20 (a wrong-dimension vector
disables every search)."""

from __future__ import annotations

import json

import pytest

from dourmouse import global_memory as gm


@pytest.fixture
def mem(tmp_path):
    m = gm.GlobalMemory(tmp_path / "g.db")
    yield m
    m.close()


def test_non_object_entry_is_rejected_and_the_rest_are_still_ingested(tmp_path, mem, monkeypatch):
    monkeypatch.setattr(gm, "embed_text", lambda text, **k: [1.0, 0.0])
    corpus = tmp_path / "c.json"
    corpus.write_text(json.dumps([{"id": "a", "text": "x"}, "stray string", 7, {"id": "b", "text": "y"}]))
    out = gm.ingest_corpus_file(corpus, memory=mem)
    assert out["accepted"] == 2
    assert len(out["rejected"]) == 2
    assert all(r["reason"] == "entry is not an object" for r in out["rejected"])
    assert out["total"] == 4


def test_search_skips_rows_of_another_dimension_instead_of_raising(mem, monkeypatch):
    monkeypatch.setattr(gm, "embed_text", lambda text, **k: [1.0, 0.0, 0.0])
    assert mem.add("good three dim", screen="s")
    # a row left behind by an older embedding model (2 dims)
    mem._conn.execute("INSERT INTO memory VALUES ('old','old text','s','','{}',?,0)", (json.dumps([1.0, 0.0]),))
    mem._conn.commit()
    hits = mem.search("anything")
    assert [h["id"] for h in hits if h["text"] == "good three dim"]
    assert all(h["id"] != "old" for h in hits)
    assert mem.last_search_skipped == 1


def test_search_skips_non_list_embeddings(mem, monkeypatch):
    monkeypatch.setattr(gm, "embed_text", lambda text, **k: [1.0, 0.0])
    mem._conn.execute("INSERT INTO memory VALUES ('bad','t','s','','{}',?,0)", (json.dumps({"x": 1}),))
    mem._conn.commit()
    assert mem.search("q") == []
    assert mem.last_search_skipped == 1


def test_add_refuses_a_vector_of_a_different_dimension(mem, monkeypatch):
    monkeypatch.setattr(gm, "embed_text", lambda text, **k: [1.0, 0.0, 0.0])
    assert mem.add("first", screen="s")
    assert mem.add("wrong", screen="s", vector=[1.0, 0.0]) is False
    assert mem._conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0] == 1


def test_add_refuses_a_non_numeric_or_empty_vector(mem):
    assert mem.add("t", vector=[]) is False
    assert mem.add("t", vector=["a", "b"]) is False


def test_one_row_with_corrupt_metadata_does_not_disable_search(mem, monkeypatch):
    monkeypatch.setattr(gm, "embed_text", lambda text, **k: [1.0, 0.0])
    assert mem.add("good row", screen="s")
    mem._conn.execute("INSERT INTO memory VALUES ('bad','bad row','s','','{not json',?,0)", (json.dumps([1.0, 0.0]),))
    mem._conn.commit()
    hits = mem.search("anything")
    assert {h["id"] for h in hits} >= {"bad"} or any(h["text"] == "good row" for h in hits)
    assert any(h["text"] == "good row" for h in hits)
    assert next(h for h in hits if h["id"] == "bad")["metadata"] == {}
