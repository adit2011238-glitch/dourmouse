"""dourmouse/semantic_graph.py — real semantic-proximity clustering
(Vision OS checklist item 2's real subset: Qdrant local mode + the
already-existing Ollama embedding cache). See that module's own
docstring for what's real (a genuine local Qdrant instance, real
embeddings via memory_embed.py) vs. the honest note that plain cosine
would suffice at this app's real scale.

The Qdrant-dependent tests use a REAL local on-disk QdrantClient (no
mocking of the vector index itself — same discipline as
test_pdf_reader.py's real PDFium calls) but monkeypatch
``memory_embed.embed_texts`` to deterministic hand-picked vectors
instead of hitting a real Ollama endpoint, so this suite stays
hermetic and offline (Rules 2.1/2.8) while still exercising real
vector search and real connected-components clustering.
"""

from __future__ import annotations

import pytest

qdrant_client = pytest.importorskip("qdrant_client")

from dourmouse import memory_embed, semantic_graph
from dourmouse.memory_store import MemoryStore


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "mem.db")
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _embed_on(monkeypatch):
    monkeypatch.setenv("DOURMOUSE_EMBED", "1")


def _fake_vectors(clusters: dict[str, list[float]]):
    """A fake embed_texts() that returns a fixed vector per real fact
    body — keyed by exact body text so a test controls exactly which
    facts land near each other, without depending on a real Ollama
    endpoint or a real embedding model's actual output."""

    def _fake(texts, timeout=10.0):
        return [clusters[t] for t in texts]

    return _fake


class TestAvailability:
    def test_real_qdrant_client_is_available_in_this_env(self):
        # This suite only runs at all when qdrant_client imports (see the
        # importorskip above) -- so the real availability check must agree.
        assert semantic_graph.semantic_graph_available() is True

    def test_reports_false_when_the_import_itself_fails(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def _fail(name, *a, **k):
            if name == "qdrant_client":
                raise ImportError("simulated missing dependency")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", _fail)
        assert semantic_graph.semantic_graph_available() is False


class TestHonestDegradation:
    def test_not_configured_when_qdrant_unavailable(self, store, tmp_path, monkeypatch):
        monkeypatch.setattr(semantic_graph, "semantic_graph_available", lambda: False)
        result = semantic_graph.build_semantic_graph(store, tmp_path)
        assert result == {"ok": False, "nodes": [], "edges": [], "error": "NOT CONFIGURED: qdrant-client not installed"}

    def test_not_configured_when_embed_disabled(self, store, tmp_path, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_EMBED", "0")
        result = semantic_graph.build_semantic_graph(store, tmp_path)
        assert result["ok"] is False
        assert "DOURMOUSE_EMBED" in result["error"]

    def test_empty_store_is_an_honest_empty_success(self, store, tmp_path):
        result = semantic_graph.build_semantic_graph(store, tmp_path)
        assert result == {"ok": True, "nodes": [], "edges": [], "error": None}

    def test_embed_endpoint_down_is_honest_not_a_crash(self, store, tmp_path, monkeypatch):
        store.remember("test", "fact one", "body one")
        monkeypatch.setattr(memory_embed, "embed_texts", lambda texts, timeout=10.0: None)
        result = semantic_graph.build_semantic_graph(store, tmp_path)
        assert result == {"ok": False, "nodes": [], "edges": [], "error": "embedding endpoint unreachable"}


class TestRealClustering:
    def test_two_distinct_clusters_from_real_vector_similarity(self, store, tmp_path, monkeypatch):
        # Two near-identical vectors (group A) and two more near-identical
        # but ORTHOGONAL vectors (group B) -- a real Qdrant nearest-
        # neighbor search should link each pair within its group and NOT
        # across groups, and real union-find should produce exactly 2
        # clusters.
        store.remember("test", "a1", "alpha one")
        store.remember("test", "a2", "alpha two")
        store.remember("test", "b1", "beta one")
        store.remember("test", "b2", "beta two")
        vectors = {
            "alpha one": [1.0, 0.01, 0.0],
            "alpha two": [0.99, 0.02, 0.0],
            "beta one": [0.0, 0.0, 1.0],
            "beta two": [0.01, 0.0, 0.98],
        }
        monkeypatch.setattr(memory_embed, "embed_texts", _fake_vectors(vectors))

        result = semantic_graph.build_semantic_graph(store, tmp_path, min_similarity=0.5)
        assert result["ok"] is True
        assert len(result["nodes"]) == 4
        by_label = {n["label"]: n["cluster"] for n in result["nodes"]}
        assert by_label["a1"] == by_label["a2"]
        assert by_label["b1"] == by_label["b2"]
        assert by_label["a1"] != by_label["b1"]
        # At least one real edge within each group, none across groups.
        edge_labels = {
            frozenset((next(n["label"] for n in result["nodes"] if n["id"] == e["a"]),
                       next(n["label"] for n in result["nodes"] if n["id"] == e["b"])))
            for e in result["edges"]
        }
        assert frozenset(("a1", "a2")) in edge_labels
        assert frozenset(("b1", "b2")) in edge_labels
        assert frozenset(("a1", "b1")) not in edge_labels

    def test_threshold_controls_whether_weakly_similar_facts_link(self, store, tmp_path, monkeypatch):
        store.remember("test", "x", "fact x")
        store.remember("test", "y", "fact y")
        vectors = {"fact x": [1.0, 0.0], "fact y": [0.7, 0.7]}  # cos sim ~0.7
        monkeypatch.setattr(memory_embed, "embed_texts", _fake_vectors(vectors))

        strict = semantic_graph.build_semantic_graph(store, tmp_path, min_similarity=0.9)
        assert strict["edges"] == []
        loose = semantic_graph.build_semantic_graph(store, tmp_path, min_similarity=0.5)
        assert len(loose["edges"]) == 1

    def test_embeddings_are_cached_not_refetched_on_second_call(self, store, tmp_path, monkeypatch):
        store.remember("test", "only", "only fact")
        calls = []

        def _counting_fake(texts, timeout=10.0):
            calls.append(list(texts))
            return [[1.0, 0.0]] * len(texts)

        monkeypatch.setattr(memory_embed, "embed_texts", _counting_fake)
        semantic_graph.build_semantic_graph(store, tmp_path)
        semantic_graph.build_semantic_graph(store, tmp_path)
        assert len(calls) == 1  # second call reused the fact_embeddings cache

    def test_limit_facts_caps_how_many_are_indexed(self, store, tmp_path, monkeypatch):
        for i in range(5):
            store.remember("test", f"f{i}", f"body {i}")
        monkeypatch.setattr(memory_embed, "embed_texts", lambda texts, timeout=10.0: [[1.0, float(i)] for i in range(len(texts))])
        result = semantic_graph.build_semantic_graph(store, tmp_path, limit_facts=2)
        assert len(result["nodes"]) == 2


class TestConnectedComponents:
    def test_pure_function_real_union_find(self):
        edges = [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
        clusters = semantic_graph._connected_components([1, 2, 3, 4, 5], edges)
        assert clusters[1] == clusters[2]
        assert clusters[3] == clusters[4]
        assert clusters[1] != clusters[3]
        assert clusters[5] not in (clusters[1], clusters[3])  # isolated node, own cluster

    def test_no_edges_gives_every_node_its_own_cluster(self):
        clusters = semantic_graph._connected_components([1, 2, 3], [])
        assert len({clusters[1], clusters[2], clusters[3]}) == 3

    def test_chain_of_edges_transitively_joins(self):
        edges = [{"a": 1, "b": 2}, {"a": 2, "b": 3}]
        clusters = semantic_graph._connected_components([1, 2, 3], edges)
        assert clusters[1] == clusters[2] == clusters[3]
