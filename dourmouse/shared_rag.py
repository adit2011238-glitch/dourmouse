"""Shared RAG layer — one retrieval surface across the LOCAL embedding
store (``global_memory.py``) and an OPTIONAL external corpus: the desktop's
own growing spatial vault (``hybrid_vault.db`` + ``vector.index``, built by
a separate live process, ``bulletproof_vault.py``, that this codebase does
not own and has never inspected directly).

Real gap this closes: ``global_memory.py`` gives Dourmouse its own local
memory, but every backend (nvidia/deepseek/claude/codex/ollama, and the
qwen/glm/kimi backends in ``cn_backends.py``) still has no single "ask the
shared knowledge base" surface that also reaches the desktop's much larger
vault once this code is deployed there. This module is that surface: a
pluggable "external corpus source" plus a merge function that combines it
with ``global_memory.GlobalMemory`` under the SAME retrieval contract
(same honest-empty-on-nothing, same refuse-on-incompatible-embedding-space
guard) rather than inventing a second, incompatible one.

THIS ENVIRONMENT HAS NO ACCESS to the real vault file. Everything below is
written defensively against an UNKNOWN schema — it probes the actual
SQLite table/column names at connection time (``sqlite3``'s own
``PRAGMA table_info`` / ``sqlite_master``) rather than hardcoding an
assumed shape, and it fails with a clear ``NOT_CONFIGURED`` /
``SCHEMA_MISMATCH`` / ``MISSING_DEPENDENCY`` / ``EMBEDDING_MISMATCH``
message when the real file doesn't match what it expects. It never
silently returns nothing and never fabricates a result (Rule 2.2).

faiss-cpu: NOT currently a Dourmouse dependency (checked ``requirements*.txt``
directly — only ``numpy`` is listed, matching ``global_memory.py``'s own
note that FAISS was deliberately skipped there). The vault's own index file
is named ``vector.index``, which is the conventional FAISS artifact name,
so reading it for real WILL need ``faiss-cpu`` once this code actually runs
on the desktop where the vault lives. Added to ``requirements.txt`` here
(see that file's comment) but imported lazily, inside the one function that
needs it — never at module import time — so a machine without faiss-cpu
installed (this dev machine included) still imports this module, runs
every other code path, and degrades to an honest ``MISSING_DEPENDENCY``
error only if/when a caller actually tries to read a FAISS index.

Embedding-space safety: the vault almost certainly was NOT embedded with
this app's Ollama model (``global_memory.EMBED_MODEL`` /
``global_memory.EMBED_DIM``) — ``bulletproof_vault.py`` is a separate
process this codebase has never inspected. Comparing across two different
embedding spaces is meaningless, not just lower-quality (the exact
rationale ``global_memory.validate_corpus_entry`` already encodes for the
pre-embedded-vector path). Rather than re-deriving that judgment here, this
module REUSES ``validate_corpus_entry`` directly — it builds a probe entry
carrying the vault index's own reported dimensionality and asks
``validate_corpus_entry`` whether that would be accepted. A dimension
mismatch is refused through the exact same guard the rest of the app
already trusts. Dimension match is a NECESSARY but not SUFFICIENT check for
true embedding-space compatibility (two different models can share an
output width) — see ``DOURMOUSE_SPATIAL_VAULT_EMBED_MODEL`` below for the
stronger, opt-in check.
"""

from __future__ import annotations

import dataclasses
import os
import sqlite3
from pathlib import Path
from typing import Any

from dourmouse.global_memory import (
    EMBED_DIM,
    EMBED_MODEL,
    GlobalMemory,
    embed_text,
    get_default_memory,
    global_memory_enabled,
    validate_corpus_entry,
)

__all__ = [
    "ExternalCorpusError",
    "VaultSchema",
    "MergedResult",
    "spatial_vault_configured",
    "probe_vault_schema",
    "query_spatial_vault",
    "merged_search",
    "format_merged_result",
]

# --------------------------------------------------------------------------- #
# Configuration (all env-var driven, all optional — see module docstring)
# --------------------------------------------------------------------------- #

_VAULT_PATH_ENV = "DOURMOUSE_SPATIAL_VAULT_PATH"          # required to enable
_VAULT_INDEX_ENV = "DOURMOUSE_SPATIAL_VAULT_INDEX_PATH"    # optional override
_VAULT_TABLE_ENV = "DOURMOUSE_SPATIAL_VAULT_TABLE"         # optional override
_VAULT_ID_COL_ENV = "DOURMOUSE_SPATIAL_VAULT_ID_COL"        # optional override
_VAULT_TEXT_COL_ENV = "DOURMOUSE_SPATIAL_VAULT_TEXT_COL"    # optional override
_VAULT_META_COL_ENV = "DOURMOUSE_SPATIAL_VAULT_METADATA_COL"  # optional override
_VAULT_EMBED_MODEL_ENV = "DOURMOUSE_SPATIAL_VAULT_EMBED_MODEL"  # optional, informational+strict

# Best-effort default column-name guesses, tried in order against whatever
# PRAGMA table_info actually reports — never assumed to be right, always
# checked (see probe_vault_schema). Sourced from common RAG/vault schema
# conventions, NOT from the real file (never inspected).
_ID_COL_CANDIDATES = ("id", "doc_id", "row_id", "uuid", "vector_id", "chunk_id")
_TEXT_COL_CANDIDATES = ("text", "content", "chunk_text", "chunk", "body", "document")
_META_COL_CANDIDATES = ("metadata", "meta", "tags", "source", "doc_type")

_DEFAULT_INDEX_NAME = "vector.index"


class ExternalCorpusError(RuntimeError):
    """Honest, typed failure for the external corpus path (Rule 2.2 — never
    a silent empty result). ``kind`` is one of NOT_CONFIGURED,
    SCHEMA_MISMATCH, MISSING_DEPENDENCY, EMBEDDING_MISMATCH, INDEX_ERROR,
    EMBED_FAILED — callers can branch on it; ``str(err)`` is always a full
    human-readable message with the ``kind`` prefixed."""

    def __init__(self, kind: str, message: str):
        self.kind = kind
        super().__init__(f"{kind}: {message}")


@dataclasses.dataclass(frozen=True)
class VaultSchema:
    """What ``probe_vault_schema`` actually found — real values read off
    the real file at connection time, never assumed."""

    table: str
    id_col: str
    text_col: str
    metadata_col: str | None
    row_count: int


def spatial_vault_configured() -> bool:
    """Deterministic on/off switch, matching
    ``global_memory.global_memory_enabled``'s own contract: never inferred,
    on only when the path env var is actually set."""
    return bool(os.environ.get(_VAULT_PATH_ENV, "").strip())


def _resolve_paths() -> tuple[Path, Path]:
    raw_db = os.environ.get(_VAULT_PATH_ENV, "").strip()
    if not raw_db:
        raise ExternalCorpusError(
            "NOT_CONFIGURED",
            f"{_VAULT_PATH_ENV} is not set — no external spatial vault to "
            "read. This is expected on any machine other than the desktop "
            "where hybrid_vault.db actually lives.",
        )
    db_path = Path(raw_db).expanduser()
    raw_index = os.environ.get(_VAULT_INDEX_ENV, "").strip()
    index_path = Path(raw_index).expanduser() if raw_index else db_path.parent / _DEFAULT_INDEX_NAME
    return db_path, index_path


def _pick_column(actual_cols: list[str], override: str, candidates: tuple[str, ...]) -> str | None:
    if override:
        return override if override in actual_cols else None
    lowered = {c.lower(): c for c in actual_cols}
    for cand in candidates:
        if cand in lowered:
            return lowered[cand]
    return None


def probe_vault_schema(db_path: Path) -> VaultSchema:
    """Probe the REAL table/column names in ``db_path`` via ``sqlite3``'s
    own ``PRAGMA table_info`` / ``sqlite_master`` — never a hardcoded
    assumption about ``hybrid_vault.db``'s shape, because this codebase has
    never seen that file. Raises ``ExternalCorpusError`` (kind
    NOT_CONFIGURED or SCHEMA_MISMATCH) with a message naming the REAL
    tables/columns found, so a human can fix it via the override env vars
    rather than this silently guessing wrong.
    """
    if not db_path.is_file():
        raise ExternalCorpusError(
            "NOT_CONFIGURED",
            f"{_VAULT_PATH_ENV} points at {db_path}, which does not exist "
            "(or isn't a file) from this machine. Nothing was read.",
        )
    # Read-only URI connection — this module must never write to a vault
    # it doesn't own, and a live-growing DB may be mid-write from
    # bulletproof_vault.py at any moment.
    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError as exc:
        raise ExternalCorpusError(
            "SCHEMA_MISMATCH", f"could not open {db_path} as a SQLite database: {exc}"
        ) from exc
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        if not tables:
            raise ExternalCorpusError(
                "SCHEMA_MISMATCH", f"{db_path} has no user tables — nothing to query."
            )

        table_override = os.environ.get(_VAULT_TABLE_ENV, "").strip()
        if table_override:
            if table_override not in tables:
                raise ExternalCorpusError(
                    "SCHEMA_MISMATCH",
                    f"{_VAULT_TABLE_ENV}={table_override!r} but that table does not "
                    f"exist in {db_path}. Real tables found: {tables}.",
                )
            table = table_override
        elif len(tables) == 1:
            table = tables[0]
        else:
            raise ExternalCorpusError(
                "SCHEMA_MISMATCH",
                f"{db_path} has {len(tables)} tables and no {_VAULT_TABLE_ENV} "
                f"override was set to disambiguate. Real tables found: {tables}. "
                f"Set {_VAULT_TABLE_ENV} to the one holding the vault's text rows.",
            )

        col_rows = conn.execute(f"PRAGMA table_info({table!r})").fetchall()
        actual_cols = [r[1] for r in col_rows]
        if not actual_cols:
            raise ExternalCorpusError(
                "SCHEMA_MISMATCH", f"table {table!r} in {db_path} reports no columns."
            )

        id_col = _pick_column(actual_cols, os.environ.get(_VAULT_ID_COL_ENV, "").strip(), _ID_COL_CANDIDATES)
        text_col = _pick_column(actual_cols, os.environ.get(_VAULT_TEXT_COL_ENV, "").strip(), _TEXT_COL_CANDIDATES)
        if id_col is None or text_col is None:
            raise ExternalCorpusError(
                "SCHEMA_MISMATCH",
                f"could not resolve an id/text column on table {table!r}. Real "
                f"columns found: {actual_cols}. Tried id candidates "
                f"{_ID_COL_CANDIDATES} and text candidates {_TEXT_COL_CANDIDATES} "
                f"— set {_VAULT_ID_COL_ENV} / {_VAULT_TEXT_COL_ENV} explicitly if "
                "this vault uses different names.",
            )
        metadata_col = _pick_column(
            actual_cols, os.environ.get(_VAULT_META_COL_ENV, "").strip(), _META_COL_CANDIDATES
        )

        row_count = conn.execute(f"SELECT COUNT(*) FROM {table!r}").fetchone()[0]
        return VaultSchema(table=table, id_col=id_col, text_col=text_col, metadata_col=metadata_col, row_count=row_count)
    finally:
        conn.close()


def _load_faiss_index(index_path: Path):
    """Lazily import faiss (see module docstring for why this is lazy and
    why faiss-cpu is being added to requirements.txt rather than assumed
    present). Raises ExternalCorpusError, never crashes the caller."""
    if not index_path.is_file():
        raise ExternalCorpusError(
            "NOT_CONFIGURED",
            f"vector index not found at {index_path} (override with "
            f"{_VAULT_INDEX_ENV} if it isn't a 'vector.index' sibling of the db).",
        )
    try:
        import faiss  # noqa: PLC0415 - deliberately lazy, see module docstring
    except ImportError as exc:
        raise ExternalCorpusError(
            "MISSING_DEPENDENCY",
            "faiss-cpu is not installed in this venv. The vault's "
            f"'{_DEFAULT_INDEX_NAME}' file needs it to be read. Run "
            "`pip install faiss-cpu` (already listed in requirements.txt) "
            "on the machine that actually has the vault.",
        ) from exc
    try:
        return faiss.read_index(str(index_path))
    except Exception as exc:  # noqa: BLE001 - faiss raises its own RuntimeError subtype
        raise ExternalCorpusError(
            "SCHEMA_MISMATCH", f"{index_path} did not load as a FAISS index: {exc}"
        ) from exc


def _check_configured_embed_model() -> None:
    """The strong, opt-in check: an operator who KNOWS what model built the
    vault can record it in DOURMOUSE_SPATIAL_VAULT_EMBED_MODEL, and a
    mismatch is refused before this module even touches the index file.
    Doesn't need the index open, so it runs first."""
    configured_model = os.environ.get(_VAULT_EMBED_MODEL_ENV, "").strip()
    if configured_model and configured_model != EMBED_MODEL:
        raise ExternalCorpusError(
            "EMBEDDING_MISMATCH",
            f"{_VAULT_EMBED_MODEL_ENV}={configured_model!r} but this store's "
            f"embedding model is {EMBED_MODEL!r}. A vector from a different "
            "model lives in an incompatible space — cosine/inner-product "
            "comparison across two spaces is meaningless, not just lower "
            "quality, so this is refused outright.",
        )


def _check_index_dimension(index_dim: int) -> None:
    """Reuses global_memory.validate_corpus_entry rather than re-deriving
    its dimension-mismatch judgment — see module docstring. Needs the
    index already open (to know its real dimensionality), so this runs
    after _load_faiss_index."""
    probe_entry = {"id": "__dim_probe__", "text": "dimension probe", "vector": [0.0] * index_dim}
    problem = validate_corpus_entry(probe_entry, expected_dim=EMBED_DIM)
    if problem:
        raise ExternalCorpusError("EMBEDDING_MISMATCH", problem)


def _metric_is_lower_better(index) -> bool:
    """FAISS's own METRIC_L2 constant marks 'lower is better'; anything else
    (typically METRIC_INNER_PRODUCT) is 'higher is better'. Falls back to
    'higher is better' when the index doesn't expose a recognizable metric
    — documented as a best-effort default, not a certainty, since this
    module has never inspected a real vault index."""
    metric = getattr(index, "metric_type", None)
    if metric is None:
        return False
    try:
        import faiss  # noqa: PLC0415

        return metric == faiss.METRIC_L2
    except ImportError:
        return False


def query_spatial_vault(query: str, *, top_k: int = 5) -> list[dict[str, Any]]:
    """Query the external spatial vault. Raises ``ExternalCorpusError``
    (never returns a fabricated or silently-empty result when something is
    actually wrong) with kind NOT_CONFIGURED / SCHEMA_MISMATCH /
    MISSING_DEPENDENCY / EMBEDDING_MISMATCH / EMBED_FAILED.

    A genuinely empty vault (0 real matches) returns ``[]`` — that IS the
    honest answer in that case, distinct from every error path above.
    """
    import numpy as np

    db_path, index_path = _resolve_paths()
    _check_configured_embed_model()
    schema = probe_vault_schema(db_path)
    index = _load_faiss_index(index_path)
    _check_index_dimension(int(index.d))

    qvec = embed_text(query)
    if qvec is None:
        raise ExternalCorpusError(
            "EMBED_FAILED",
            "could not embed the query text via Ollama (/api/embeddings) — "
            "is Ollama running with the configured model pulled? Nothing "
            "was fabricated.",
        )
    if len(qvec) != int(index.d):
        # Defensive re-check: _check_index_dimension already gates on
        # index.d vs EMBED_DIM, but a live query vector's actual length is
        # the ground truth — trust it over the configured constant if they
        # ever drift.
        raise ExternalCorpusError(
            "EMBEDDING_MISMATCH",
            f"query embedding has {len(qvec)} dimensions but the vault "
            f"index reports {index.d} — refusing to compare across spaces.",
        )

    q = np.array([qvec], dtype="float32")
    distances, indices = index.search(q, max(1, top_k))
    lower_is_better = _metric_is_lower_better(index)

    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    hits: list[dict[str, Any]] = []
    stale = 0
    try:
        cols = [schema.id_col, schema.text_col] + ([schema.metadata_col] if schema.metadata_col else [])
        select_cols = ", ".join(f'"{c}"' for c in cols)
        for raw_score, vault_id in zip(distances[0].tolist(), indices[0].tolist()):
            if vault_id < 0:
                continue  # FAISS's own "no result" sentinel
            row = conn.execute(
                f'SELECT {select_cols} FROM "{schema.table}" WHERE "{schema.id_col}" = ?',
                (vault_id,),
            ).fetchone()
            if row is None:
                stale += 1  # index entry with no matching row — a live-growing vault can go stale between writes
                continue
            text_val = row[1]
            meta_val = row[2] if schema.metadata_col else None
            hits.append({
                "id": str(row[0]),
                "text": text_val,
                "raw_score": float(raw_score),
                "metadata": {"raw": meta_val} if meta_val is not None else {},
            })
    finally:
        conn.close()

    if not hits:
        return []

    raws = [h["raw_score"] for h in hits]
    lo, hi = min(raws), max(raws)
    span = (hi - lo) or 1.0
    for h in hits:
        norm = (h["raw_score"] - lo) / span
        # min-max normalize into 0..1 "higher is better" regardless of the
        # underlying metric's own direction — see _metric_is_lower_better.
        h["score"] = (1.0 - norm) if lower_is_better else norm
        h["screen"] = "spatial_vault"
        h["session_id"] = ""
        h["ts"] = None
    hits.sort(key=lambda h: h["score"], reverse=True)
    if stale:
        for h in hits:
            h.setdefault("metadata", {})["vault_stale_entries_skipped"] = stale
    return hits[:top_k]


@dataclasses.dataclass
class MergedResult:
    """Combined output of `merged_search` — hits tagged by source, plus
    any non-fatal warnings (e.g. the vault is configured but broken) so a
    caller can show them rather than silently proceeding as if nothing was
    consulted."""

    hits: list[dict[str, Any]]
    sources_used: list[str]
    warnings: list[str]


def merged_search(
    query: str, *, top_k: int = 5, memory: GlobalMemory | None = None
) -> MergedResult:
    """The actual merge: local ``GlobalMemory`` (Ollama-embedded, this
    app's own store) plus the external spatial vault, when each is
    configured. NEVER raises — a broken/unreachable vault becomes a
    ``warnings`` entry, not a crashed turn (an observer/retrieval helper
    must never break the turn it's serving, matching
    ``dispatch._maybe_ingest_memory``'s own rule). An honestly EMPTY
    ``hits`` list (with no warnings) means both sources were consulted and
    found nothing real — never a fabricated placeholder.

    Cross-source ranking is BEST EFFORT, not a precise unified score:
    local hits carry a true cosine similarity (0..1, well-defined); vault
    hits carry a min-max-normalized rank within THIS query's own result set
    (see ``query_spatial_vault``) because the vault's underlying metric
    (L2 vs inner product, and whatever embedding model actually produced
    it) is not something this module can verify from here. Each hit is
    tagged with its source so a caller can weigh that honestly instead of
    treating the combined sort as more precise than it is.
    """
    hits: list[dict[str, Any]] = []
    sources_used: list[str] = []
    warnings: list[str] = []

    if global_memory_enabled():
        mem = memory or get_default_memory()
        local_hits = mem.search(query, top_k=top_k)
        for h in local_hits:
            h = dict(h)
            h["source"] = "local"
            hits.append(h)
        sources_used.append("local")

    if spatial_vault_configured():
        try:
            vault_hits = query_spatial_vault(query, top_k=top_k)
        except ExternalCorpusError as exc:
            warnings.append(str(exc))
        else:
            for h in vault_hits:
                h = dict(h)
                h["source"] = "spatial_vault"
                hits.append(h)
            sources_used.append("spatial_vault")

    hits.sort(key=lambda h: h.get("score", 0.0), reverse=True)
    return MergedResult(hits=hits[:top_k], sources_used=sources_used, warnings=warnings)


def format_merged_result(query: str, result: MergedResult) -> str:
    """Plain-text rendering for the tool-call path (general_roster.py) —
    mirrors ``global_memory.GlobalMemory.retrieve_context_for_prompt``'s
    formatting shape but across both sources, tagging each line's origin
    so 'from the local store' and 'from the desktop vault' are never
    conflated."""
    if not result.sources_used and not result.warnings:
        return (
            "NOT CONFIGURED: shared memory has no source enabled on this "
            "machine — set DOURMOUSE_GLOBAL_MEMORY=1 for the local "
            "embedding store and/or DOURMOUSE_SPATIAL_VAULT_PATH for the "
            "desktop's spatial vault. Neither is set, so nothing was "
            "queried, and nothing was fabricated."
        )
    sources_desc = ", ".join(result.sources_used) if result.sources_used else "none answered"
    lines = [f"SHARED MEMORY SEARCH ({sources_desc}) for {query!r}:"]
    if result.warnings:
        lines.append("WARNINGS (a configured source did not answer honestly):")
        lines.extend(f"  - {w}" for w in result.warnings)
    if not result.hits:
        lines.append("No matches (honest — every configured source was consulted).")
        return "\n".join(lines)
    for i, h in enumerate(result.hits):
        origin = "local store" if h["source"] == "local" else "desktop spatial vault"
        lines.append(
            f"[{i + 1}] score={h.get('score', 0.0):.2f} source={origin}\n    {h['text']}"
        )
    return "\n".join(lines)
