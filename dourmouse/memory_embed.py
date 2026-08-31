"""Semantic memory recall (v4.1, P6) — vector similarity OVER the FTS5 store.

Layers optional local embeddings (Ollama ``nomic-embed-text``) on top of the
existing SQLite FTS5 long-term store: FTS5 stays the zero-dependency primary,
and semantic recall is a strictly-better fallback chain — if embeddings are
disabled, the model is missing, or the endpoint fails, ``semantic_search``
honestly returns the FTS5 path (Rules 2.2 / 2.8). Nothing is fabricated:
similarity is deterministic cosine math over real stored bodies, never an LLM
judgment. Embeddings are cached in ``fact_embeddings`` so recall is O(n) dot
products after the first pass.

Env gates:
- ``DOURMOUSE_EMBED=1``   -> enable the semantic layer (default off).
- ``DOURMOUSE_EMBED_MODEL`` -> Ollama embed model (default ``nomic-embed-text``).
- ``OLLAMA_BASE_URL``     -> Ollama root (default ``http://127.0.0.1:11434``).
"""

from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.request
from typing import Any

from dourmouse.learn import distill_query
from dourmouse.memory_store import MemoryStore

_EMBED_ENV = "DOURMOUSE_EMBED"
_EMBED_MODEL_ENV = "DOURMOUSE_EMBED_MODEL"
_OLLAMA_BASE_ENV = "OLLAMA_BASE_URL"

_DEFAULT_EMBED_MODEL = "nomic-embed-text"
_DEFAULT_OLLAMA_ROOT = "http://127.0.0.1:11434"
_OFF_VALUES = {"", "0", "false", "no", "off"}


def embed_enabled(value: str | None = None) -> bool:
    """``DOURMOUSE_EMBED`` gate; default OFF (FTS5 remains the primary)."""
    if value is None:
        value = os.environ.get(_EMBED_ENV, "0")
    return str(value).strip().lower() not in _OFF_VALUES


def embed_model() -> str:
    """The active Ollama embedding model (deterministic, Rule 2.8)."""
    raw = os.environ.get(_EMBED_MODEL_ENV, _DEFAULT_EMBED_MODEL)
    return (raw or "").strip() or _DEFAULT_EMBED_MODEL


def _ollama_root() -> str:
    """Ollama base root; tolerates a trailing ``/v1`` OpenAI-style suffix."""
    raw = os.environ.get(_OLLAMA_BASE_ENV, _DEFAULT_OLLAMA_ROOT).strip()
    return raw.removesuffix("/v1") or _DEFAULT_OLLAMA_ROOT


def embed_texts(texts: list[str], timeout: float = 10.0) -> list[list[float]] | None:
    """Embed texts via the local Ollama embeddings endpoint; None on failure.

    Deterministic and stdlib-only (``urllib``). Returns vectors aligned with
    the input list, or ``None`` when the endpoint is unreachable or returns
    garbage — the caller then falls back to FTS5 honestly. Never raises
    (Rule 2.2): an embed outage must not take down recall.
    """
    clean = [t for t in (texts or []) if isinstance(t, str) and t]
    if not clean:
        return []
    url = f"{_ollama_root()}/api/embeddings"
    out: list[list[float]] = []
    for text in clean:
        payload = json.dumps(
            {"model": embed_model(), "prompt": text[:8000]}
        ).encode()
        try:
            req = urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
        except (urllib.error.URLError, OSError, TimeoutError, ValueError, json.JSONDecodeError):
            return None
        vec = data.get("embedding") if isinstance(data, dict) else None
        if not isinstance(vec, list) or not vec:
            return None
        out.append([float(x) for x in vec])
    return out


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity, pure Python (stdlib only). 0.0 for empty/mismatched."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _fts_hits(store: MemoryStore, query: str, limit: int) -> list[dict[str, Any]]:
    """FTS5 recall for the fallback path. The raw conversational query is
    distilled to its distinctive terms first (same deterministic stopword
    pass the learn loop uses) so "why did we change the risk parameters"
    recalls on [risk, parameters] instead of AND-matching every word."""
    distilled = distill_query(query)
    if not distilled:
        return []
    return store.search(distilled, limit=limit)


def ensure_embeddings(
    store: MemoryStore, facts: list[dict[str, Any]]
) -> dict[int, list[float]] | None:
    """Real embeddings for every given fact, using the SAME cache
    (``fact_embeddings``, via ``store.get_embeddings``/``save_embedding``)
    ``semantic_search`` already reads/writes. Factored out (v13.6) so a
    second real caller — ``semantic_graph.py``'s clustering, which needs
    embeddings for potentially every stored fact, not just the top
    recall hits — shares ONE implementation instead of a second,
    potentially-diverging copy of the same missing-batch logic.

    Returns the full ``{fact_id: vector}`` map (already-cached vectors
    plus any freshly embedded ones), or ``None`` — never a partial or
    fabricated result — if the embed endpoint fails for any batch of
    missing facts.
    """
    vectors = store.get_embeddings()
    missing = [f for f in facts if f["id"] not in vectors]
    if missing:
        batch = embed_texts([f["body"] for f in missing])
        if batch is None or len(batch) != len(missing):
            return None
        for fact, vec in zip(missing, batch):
            store.save_embedding(fact["id"], embed_model(), vec)
            vectors[fact["id"]] = vec
    return vectors


def semantic_search(
    store: MemoryStore, query: str, limit: int = 5
) -> dict[str, Any]:
    """Vector recall over the store with an honest FTS5 fallback chain.

    Returns ``{"method": "semantic" | "fts5", "hits": [...]}``. Each hit is
    ``{source, title, snippet, score}``. The method is always reported so the
    agent (and the user) knows exactly what retrieval produced the results.

    Fallback order (never an error, never fabricated):
    1. gate off / empty query / no facts -> FTS5;
    2. embed endpoint down or garbage     -> FTS5;
    3. semantic best score below 0.1      -> FTS5 (nothing related enough).
    """
    query = (query or "").strip()
    limit = max(1, min(int(limit), 20))
    if not query:
        return {"method": "fts5", "hits": []}

    if not embed_enabled():
        return {"method": "fts5", "hits": _fts_hits(store, query, limit)}

    facts = store.all_facts()
    if not facts:
        return {"method": "fts5", "hits": []}

    vectors = ensure_embeddings(store, facts)
    if vectors is None:
        return {"method": "fts5", "hits": _fts_hits(store, query, limit)}

    qvecs = embed_texts([query])
    if qvecs is None:
        return {"method": "fts5", "hits": _fts_hits(store, query, limit)}
    qvec = qvecs[0]

    scored: list[tuple[float, dict[str, Any]]] = []
    for f in facts:
        vec = vectors.get(f["id"])
        if not vec:
            continue
        sim = cosine_similarity(qvec, vec)
        body = (f["body"] or "").replace("\n", " ")
        snippet = body[:160] + ("…" if len(body) > 160 else "")
        scored.append(
            (
                sim,
                {
                    "source": f["source"],
                    "title": f["title"],
                    "snippet": snippet,
                    "score": round(sim, 4),
                },
            )
        )
    scored.sort(key=lambda t: t[0], reverse=True)
    hits = [h for _, h in scored[:limit]]
    if not hits or hits[0]["score"] < 0.1:
        return {"method": "fts5", "hits": _fts_hits(store, query, limit)}
    return {"method": "semantic", "hits": hits}
