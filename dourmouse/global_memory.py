"""Unified, embedding-based memory across every screen and agent.

Real gap this closes: the existing `memory` agent's `memory_search_semantic`
tool is callable, but nothing auto-injects relevant past context into a
turn the way JARVIS's own per-agent memory already does — a user has to
know to ask for it. This module does both halves: real ingestion (every
completed turn, across every screen — COMMS/RESEARCH/CODE/NEWS/MEDIA —
gets embedded and stored) and real retrieval, auto-injected into the
dispatch loop before the model even sees the prompt.

Deliberately does NOT pull in sentence-transformers or FAISS. Neither is
installed in this app's venv (checked directly, not assumed), and adding
them would mean a heavier installer and slower cold start for the main
app — a real tradeoff, not free. Instead:

- Embeddings come from Ollama's own /api/embeddings endpoint — already a
  live, local, zero-cost backend this app already depends on
  (code_ollama, the compute agent). One HTTP call, no new dependency.
- Vector search is brute-force cosine similarity over numpy arrays
  (numpy IS already a real dependency here). At personal-assistant memory
  scale — thousands, not millions, of stored turns — this is genuinely
  fast enough; FAISS exists to solve a scale problem this system does not
  have.

Everything in this module is OFF by default (DOURMOUSE_GLOBAL_MEMORY=0
unless explicitly set) — see dispatch.py's wiring for why: turning this on
adds a real embedding call to every single turn, and the specific Ollama
embedding model this expects (EMBED_MODEL, default "nomic-embed-text") has
not been confirmed pulled on every deployment. Enable it deliberately once
that's checked, not as a silent side effect of this file existing.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from dourmouse.config import user_config_dir

__all__ = [
    "GlobalMemory",
    "embed_text",
    "cosine_similarity",
    "validate_corpus_entry",
    "ingest_corpus_file",
    "global_memory_enabled",
    "get_default_memory",
]

EMBED_MODEL = os.environ.get("DOURMOUSE_EMBED_MODEL", "nomic-embed-text")
EMBED_DIM = int(os.environ.get("DOURMOUSE_EMBED_DIM", "768"))  # nomic-embed-text's real output size
_OLLAMA_URL = os.environ.get("DOURMOUSE_OLLAMA_URL", "http://127.0.0.1:11434")

# Pre-fix location (repo root, beside the package) — real bug: this broke
# in a frozen/installed build (the package dir is read-only / wiped on
# update) and, on a dev checkout, dumped user memory into the repo tree
# instead of the per-user config dir every other piece of user state (the
# .env, the session file) already uses. Kept only as the one-time
# migration source below — never written to again.
_LEGACY_DB = Path(__file__).resolve().parent.parent / "global_memory.sqlite3"


def _default_db_path() -> Path:
    """Where GlobalMemory stores its sqlite3 file absent an explicit
    ``db_path``: the platform-correct per-user config directory
    (``dourmouse.config.user_config_dir()`` — already handles Windows/
    macOS/XDG and the ``DOURMOUSE_CONFIG_DIR`` test override; reused here
    rather than duplicated, exactly like ``firstrun.py`` and
    ``code_backends.py`` already do).

    Read fresh on every call (not cached at import time) so the
    ``DOURMOUSE_CONFIG_DIR`` test override — set per-test by conftest's
    autouse ``_user_config_isolated`` fixture, AFTER this module is
    imported — is honored, the same reasoning ``config.py``'s own
    ``_read_user_config_file`` already documents for reading that env var
    fresh rather than once.

    Backward compatible: a legacy DB at the OLD repo-root path
    (``_LEGACY_DB``, pre-fix installs) is copied — never moved — to the
    new location the first time nothing exists there yet, so an existing
    user's memory is not silently orphaned by this fix. Logged loudly
    (Rule 2.2 honesty) rather than done silently; a copy failure is
    logged and swallowed (never raised) so a permissions hiccup on the
    legacy file can't break memory store construction — the caller just
    starts a fresh store at the new location, same as a brand-new install.
    """
    new_path = user_config_dir() / "global_memory.sqlite3"
    if not new_path.exists() and _LEGACY_DB.exists():
        try:
            new_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(_LEGACY_DB, new_path)
            print(
                f"[GLOBAL_MEMORY] migrated legacy DB from {_LEGACY_DB} to "
                f"{new_path} (one-time; old copy left in place, not deleted)"
            )
        except OSError as exc:
            print(f"[GLOBAL_MEMORY] could not migrate legacy DB: {exc}")
    return new_path


def global_memory_enabled() -> bool:
    """Deterministic on/off switch (Rule 2.8) — never inferred, never on
    by default. See this module's own docstring for exactly why."""
    return os.environ.get("DOURMOUSE_GLOBAL_MEMORY", "0").strip() == "1"


def embed_text(text: str, *, timeout: float = 15.0) -> list[float] | None:
    """Real embedding via Ollama's /api/embeddings. Returns None (never a
    fabricated vector) if the model isn't pulled or Ollama isn't reachable
    — callers must treat that as NOT CONFIGURED, exactly like every other
    honest-failure tool in this codebase, not silently skip storage."""
    body = json.dumps({"model": EMBED_MODEL, "prompt": text}).encode("utf-8")
    req = urllib.request.Request(
        f"{_OLLAMA_URL}/api/embeddings", data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None
    vec = data.get("embedding")
    if not isinstance(vec, list) or not vec:
        return None
    return vec


def cosine_similarity(a: list[float], b: list[float]) -> float:
    import numpy as np

    va, vb = np.array(a, dtype=float), np.array(b, dtype=float)
    denom = (np.linalg.norm(va) * np.linalg.norm(vb))
    if denom == 0:
        return 0.0
    return float(np.dot(va, vb) / denom)


def validate_corpus_entry(entry: dict[str, Any], *, expected_dim: int = EMBED_DIM) -> str | None:
    """Real validation for a HANDED-OFF corpus entry (Rule 2.2: never
    silently trust external data). Returns None when valid, else a plain
    string explaining exactly what's wrong.

    Two accepted shapes, matching what was actually specified to whoever
    is producing the corpus:
    - {"id", "text", "source", "metadata"}: raw text, embedded HERE with
      this module's own model — always safe, always dimension-correct.
    - {"id", "text", "vector": float[expected_dim], "metadata"}:
      pre-embedded elsewhere. MUST match expected_dim exactly — a vector
      from a different embedding model lives in an incompatible vector
      space, and cosine similarity across two different spaces is
      meaningless, not just lower-quality. Refused outright, not
      averaged in silently.
    """
    if not isinstance(entry, dict):
        return "entry is not an object"
    if not (entry.get("id") or "").__str__().strip():
        return "missing required field 'id'"
    if not (entry.get("text") or "").strip():
        return "missing required field 'text'"
    vec = entry.get("vector")
    if vec is not None:
        if not isinstance(vec, list) or not all(isinstance(x, (int, float)) for x in vec):
            return "'vector' must be a flat list of numbers"
        if len(vec) != expected_dim:
            return (
                f"'vector' has {len(vec)} dimensions, expected {expected_dim} "
                f"(this store's embedding model is {EMBED_MODEL!r} — a vector "
                "from a different model lives in an incompatible space; "
                "cosine similarity across two different spaces is meaningless, "
                "not just lower quality, so this is refused rather than accepted)"
            )
    return None


class GlobalMemory:
    """One unified store, real ingestion + real retrieval, across every
    screen. Not per-agent silos (unlike JARVIS's own per-field design,
    which makes sense at 500 genuinely separate specialists — Dourmouse
    core is one assistant across a handful of screens, so one shared store
    with a `screen` tag on each row is the honest fit for this system's
    actual shape)."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path else _default_db_path()
        # check_same_thread=False + an explicit lock: this store is reached
        # from dispatch.py's per-turn ingestion/retrieval, and webui.py runs
        # a ThreadingHTTPServer (one thread per request) -- the stdlib default
        # (check_same_thread=True) would raise sqlite3.ProgrammingError the
        # first time a request landed on a different thread than whichever
        # one first constructed this instance. Same pattern as
        # google_auth.py's AuthStore and memory_store.py.
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS memory (
                    id TEXT PRIMARY KEY,
                    text TEXT NOT NULL,
                    screen TEXT NOT NULL,
                    session_id TEXT,
                    metadata TEXT,
                    embedding TEXT NOT NULL,
                    ts REAL NOT NULL
                )"""
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def add(
        self, text: str, *, screen: str = "", session_id: str = "",
        metadata: dict[str, Any] | None = None, vector: list[float] | None = None,
        item_id: str | None = None,
    ) -> bool:
        """Real ingestion. Embeds locally unless a caller already supplied
        a validated ``vector`` (the pre-embedded handoff path). Returns
        False (never raises, never fabricates a fake row) when embedding
        fails — the caller decides whether that's worth surfacing."""
        vec = vector if vector is not None else embed_text(text)
        if vec is None:
            return False
        item_id = item_id or f"m{time.time_ns()}"
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO memory VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    item_id, text, screen, session_id,
                    json.dumps(metadata or {}), json.dumps(vec), time.time(),
                ),
            )
            self._conn.commit()
        return True

    def search(self, query: str, *, top_k: int = 5, screen: str | None = None) -> list[dict[str, Any]]:
        """Real cosine-similarity search over every stored row (optionally
        filtered to one screen). Returns [] honestly on embed failure or
        an empty store — never a fabricated result."""
        qvec = embed_text(query)
        if qvec is None:
            return []
        sql = "SELECT id, text, screen, session_id, metadata, embedding, ts FROM memory"
        params: tuple[Any, ...] = ()
        if screen:
            sql += " WHERE screen = ?"
            params = (screen,)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        scored = []
        for rid, text, scr, sess, meta, emb_json, ts in rows:
            try:
                vec = json.loads(emb_json)
            except json.JSONDecodeError:
                continue
            score = cosine_similarity(qvec, vec)
            scored.append({
                "id": rid, "text": text, "screen": scr, "session_id": sess,
                "metadata": json.loads(meta) if meta else {}, "score": score, "ts": ts,
            })
        scored.sort(key=lambda r: r["score"], reverse=True)
        return scored[:top_k]

    def retrieve_context_for_prompt(self, prompt: str, *, top_k: int = 3, min_score: float = 0.5) -> str:
        """Formatted, ready-to-inject context block — mirrors JARVIS's own
        "RELEVANT KNOWLEDGE FROM YOUR FIELD:" pattern so this reuses an
        already-proven prompt shape rather than inventing a new one.
        Empty string (never a placeholder) when nothing real clears the
        relevance bar — an empty result must never become injected noise."""
        hits = [h for h in self.search(prompt, top_k=top_k) if h["score"] >= min_score]
        if not hits:
            return ""
        lines = [
            f"[Source {i + 1}, relevance={h['score']:.2f}, from {h['screen'] or 'unknown'}] {h['text']}"
            for i, h in enumerate(hits)
        ]
        return "RELEVANT PAST CONTEXT (from earlier conversations — use it when relevant, but verify anything time-sensitive):\n" + "\n\n".join(lines)


_default_instance: GlobalMemory | None = None
_default_instance_lock = threading.Lock()


def get_default_memory() -> GlobalMemory:
    """Lazily-constructed process-wide default store, matching the
    lazy-singleton pattern this codebase already uses elsewhere for
    similarly process-lifetime resources (see goals.get_goal_store).
    Double-checked locking: webui.py's ThreadingHTTPServer means the
    first real request to touch global memory can arrive on any worker
    thread, and two such requests racing here would otherwise each
    construct their own GlobalMemory (one connection silently leaked)."""
    global _default_instance
    if _default_instance is None:
        with _default_instance_lock:
            if _default_instance is None:
                _default_instance = GlobalMemory()
    return _default_instance


def ingest_corpus_file(path: str | Path, *, memory: GlobalMemory | None = None) -> dict[str, Any]:
    """Real bulk ingestion for a HANDED-OFF corpus file — the exact
    workflow this module exists to make safe: a human hands over
    {id, text, source, metadata} (or pre-embedded {..., vector}) rows as
    JSON, this validates every one (validate_corpus_entry) before trusting
    it, embeds the raw-text ones locally, and reports real per-row
    outcomes rather than a bare success/fail.
    """
    memory = memory or get_default_memory()
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{path} must contain a JSON array of entries, got {type(raw).__name__}")

    accepted = 0
    rejected: list[dict[str, str]] = []
    embed_failed: list[str] = []
    for entry in raw:
        problem = validate_corpus_entry(entry)
        if problem:
            rejected.append({"id": str(entry.get("id", "?")), "reason": problem})
            continue
        ok = memory.add(
            entry["text"], screen=entry.get("source", "corpus"),
            metadata=entry.get("metadata", {}), vector=entry.get("vector"),
            item_id=str(entry["id"]),
        )
        if ok:
            accepted += 1
        else:
            embed_failed.append(str(entry["id"]))

    return {
        "total": len(raw), "accepted": accepted,
        "rejected": rejected, "embed_failed": embed_failed,
    }
