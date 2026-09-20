"""SQLite persistence for the device wiki (Domain E, step 2) -- same
one-connection-per-operation, WAL-mode discipline as every other real
store in this codebase (research_pipeline/store.py, security/sentry.py's
SentryStore, goals.py). Unlike research_pipeline's own one-row-per-
question JSON-body shape, this store is one row PER REAL FILE -- a
device wiki can hold thousands of entries, and querying/updating one
file's own row is the natural, efficient shape here, not a single
mega-JSON blob for the whole wiki.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Optional

from dourmouse.config import workspace_dir

from .core import WikiEntry

#: Workspace-relative default, applied from the start (finding #028's own
#: lesson, matching research_pipeline/store.py's and sentry.py's own
#: DEFAULT_DB convention) -- never retrofitted.
DEFAULT_DB = workspace_dir() / "device_wiki" / "wiki.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS wiki_entries (
    path          TEXT NOT NULL PRIMARY KEY,
    content_hash  TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL,
    status        TEXT NOT NULL,
    summary       TEXT NOT NULL DEFAULT '',
    summarized_at REAL,
    last_seen     REAL NOT NULL
);
"""


class WikiStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self._path), timeout=30.0)

    def save_entry(self, entry: WikiEntry) -> None:
        with self._lock, self._conn() as conn:
            self._upsert(conn, entry)
            conn.commit()

    def save_all(self, entries: dict[str, WikiEntry]) -> None:
        """Real batch upsert -- the caller passes the exact dict
        `reconcile()` returns after a real scan, one transaction for the
        whole batch rather than one commit per file."""
        with self._lock, self._conn() as conn:
            for entry in entries.values():
                self._upsert(conn, entry)
            conn.commit()

    @staticmethod
    def _upsert(conn: sqlite3.Connection, entry: WikiEntry) -> None:
        conn.execute(
            "INSERT INTO wiki_entries "
            "(path, content_hash, size_bytes, status, summary, summarized_at, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET "
            "  content_hash=excluded.content_hash, size_bytes=excluded.size_bytes, "
            "  status=excluded.status, summary=excluded.summary, "
            "  summarized_at=excluded.summarized_at, last_seen=excluded.last_seen",
            (entry.path, entry.content_hash, entry.size_bytes, entry.status,
             entry.summary, entry.summarized_at, entry.last_seen),
        )

    def get(self, path: str) -> Optional[WikiEntry]:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT path, content_hash, size_bytes, status, summary, summarized_at, last_seen "
                "FROM wiki_entries WHERE path=?", (path,),
            ).fetchone()
        return _row_to_entry(row) if row else None

    def list_all(self, status: str | None = None) -> list[WikiEntry]:
        with self._lock, self._conn() as conn:
            if status is None:
                rows = conn.execute(
                    "SELECT path, content_hash, size_bytes, status, summary, summarized_at, last_seen "
                    "FROM wiki_entries ORDER BY path"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT path, content_hash, size_bytes, status, summary, summarized_at, last_seen "
                    "FROM wiki_entries WHERE status=? ORDER BY path", (status,),
                ).fetchall()
        return [_row_to_entry(r) for r in rows]

    def all_as_dict(self) -> dict[str, WikiEntry]:
        """The exact shape `reconcile()`'s own `existing` parameter
        expects -- feeds this store's current real state back into the
        next scan's reconciliation."""
        return {e.path: e for e in self.list_all()}


def _row_to_entry(row: tuple) -> WikiEntry:
    path, content_hash, size_bytes, status, summary, summarized_at, last_seen = row
    return WikiEntry(
        path=path, content_hash=content_hash, size_bytes=size_bytes, status=status,
        summary=summary, summarized_at=summarized_at, last_seen=last_seen,
    )
