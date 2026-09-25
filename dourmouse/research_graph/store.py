"""SQLite persistence for the research object graph (R1 + R2, finding #095).

One table per object type (``obj_<type>``), keyed ``(id, version)``: nothing
is ever updated in place. A revision of a VERSIONED object inserts version
N+1 and marks version N ``superseded_at``; an IMMUTABLE object refuses any
revision. One append-only ``edges`` table carries the typed relationships.

This replaces the old single-JSON-blob ``research_records`` table as the
place research lives; ``migrate.py`` moves the old records across without
touching or deleting the old table.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dourmouse.config import workspace_dir

from .model import OBJECT_TYPES, GraphError, ImmutableObject, Mutability, validate_body, validate_relation

SCHEMA_VERSION = 1

# R6 (finding #128): every change to the graph is announced to observers
# (the server records each in the append-only event log). Inside a
# transaction the announcements wait for the commit and are dropped on a
# rollback, so the log never shows a change that did not happen.
_observers: list[Any] = []


def add_observer(fn: Any) -> None:
    if fn not in _observers:
        _observers.append(fn)


def remove_observer(fn: Any) -> None:
    if fn in _observers:
        _observers.remove(fn)


def default_db() -> Path:
    """Resolved per call (finding #084): the same file the research pipeline
    already uses, so the graph and the legacy table sit side by side."""
    return workspace_dir() / "research_pipeline" / "research.db"


@dataclass(frozen=True)
class GraphObject:
    type: str
    id: str
    version: int
    body: dict[str, Any]
    project_id: str | None
    created_at: float
    created_by: str
    superseded_at: float | None

    @property
    def ref(self) -> tuple[str, str]:
        return (self.type, self.id)


@dataclass(frozen=True)
class Edge:
    src_type: str
    src_id: str
    relation: str
    dst_type: str
    dst_id: str
    created_at: float
    created_by: str


class GraphStore:
    def __init__(self, path: str | Path | None = None, *, clock=time.time) -> None:  # type: ignore[no-untyped-def]
        self.path = Path(path) if path is not None else default_db()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._clock = clock
        self._tx = threading.local()
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")  # persists in the file
            self._ensure_schema(conn)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run many operations on one connection with one commit. A record
        sync is dozens of writes; one fsync each took 39s for the test file,
        and a failure part-way must not leave half a record in the graph."""
        if getattr(self._tx, "conn", None) is not None:
            yield  # nested: the outer transaction owns the commit
            return
        with self._lock:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.execute("PRAGMA synchronous=NORMAL")
            self._tx.conn = conn
            self._tx.pending = []
            try:
                yield
                conn.commit()
            except BaseException:
                conn.rollback()
                self._tx.pending = []
                raise
            finally:
                self._tx.conn = None
                conn.close()
            pending, self._tx.pending = self._tx.pending, []
            for event in pending:
                self._announce(event)

    def _emit(self, kind: str, obj_type: str, obj_id: str, by: str, **extra: Any) -> None:
        event = {"kind": kind, "type": obj_type, "id": obj_id, "by": by, "db": str(self.path), **extra}
        if getattr(self._tx, "conn", None) is not None:
            self._tx.pending.append(event)
        else:
            self._announce(event)

    @staticmethod
    def _announce(event: dict[str, Any]) -> None:
        for fn in list(_observers):
            with contextlib.suppress(Exception):  # an observer must never break a write
                fn(event)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        shared = getattr(self._tx, "conn", None)
        if shared is not None:
            yield shared
            return
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            conn.execute("PRAGMA synchronous=NORMAL")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _table(obj_type: str) -> str:
        if obj_type not in OBJECT_TYPES:
            raise GraphError(f"unknown object type {obj_type!r}")
        return f"obj_{obj_type}"

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE IF NOT EXISTS graph_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        for name in OBJECT_TYPES:
            t = self._table(name)
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {t} ("
                "id TEXT NOT NULL, version INTEGER NOT NULL, project_id TEXT, "
                "body TEXT NOT NULL, created_at REAL NOT NULL, created_by TEXT NOT NULL, "
                "superseded_at REAL, PRIMARY KEY (id, version))"
            )
            conn.execute(f"CREATE INDEX IF NOT EXISTS {t}_project ON {t}(project_id)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS edges ("
            "seq INTEGER PRIMARY KEY AUTOINCREMENT, src_type TEXT NOT NULL, src_id TEXT NOT NULL, "
            "relation TEXT NOT NULL, dst_type TEXT NOT NULL, dst_id TEXT NOT NULL, "
            "created_at REAL NOT NULL, created_by TEXT NOT NULL, "
            "UNIQUE (src_type, src_id, relation, dst_type, dst_id))"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS edges_src ON edges(src_type, src_id, relation)")
        conn.execute("CREATE INDEX IF NOT EXISTS edges_dst ON edges(dst_type, dst_id, relation)")
        conn.execute(
            "INSERT OR IGNORE INTO graph_meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
        )

    def meta_get(self, key: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM graph_meta WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def meta_set(self, key: str, value: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO graph_meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value),
            )

    # -- objects --------------------------------------------------------- #

    def put(
        self, obj_type: str, obj_id: str, body: dict[str, Any], *,
        created_by: str, project_id: str | None = None,
    ) -> GraphObject:
        """Create ``obj_id`` if absent. Idempotent: putting the same body again
        returns the existing object; putting a DIFFERENT body for an existing
        id is refused (use ``revise`` for versioned types). Deterministic ids
        make migration and ongoing sync the same safe operation."""
        validate_body(obj_type, body)
        table = self._table(obj_type)
        with self._lock, self._conn() as conn:
            # Compared with version 1, the content the object was created
            # with: a later revise() must not make an idempotent re-put fail.
            row = conn.execute(
                f"SELECT body FROM {table} WHERE id=? AND version=1", (obj_id,)  # noqa: S608 -- table name from a closed set
            ).fetchone()
            if row is not None:
                if json.loads(row[0]) != body:
                    raise GraphError(
                        f"{obj_type} {obj_id} already exists with different content; "
                        + ("it is immutable" if OBJECT_TYPES[obj_type].mutability is Mutability.IMMUTABLE else "use revise()")
                    )
                return self._get(conn, obj_type, obj_id, None)
            conn.execute(
                f"INSERT INTO {table}(id, version, project_id, body, created_at, created_by) "  # noqa: S608
                "VALUES (?, 1, ?, ?, ?, ?)",
                (obj_id, project_id, json.dumps(body, sort_keys=True), self._clock(), created_by),
            )
            created = self._get(conn, obj_type, obj_id, None)
        self._emit("graph.put", obj_type, obj_id, created_by, version=1, project_id=project_id)
        return created

    def revise(self, obj_type: str, obj_id: str, changes: dict[str, Any], *, created_by: str) -> GraphObject:
        """Version N+1 of a VERSIONED object; version N stays readable and is
        marked superseded. IMMUTABLE objects refuse (spec item 36: the
        original passage must never change)."""
        if OBJECT_TYPES.get(obj_type) is None:
            raise GraphError(f"unknown object type {obj_type!r}")
        if OBJECT_TYPES[obj_type].mutability is Mutability.IMMUTABLE:
            raise ImmutableObject(f"{obj_type} objects are immutable; {obj_id} cannot be revised")
        validate_body(obj_type, changes, partial=True)
        table = self._table(obj_type)
        with self._lock, self._conn() as conn:
            cur = self._get(conn, obj_type, obj_id, None)
            body = {**cur.body, **changes}
            validate_body(obj_type, body)
            now = self._clock()
            conn.execute(
                f"UPDATE {table} SET superseded_at=? WHERE id=? AND version=?", (now, obj_id, cur.version)  # noqa: S608
            )
            conn.execute(
                f"INSERT INTO {table}(id, version, project_id, body, created_at, created_by) "  # noqa: S608
                "VALUES (?, ?, ?, ?, ?, ?)",
                (obj_id, cur.version + 1, cur.project_id, json.dumps(body, sort_keys=True), now, created_by),
            )
            revised = self._get(conn, obj_type, obj_id, None)
        self._emit("graph.revise", obj_type, obj_id, created_by, version=revised.version,
                   changed=sorted(changes))
        return revised

    def _get(self, conn: sqlite3.Connection, obj_type: str, obj_id: str, version: int | None) -> GraphObject:
        table = self._table(obj_type)
        if version is None:
            row = conn.execute(
                f"SELECT id, version, project_id, body, created_at, created_by, superseded_at FROM {table} "  # noqa: S608
                "WHERE id=? ORDER BY version DESC LIMIT 1", (obj_id,),
            ).fetchone()
        else:
            row = conn.execute(
                f"SELECT id, version, project_id, body, created_at, created_by, superseded_at FROM {table} "  # noqa: S608
                "WHERE id=? AND version=?", (obj_id, version),
            ).fetchone()
        if row is None:
            raise KeyError(f"no {obj_type} {obj_id}" + (f" version {version}" if version else ""))
        return GraphObject(obj_type, row[0], row[1], json.loads(row[3]), row[2], row[4], row[5], row[6])

    def get(self, obj_type: str, obj_id: str, version: int | None = None) -> GraphObject:
        with self._conn() as conn:
            return self._get(conn, obj_type, obj_id, version)

    def history(self, obj_type: str, obj_id: str) -> list[GraphObject]:
        table = self._table(obj_type)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT version FROM {table} WHERE id=? ORDER BY version", (obj_id,)  # noqa: S608
            ).fetchall()
            return [self._get(conn, obj_type, obj_id, r[0]) for r in rows]

    def as_of(self, obj_type: str, obj_id: str, when: float) -> GraphObject | None:
        """The version that was current at ``when``: what we actually knew then."""
        best = None
        for v in self.history(obj_type, obj_id):
            if v.created_at <= when:
                best = v
        return best

    def find(self, obj_type: str, *, project_id: str | None = None, **fields: Any) -> list[GraphObject]:
        """Current versions whose body matches every ``field=value``."""
        table = self._table(obj_type)
        where, args = ["superseded_at IS NULL"], []
        if project_id is not None:
            where.append("project_id = ?")
            args.append(project_id)
        for k, v in fields.items():
            if k not in OBJECT_TYPES[obj_type].fields:
                raise GraphError(f"{obj_type} has no field {k!r}")
            where.append(f"json_extract(body, '$.{k}') = ?")
            args.append(v)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT id FROM {table} WHERE {' AND '.join(where)} ORDER BY created_at, id", args  # noqa: S608
            ).fetchall()
            return [self._get(conn, obj_type, r[0], None) for r in rows]

    def count(self, obj_type: str) -> int:
        with self._conn() as conn:
            return int(conn.execute(
                f"SELECT COUNT(DISTINCT id) FROM {self._table(obj_type)}"  # noqa: S608
            ).fetchone()[0])

    # -- edges ------------------------------------------------------------ #

    def link(self, src: tuple[str, str], relation: str, dst: tuple[str, str], *, created_by: str) -> None:
        """Append a typed edge (idempotent). Both ends must exist."""
        validate_relation(relation)
        with self._lock, self._conn() as conn:
            for t, i in (src, dst):
                self._get(conn, t, i, None)  # raises KeyError for a dangling end
            cur = conn.execute(
                "INSERT OR IGNORE INTO edges(src_type, src_id, relation, dst_type, dst_id, created_at, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (src[0], src[1], relation, dst[0], dst[1], self._clock(), created_by),
            )
            added = cur.rowcount == 1
        if added:
            self._emit("graph.link", src[0], src[1], created_by, relation=relation, dst_type=dst[0], dst_id=dst[1])

    def edges(
        self, *, src: tuple[str, str] | None = None, dst: tuple[str, str] | None = None,
        relation: str | None = None,
    ) -> list[Edge]:
        where, args = [], []
        if src is not None:
            where += ["src_type = ?", "src_id = ?"]
            args += list(src)
        if dst is not None:
            where += ["dst_type = ?", "dst_id = ?"]
            args += list(dst)
        if relation is not None:
            validate_relation(relation)
            where.append("relation = ?")
            args.append(relation)
        sql = "SELECT src_type, src_id, relation, dst_type, dst_id, created_at, created_by FROM edges"
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self._conn() as conn:
            return [Edge(*r) for r in conn.execute(sql + " ORDER BY seq", args).fetchall()]

    def related(self, obj: tuple[str, str], relation: str) -> list[GraphObject]:
        """Current versions of everything ``obj`` points at by ``relation``:
        the spec's "Hypothesis H1 supported by Evidence E1, E4" as a query."""
        out = []
        with self._conn() as conn:
            for e in self.edges(src=obj, relation=relation):
                out.append(self._get(conn, e.dst_type, e.dst_id, None))
        return out

