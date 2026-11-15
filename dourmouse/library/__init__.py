"""The library: the user wiki plus one store per project (plan 2.1, finding 206).

* ``wiki()``: ``<workspace>/library/wiki.db``, everything about the owner (remembered facts, consented folders,
  saved pages, paper cards).
* ``project(id)``: ``<workspace>/library/projects/<id>.db``, one project's files, instructions, memory and thread
  summaries. No call here reads a project other than the one it is given.
* ``search(query, project=...)``: that project's store first, then the wiki. Results say which store they came from.
* ``cite(ref)`` and ``verify_quote(ref, quote)``: a passage is named by a ref, ``wiki:12`` or ``project:<id>:12``,
  because passage numbers are per file.

Embeddings are plan 2.3; here the vector table, the queue and the privacy gate exist and are tested with a stub.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .chunking import Chunk, chunk_text, page_marker
from .embedder import DIM, Embedder, HashEmbedder
from .gate import holds_secret, may_embed
from .store import Store, normalise_quote

__all__ = ["Chunk", "DIM", "Embedder", "HashEmbedder", "Store", "chunk_text", "cite", "holds_secret", "may_embed",
           "normalise_quote", "page_marker", "project", "project_store", "search", "verify_quote", "wiki", "library_dir"]

_PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{2,60}$")


def library_dir() -> Path:
    from dourmouse.config import workspace_dir

    return Path(workspace_dir()) / "library"


def wiki() -> Store:
    return Store(library_dir() / "wiki.db", kind="wiki")


def project_store(project_id: str) -> Store:
    """This project's store. The id is checked, so no path outside the projects folder can be built."""
    if not isinstance(project_id, str) or not _PROJECT_ID.match(project_id):
        raise LookupError("no such project")
    return Store(library_dir() / "projects" / f"{project_id}.db", kind="project")


project = project_store


def _parse_ref(ref: str) -> tuple[Store, int]:
    parts = str(ref or "").split(":")
    try:
        if len(parts) == 2 and parts[0] == "wiki":
            return wiki(), int(parts[1])
        if len(parts) == 3 and parts[0] == "project":
            return project_store(parts[1]), int(parts[2])
    except ValueError:
        pass
    raise LookupError("no such passage")


def search(query: str, *, project: str = "", limit: int = 8, filters: dict[str, Any] | None = None,
           embedder: Any = None, privacy_store: Any = None) -> dict[str, Any]:
    """Search a project's store first, then the wiki, and return ``{"results", "mode", "note"}``.

    With an ``embedder`` the query is embedded and the vector ranking is fused with the keyword ranking. A remote
    embedder is used only if the privacy gate allows it (Private mode off); otherwise, and when nothing is embedded
    yet, the search is keyword-only and ``note`` says why."""
    from . import gate

    limit = max(1, min(int(limit), 50))
    qvec: list[float] | None = None
    model = ""
    note = ""
    mode = "keyword"
    if embedder is not None:
        ok, why = gate.may_embed({"text": "", "tier": "T1", "folder": ""},
                                 remote=bool(getattr(embedder, "remote", True)), privacy_store=privacy_store)
        if ok:
            try:
                qvec = embedder.embed_query(query)
                model = str(getattr(embedder, "model", ""))
                mode = "hybrid"
            except Exception as exc:  # noqa: BLE001 - any embedder failure falls back to keywords, said plainly
                note = f"Searched by keywords only: the embedding service failed ({type(exc).__name__})."
        else:
            note = f"Searched by keywords only: {why}."
    stores: list[tuple[str, Store]] = []
    if project:
        stores.append(("project", project_store(project)))
    stores.append(("wiki", wiki()))
    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    for label, st in stores:
        for hit in st.search(query, limit, filters=filters, query_vector=qvec, model=model):
            key = (hit["source"], hit["source_id"], hit["passage"])
            if key in seen:
                continue
            seen.add(key)
            hit["store"] = label
            hit["ref"] = f"wiki:{hit['passage_id']}" if label == "wiki" else f"project:{project}:{hit['passage_id']}"
            results.append(hit)
            if len(results) >= limit:
                break
        if len(results) >= limit:
            break
    if qvec is not None and not any("vector" in h["matched"] for h in results) and results:
        note = note or "Nothing in these stores has an embedding yet, so the order is by keywords."
        mode = "keyword"
    return {"results": results, "mode": mode, "note": note}


def cite(ref: str) -> dict[str, Any]:
    store, pid = _parse_ref(ref)
    out = store.cite(pid)
    out["ref"] = ref
    return out


def verify_quote(ref: str, quote: str) -> dict[str, Any]:
    store, pid = _parse_ref(ref)
    out = store.verify_quote(pid, quote)
    out["ref"] = ref
    return out
