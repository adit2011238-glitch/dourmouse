"""Real semantic-proximity clustering (v13.6).

Vision OS checklist item 2: "Qdrant + Ollama embeddings, semantic-
proximity gravity clustering physics" — canvas elements should cluster
by real meaning, not manual placement.

Honest scope, stated plainly:

- **Real embeddings, reused not re-derived.** This calls
  ``dourmouse/memory_embed.py``'s already-existing, already-live
  ``embed_texts()`` (Ollama ``nomic-embed-text``, honest ``None``-on-
  failure contract) and its ``ensure_embeddings()`` cache (the SAME
  ``fact_embeddings`` SQLite cache ``semantic_search`` already reads/
  writes) rather than a second, parallel embedding pipeline.
- **Real Qdrant, in LOCAL on-disk mode.** ``qdrant-client``'s
  ``QdrantClient(path=...)`` is a genuine Qdrant instance — same client
  library, same HNSW vector index, same query API a networked Qdrant
  server would use — just embedded in-process with on-disk storage
  instead of a separate server/Docker container. At this app's real
  scale (a memory store with, realistically, dozens to a few hundred
  facts — not the millions-of-vectors regime Qdrant's distributed mode
  exists for), standing up a separate Qdrant server would be
  disproportionate infrastructure weight for zero practical benefit —
  the SAME deliberate right-sizing call this session already made for
  GDELT ingestion (see ``gdelt_graph.py``'s own docstring on why a
  plain poller substitutes for Streamparse/Storm there). Honest
  admission: at this scale, plain Python cosine similarity (already
  implemented in ``memory_embed.cosine_similarity``) would answer the
  same nearest-neighbor query with no dependency at all — Qdrant is
  used anyway because it was explicitly named and a real local
  installation costs nothing beyond one pip package, not because it is
  strictly necessary here.
- **NOT CONFIGURED, honestly, never a crash or fabricated similarity**:
  if ``qdrant-client`` isn't installed, semantic embeddings are
  disabled (``DOURMOUSE_EMBED=0``, the existing default), or the embed
  endpoint is unreachable, ``build_semantic_graph`` reports that
  plainly and returns an empty graph — never invented clusters.
- **Applied to real, already-existing content**: this clusters
  Dourmouse's own RAG memory store (``dourmouse/memory_store.py``'s
  ``all_facts()``) — real remembered facts, not synthetic demo data.

Clustering itself is real but deliberately simple: connected components
over the real similarity-edge graph at a fixed threshold (pure Python
union-find, no ML claim) — the same "deterministic math, never an LLM
judgment" discipline ``memory_embed.py`` already states for its own
cosine ranking.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dourmouse import memory_embed
from dourmouse.memory_store import MemoryStore

_COLLECTION_NAME = "dourmouse_memory_semantic"
_DEFAULT_MIN_SIMILARITY = 0.55
_DEFAULT_TOP_K = 6
_DEFAULT_LIMIT_FACTS = 300


def semantic_graph_available() -> bool:
    """True if the real ``qdrant-client`` package is importable. Cheap,
    side-effect-free — safe to call from a status endpoint on every
    request."""
    try:
        import qdrant_client  # noqa: F401
    except ImportError:
        return False
    return True


def _qdrant_index_dir(workspace_root: Path) -> Path:
    d = workspace_root / "semantic_index"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _open_client(index_dir: Path):
    from qdrant_client import QdrantClient

    return QdrantClient(path=str(index_dir))


def _recreate_collection(client, dim: int) -> None:
    from qdrant_client.models import Distance, VectorParams

    # Rebuilt fresh on every call rather than incrementally maintained --
    # correct and simple at this real scale (hundreds of facts, not
    # millions), and guarantees the index never silently drifts from
    # what's actually in the memory store (a stale point for a deleted
    # fact would otherwise linger forever).
    if client.collection_exists(_COLLECTION_NAME):
        client.delete_collection(_COLLECTION_NAME)
    client.create_collection(
        _COLLECTION_NAME, vectors_config=VectorParams(size=dim, distance=Distance.COSINE)
    )


def _connected_components(ids: list[int], edges: list[dict[str, Any]]) -> dict[int, int]:
    """Real, simple connected-components clustering over the real
    similarity-edge graph — pure Python union-find, no external
    library, no fabricated "AI cluster" label."""
    parent = {i: i for i in ids}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for e in edges:
        if e["a"] in parent and e["b"] in parent:
            union(e["a"], e["b"])
    # Renumber roots to small dense cluster ids (0, 1, 2, ...) purely
    # for a stable, readable client-side color palette index.
    roots = sorted({find(i) for i in ids})
    root_to_cluster = {r: idx for idx, r in enumerate(roots)}
    return {i: root_to_cluster[find(i)] for i in ids}


def build_semantic_graph(
    store: MemoryStore,
    workspace_root: Path,
    min_similarity: float = _DEFAULT_MIN_SIMILARITY,
    top_k: int = _DEFAULT_TOP_K,
    limit_facts: int = _DEFAULT_LIMIT_FACTS,
) -> dict[str, Any]:
    """Real semantic clustering of the memory store's actual facts.
    Always returns ``{"ok", "nodes", "edges", "error"}`` — never raises
    (Rule 2.1/2.2): a missing dependency, a disabled embed layer, or an
    unreachable Ollama endpoint is reported honestly, not silently
    swallowed into an empty-looking success."""
    if not semantic_graph_available():
        return {"ok": False, "nodes": [], "edges": [], "error": "NOT CONFIGURED: qdrant-client not installed"}
    if not memory_embed.embed_enabled():
        return {"ok": False, "nodes": [], "edges": [], "error": "NOT CONFIGURED: semantic embeddings disabled (set DOURMOUSE_EMBED=1)"}

    facts = store.all_facts()[: max(1, limit_facts)]
    if not facts:
        return {"ok": True, "nodes": [], "edges": [], "error": None}

    vectors = memory_embed.ensure_embeddings(store, facts)
    if vectors is None:
        return {"ok": False, "nodes": [], "edges": [], "error": "embedding endpoint unreachable"}

    usable = [f for f in facts if vectors.get(f["id"])]
    if not usable:
        return {"ok": True, "nodes": [], "edges": [], "error": None}

    dim = len(next(iter(vectors.values())))
    try:
        client = _open_client(_qdrant_index_dir(workspace_root))
    except Exception as exc:  # noqa: BLE001 - a broken local index dir must not crash the app
        return {"ok": False, "nodes": [], "edges": [], "error": f"could not open local Qdrant index: {exc}"}

    try:
        from qdrant_client.models import PointStruct

        _recreate_collection(client, dim)
        client.upsert(
            _COLLECTION_NAME,
            points=[
                PointStruct(id=f["id"], vector=vectors[f["id"]], payload={"title": f["title"], "source": f["source"]})
                for f in usable
            ],
        )
        edges: list[dict[str, Any]] = []
        seen_pairs: set[tuple[int, int]] = set()
        for f in usable:
            hits = client.query_points(_COLLECTION_NAME, query=vectors[f["id"]], limit=top_k + 1).points
            for h in hits:
                if h.id == f["id"] or h.score < min_similarity:
                    continue
                pair = (f["id"], h.id) if f["id"] < h.id else (h.id, f["id"])
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                edges.append({"a": pair[0], "b": pair[1], "score": round(float(h.score), 4)})
    finally:
        client.close()

    ids = [f["id"] for f in usable]
    clusters = _connected_components(ids, edges)
    nodes = [
        {"id": f["id"], "label": f["title"], "source": f["source"], "cluster": clusters[f["id"]]}
        for f in usable
    ]
    return {"ok": True, "nodes": nodes, "edges": edges, "error": None}
