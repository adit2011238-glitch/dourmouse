"""The research thread's door to the shared library (PL-10 "Shared foundations"
item 2: ``add(source, text, meta)``, ``search(query, filters)``,
``cite(passage_id)``).

Thread T4 builds the one library every thread writes through. It has not
landed yet, so research writes behind this thin interface and an interim store:
SQLite with a full-text index (keyword search only, no embeddings). When T4's
library exists, ``get_library`` returns it and nothing else in the research
pipeline changes; T4 migrates what the interim store holds and retires it.

A passage id is derived from the source and the passage text, so adding the
same paper twice stores nothing new and every citation stays valid.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Protocol

from dourmouse.config import workspace_dir

#: Target passage size. Passages split at paragraph, then sentence, boundaries.
PASSAGE_CHARS = 1200
MAX_SEARCH_LIMIT = 100


class Library(Protocol):
    def add(self, source: str, text: str, meta: dict[str, Any]) -> list[str]: ...

    def search(self, query: str, filters: dict[str, Any] | None = None, limit: int = 10) -> list[dict[str, Any]]: ...

    def cite(self, passage_id: str) -> dict[str, Any]: ...


def default_db() -> Path:
    """Resolved per call (finding #084), never at import."""
    return workspace_dir() / "research_pipeline" / "library_interim.db"


def split_passages(text: str, size: int = PASSAGE_CHARS) -> list[str]:
    """Paragraphs packed up to ``size`` characters; a paragraph longer than
    that is split at sentence ends, and a sentence longer than that at a space.
    Every character of the input except the whitespace between pieces is kept."""
    out: list[str] = []
    cur = ""

    def flush() -> None:
        nonlocal cur
        if cur.strip():
            out.append(cur.strip())
        cur = ""

    for para in re.split(r"\n\s*\n", text or ""):
        para = para.strip()
        if not para:
            continue
        pieces = [para] if len(para) <= size else re.split(r"(?<=[.!?])\s+", para)
        for piece in pieces:
            while len(piece) > size:
                cut = piece.rfind(" ", 0, size)
                cut = cut if cut > size // 2 else size
                if cur:
                    flush()
                out.append(piece[:cut].strip())
                piece = piece[cut:].strip()
            if cur and len(cur) + 1 + len(piece) > size:
                flush()
            cur = f"{cur}\n{piece}" if cur else piece
        if len(cur) >= size * 0.6:
            flush()
    flush()
    return out


def passage_id(source: str, text: str) -> str:
    return "p-" + hashlib.sha256(f"{source}\x00{text}".encode()).hexdigest()[:20]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS passages (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    text TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}',
    added_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_passages_source ON passages(source);
CREATE VIRTUAL TABLE IF NOT EXISTS passages_fts USING fts5(text, content='passages', content_rowid='rowid');
"""


class InterimLibrary:
    """Keyword-only library in one SQLite file. Thread-safe."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_db()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._lock, self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def add(self, source: str, text: str, meta: dict[str, Any]) -> list[str]:
        source = (source or "").strip()
        if not source:
            raise ValueError("library add: source is required")
        ids: list[str] = []
        with self._lock, self._conn() as conn:
            for i, chunk in enumerate(split_passages(text)):
                pid = passage_id(source, chunk)
                body = json.dumps({**(meta or {}), "chunk": i}, ensure_ascii=False, default=str)
                cur = conn.execute(
                    "INSERT OR IGNORE INTO passages (id, source, text, meta, added_at) VALUES (?, ?, ?, ?, ?)",
                    (pid, source, chunk, body, time.time()),
                )
                if cur.rowcount:
                    conn.execute("INSERT INTO passages_fts (rowid, text) VALUES (last_insert_rowid(), ?)", (chunk,))
                ids.append(pid)
            conn.commit()
        return ids

    @staticmethod
    def _match(query: str) -> str:
        from .answer_critic import _STOPWORDS

        words = [w for w in re.findall(r"\w+", (query or "").lower()) if len(w) > 1 and w not in _STOPWORDS]
        return " OR ".join(f'"{w}"' for w in dict.fromkeys(words))

    def search(self, query: str, filters: dict[str, Any] | None = None, limit: int = 10) -> list[dict[str, Any]]:
        """Best passages first (BM25). ``filters``: ``source`` (one or a list)
        and ``meta`` (a dict every listed key must equal)."""
        filters = filters or {}
        for key in filters.get("meta") or {}:
            if not re.fullmatch(r"[A-Za-z0-9_]{1,40}", str(key)):
                raise ValueError(f"bad meta filter key {key!r}")
        match = self._match(query)
        if not match:
            return []
        sql = ("SELECT p.id, p.source, p.text, p.meta, bm25(passages_fts) AS score FROM passages_fts "
               "JOIN passages p ON p.rowid = passages_fts.rowid WHERE passages_fts MATCH ?")
        params: list[Any] = [match]
        sources = filters.get("source")
        if sources:
            sources = [sources] if isinstance(sources, str) else list(sources)
            sql += " AND p.source IN (" + ",".join("?" for _ in sources) + ")"
            params.extend(sources)
        for key, value in (filters.get("meta") or {}).items():
            # key checked above against [A-Za-z0-9_]: never a quote or an operator
            sql += f" AND json_extract(p.meta, '$.{key}') = ?"
            params.append(value)
        sql += " ORDER BY score LIMIT ?"
        params.append(max(1, min(int(limit), MAX_SEARCH_LIMIT)))
        with self._lock, self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [{"passage_id": r["id"], "source": r["source"], "text": r["text"], "meta": json.loads(r["meta"]),
                 "score": -float(r["score"])} for r in rows]

    def cite(self, passage_id: str) -> dict[str, Any]:
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT id, source, text, meta FROM passages WHERE id=?", (passage_id,)).fetchone()
        if row is None:
            raise KeyError(f"no passage {passage_id!r}")
        return {"passage_id": row["id"], "source": row["source"], "text": row["text"], "meta": json.loads(row["meta"])}

    def count(self) -> int:
        with self._lock, self._conn() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM passages").fetchone()[0])


def get_library() -> Library:
    """The one switch to flip when T4's library lands."""
    return InterimLibrary()
