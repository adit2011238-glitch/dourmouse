"""FD2 fix P5-56: build_semantic_graph keeps its never-raises contract."""

from __future__ import annotations

import pytest

pytest.importorskip("qdrant_client")

from dourmouse import memory_embed, semantic_graph
from dourmouse.memory_store import MemoryStore


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "mem.db")
    yield s
    s.close()


@pytest.fixture(autouse=True)
def embed_on(monkeypatch):
    monkeypatch.setenv("DOURMOUSE_EMBED", "1")


def _facts(store, n=3):
    for i in range(n):
        store.remember("src", f"fact {i}", f"body {i}")
    return store.all_facts()


def test_first_vector_missing_does_not_crash(store, tmp_path, monkeypatch):
    facts = _facts(store)
    ids = [f["id"] for f in facts]
    vectors = {ids[0]: None, ids[1]: [1.0, 0.0], ids[2]: [1.0, 0.1]}
    monkeypatch.setattr(memory_embed, "ensure_embeddings", lambda s, fs: vectors)
    out = semantic_graph.build_semantic_graph(store, tmp_path)  # must not raise
    assert out["ok"] is True
    assert {n["id"] for n in out["nodes"]} == {ids[1], ids[2]}


def test_empty_first_vector_does_not_crash(store, tmp_path, monkeypatch):
    facts = _facts(store)
    ids = [f["id"] for f in facts]
    vectors = {ids[0]: [], ids[1]: [1.0, 0.0], ids[2]: [0.0, 1.0]}
    monkeypatch.setattr(memory_embed, "ensure_embeddings", lambda s, fs: vectors)
    out = semantic_graph.build_semantic_graph(store, tmp_path)
    assert out["ok"] is True


def test_qdrant_failure_is_an_error_dict_not_an_exception(store, tmp_path, monkeypatch):
    facts = _facts(store)
    vectors = {f["id"]: [1.0, 0.0] for f in facts}
    monkeypatch.setattr(memory_embed, "ensure_embeddings", lambda s, fs: vectors)

    def locked(client, dim):
        raise RuntimeError("Storage folder is already accessed by another instance of Qdrant client")

    monkeypatch.setattr(semantic_graph, "_recreate_collection", locked)
    out = semantic_graph.build_semantic_graph(store, tmp_path)  # must not raise
    assert out["ok"] is False
    assert out["nodes"] == [] and out["edges"] == []
    assert "already accessed" in out["error"]
