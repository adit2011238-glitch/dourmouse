"""FD2 fixes for shared_rag: P5-58 (never raises, non-integer ids), P5-59 (absolute scores)."""

from __future__ import annotations

import math
import sqlite3
import sys

import pytest

from dourmouse import shared_rag
from dourmouse.shared_rag import merged_search, query_spatial_vault
from dourmouse.tests.test_shared_rag import _FakeFaissIndex, _FakeFaissModule, _make_db


def _setup_vault(tmp_path, monkeypatch, rows, hits, metric, d=2):
    db = tmp_path / "vault.db"
    _make_db(db, rows=rows)
    (tmp_path / "vector.index").write_bytes(b"placeholder")
    monkeypatch.setenv("DOURMOUSE_SPATIAL_VAULT_PATH", str(db))
    idx = _FakeFaissIndex(d=d, metric_type=metric, ordered_hits=hits, ntotal=len(rows))
    monkeypatch.setitem(sys.modules, "faiss", _FakeFaissModule(idx))
    monkeypatch.setattr(shared_rag, "EMBED_DIM", d)
    monkeypatch.setattr(shared_rag, "embed_text", lambda q: [0.1] * d)
    monkeypatch.delenv("DOURMOUSE_GLOBAL_MEMORY", raising=False)
    return db


# --- P5-58 ----------------------------------------------------------------- #
def test_uuid_text_ids_do_not_crash_the_id_map(tmp_path, monkeypatch):
    db = tmp_path / "vault.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE chunks (id TEXT PRIMARY KEY, text TEXT)")
    conn.execute("INSERT INTO chunks VALUES ('aaaa-1111', 'first')")
    conn.execute("INSERT INTO chunks VALUES ('bbbb-2222', 'second')")
    conn.commit()
    conn.close()
    (tmp_path / "vector.index").write_bytes(b"placeholder")
    monkeypatch.setenv("DOURMOUSE_SPATIAL_VAULT_PATH", str(db))
    idx = _FakeFaissIndex(d=2, metric_type=0, ordered_hits=[(0.9, 1), (0.4, 0)], ntotal=2)
    monkeypatch.setitem(sys.modules, "faiss", _FakeFaissModule(idx))
    monkeypatch.setattr(shared_rag, "EMBED_DIM", 2)
    monkeypatch.setattr(shared_rag, "embed_text", lambda q: [0.1, 0.2])
    hits = query_spatial_vault("q", top_k=5)
    assert [h["id"] for h in hits] == ["bbbb-2222", "aaaa-1111"]


def test_bad_filter_sql_becomes_a_warning_not_an_exception(tmp_path, monkeypatch):
    _setup_vault(tmp_path, monkeypatch, [(1, "a")], [(0.9, 0)], 0)
    monkeypatch.setenv("DOURMOUSE_SPATIAL_VAULT_ID_FILTER_SQL", "no_such_column = 1")
    result = merged_search("q")  # must not raise
    assert result.hits == []
    assert len(result.warnings) == 1
    assert "spatial_vault" not in result.sources_used


def test_unexpected_vault_exception_becomes_a_warning(tmp_path, monkeypatch):
    _setup_vault(tmp_path, monkeypatch, [(1, "a")], [(0.9, 0)], 0)

    def boom(*a, **k):
        raise ValueError("surprise")

    monkeypatch.setattr(shared_rag, "query_spatial_vault", boom)
    result = merged_search("q")
    assert any("surprise" in w for w in result.warnings)


def test_local_search_failure_becomes_a_warning_and_vault_still_consulted(tmp_path, monkeypatch):
    _setup_vault(tmp_path, monkeypatch, [(1, "vault text")], [(0.9, 0)], 0)
    monkeypatch.setenv("DOURMOUSE_GLOBAL_MEMORY", "1")

    class BadMem:
        def search(self, query, top_k=5):
            raise RuntimeError("local store locked")

    result = merged_search("q", memory=BadMem())
    assert any("local store locked" in w for w in result.warnings)
    assert result.sources_used == ["spatial_vault"]
    assert [h["text"] for h in result.hits] == ["vault text"]


# --- P5-59 ----------------------------------------------------------------- #
def test_lone_inner_product_hit_keeps_its_absolute_score(tmp_path, monkeypatch):
    _setup_vault(tmp_path, monkeypatch, [(1, "a")], [(0.83, 0)], 0)
    hits = query_spatial_vault("q")
    assert hits[0]["score"] == pytest.approx(0.83, abs=1e-6)


def test_lone_l2_hit_is_not_pinned_to_one(tmp_path, monkeypatch):
    _setup_vault(tmp_path, monkeypatch, [(1, "a")], [(0.5, 0)], 1)
    hits = query_spatial_vault("q")
    assert hits[0]["score"] == pytest.approx(math.exp(-0.5), abs=1e-6)
    assert hits[0]["score"] < 1.0


def test_poor_top_hit_is_not_rescaled_to_one(tmp_path, monkeypatch):
    _setup_vault(tmp_path, monkeypatch, [(1, "a"), (2, "b")], [(0.2, 0), (0.1, 1)], 0)
    hits = query_spatial_vault("q")
    assert hits[0]["score"] == pytest.approx(0.2, abs=1e-6)
    assert hits[1]["score"] == pytest.approx(0.1, abs=1e-6)


def test_score_is_clamped_into_unit_range(tmp_path, monkeypatch):
    _setup_vault(tmp_path, monkeypatch, [(1, "a"), (2, "b")], [(1.7, 0), (-0.3, 1)], 0)
    hits = query_spatial_vault("q")
    assert [h["score"] for h in hits] == [1.0, 0.0]
