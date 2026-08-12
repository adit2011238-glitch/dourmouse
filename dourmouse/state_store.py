"""Cross-device state store (Phase R0 — one DourMouse; v5.17 per-user).

The server is the single source of truth; every device reads and writes the
SAME store, so a watchlist star on the desktop is present on the phone the
next time it loads. Since v5.17 the store is **per-owner**: every row carries
an ``owner`` — the signed-in Google user's email, or the ``"*"`` shared
bucket when nobody is signed in. Two signed-in people on one server never
see each other's watchlist, alerts, prefs, activity, or resume workspace.

Ownership rules (honest and deterministic):

- ``"*"`` (``SHARED_OWNER``) is the shared/global bucket: signed-out devices
  and system-generated rows (e.g. the ATLAS-run SYSTEM alert, which is
  posted from a background thread with no user) live here.
- **watchlist / prefs / recent / workspace** are STRICTLY per-owner — a
  signed-in user sees only their own rows; a signed-out client sees only the
  shared bucket. ``muted_sources`` is a pref, so mutes are per-owner too.
- **alerts** are the one cross-cutting surface: every viewer sees the
  shared/system alerts (``owner='*'``) PLUS their own. Dismissing or
  reprioritizing another user's alert is refused (ownership-guarded UPDATE).
  A shared/system alert is ONE row, so dismissing it hides it for everyone
  (deliberate: it is a server-wide broadcast, not a per-viewer inbox);
  muting a kind hides it only for the muter.

Migration visibility note: pre-v5.17 ``alerts`` rows (backfilled to ``*``)
stay visible to everyone, but pre-v5.17 ``recent`` rows land in the shared
bucket and are therefore only visible to signed-out clients — a signed-in
user starts a fresh activity history by design.

Surfaces (spec §7 cross-device state):

- **watchlist** — symbols starred from any device (idempotent add).
- **alerts** — the DOURMOUSE ALERTS inbox: typed (atlas/world/market/
  system), dismissible, per-kind mute, prioritizable, deep-linkable.
- **prefs** — simple key/value preferences (last-write-wins).
- **recent** — append-only recent-activity log (audit-style, like the
  sessions the webui already keeps).
- **workspace** — per-device "where I was", for the resume banner.

Storage: SQLite (WAL) with O(1) transactional appends — the same pattern the
ATLAS trial registry and champion stores use. ``path=None`` keeps the store
in memory (hermetic tests); a real path persists across restarts.

Migration: Phase-R0 databases (pre-v5.17) have no ``owner`` column. The
watchlist/prefs/workspace tables are REBUILT (their old single-column
primary keys — symbol/key/device — would collide across owners) and their
rows backfilled into the shared bucket; alerts/recent get a plain ALTER.
Nothing is lost; pre-existing data stays visible to signed-out devices.

Honesty (Rule 2.2 / 2.8): bad kinds/severities/empty symbols raise
``ValueError`` with the REAL reason — never a silent write or a fabricated
row. Deterministic: all writes go through one code path, results are sorted
by explicit keys (priority then id; recent by id).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: Allowed alert kinds (the DOURMOUSE ALERTS types). Anything else is
#: rejected — never silently persisted under a made-up kind.
ALERT_KINDS = frozenset({"atlas", "world", "market", "system"})
#: Allowed severities.
SEVERITIES = frozenset({"low", "med", "high"})

#: Muted sources are stored as a JSON list under this prefs key.
_MUTED_PREFS_KEY = "muted_sources"

#: The shared/global bucket: rows owned by no signed-in user (signed-out
#: devices, system-generated alerts). Alerts with this owner are visible to
#: EVERYONE; all other surfaces read strictly per-owner.
SHARED_OWNER = "*"

#: Current table DDL (owner column + composite PKs where a single-column PK
#: would collide across owners).
_TABLE_DDL = {
    "watchlist": (
        "CREATE TABLE watchlist ("
        " owner TEXT NOT NULL DEFAULT '*', symbol TEXT NOT NULL,"
        " name TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'desktop',"
        " created TEXT NOT NULL, PRIMARY KEY (owner, symbol))"
    ),
    "alerts": (
        "CREATE TABLE alerts ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL DEFAULT '*',"
        " kind TEXT NOT NULL, title TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',"
        " severity TEXT NOT NULL DEFAULT 'med', link TEXT NOT NULL DEFAULT '',"
        " created TEXT NOT NULL, dismissed INTEGER NOT NULL DEFAULT 0,"
        " priority INTEGER NOT NULL DEFAULT 0)"
    ),
    "prefs": (
        "CREATE TABLE prefs ("
        " owner TEXT NOT NULL DEFAULT '*', key TEXT NOT NULL, value TEXT NOT NULL,"
        " PRIMARY KEY (owner, key))"
    ),
    "recent": (
        "CREATE TABLE recent ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL DEFAULT '*',"
        " at TEXT NOT NULL, what TEXT NOT NULL)"
    ),
    "workspace": (
        "CREATE TABLE workspace ("
        " owner TEXT NOT NULL DEFAULT '*', device TEXT NOT NULL,"
        " workspace TEXT NOT NULL, at TEXT NOT NULL,"
        " PRIMARY KEY (owner, device))"
    ),
}

#: Pre-owner column order for the tables that need a REBUILD migration (their
#: old single-column primary keys would collide across owners).
_LEGACY_COLUMNS = {
    "watchlist": ("symbol", "name", "source", "created"),
    "prefs": ("key", "value"),
    "workspace": ("device", "workspace", "at"),
}


def _now() -> str:
    # Microseconds so same-second writes order deterministically (two
    # devices updating "last workspace" within the same second must not
    # produce an ambiguous "most recent").
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class StateStore:
    """SQLite-backed cross-device state. Thread-safe (one connection per
    operation), crash-safe (WAL + transactions), O(1) appends. Every
    surface is scoped by ``owner`` — see the module docstring for the
    visibility rules."""

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self.path = Path(path) if path is not None else None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._db_path = str(self.path)
            self._conn = None  # file mode: one connection per operation
        else:
            # In-memory: a SINGLE shared connection is mandatory — each
            # sqlite ``:memory:`` connection is its own database, so
            # per-operation connections would silently see no tables.
            self._db_path = ":memory:"
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._lock = threading.Lock()
        self._closed = False
        self._init_db()

    # -- plumbing -------------------------------------------------------- #

    def _connect(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError("state store is closed")
        if self._conn is not None:
            # in-memory mode: the one shared connection
            self._conn.row_factory = sqlite3.Row
            return self._conn
        connection = sqlite3.connect(self._db_path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            for table, ddl in _TABLE_DDL.items():
                columns = [
                    row[1]
                    for row in connection.execute(f"PRAGMA table_info({table})")
                ]
                if not columns:
                    connection.execute(ddl)
                    continue
                if "owner" in columns:
                    continue
                if table in _LEGACY_COLUMNS:
                    # Legacy single-column PK (symbol/key/device) would
                    # collide across owners — rebuild the table, backfilling
                    # every pre-existing row into the shared bucket.
                    self._rebuild_legacy(connection, table)
                else:
                    connection.execute(
                        "ALTER TABLE {table} ADD COLUMN owner TEXT NOT NULL DEFAULT '{shared}'".format(
                            table=table, shared=SHARED_OWNER
                        )
                    )

    @staticmethod
    def _rebuild_legacy(connection: sqlite3.Connection, table: str) -> None:
        legacy = f"{table}_legacy"
        connection.execute(f"ALTER TABLE {table} RENAME TO {legacy}")
        connection.execute(_TABLE_DDL[table])
        cols = ", ".join(_LEGACY_COLUMNS[table])
        connection.execute(
            "INSERT INTO {table} (owner, {cols}) SELECT '{shared}', {cols} FROM {legacy}".format(
                table=table, cols=cols, shared=SHARED_OWNER, legacy=legacy
            )
        )
        connection.execute(f"DROP TABLE {legacy}")

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
                self._closed = True
                return
            if self.path is not None:
                # Best-effort checkpoint so the WAL is flushed on shutdown
                # (must run BEFORE _closed flips — _connect refuses closed
                # stores).
                try:
                    with self._connect() as connection:
                        connection.execute("PRAGMA wal_checkpoint(FULL)")
                except sqlite3.Error:
                    pass
            self._closed = True

    # -- watchlist ------------------------------------------------------- #

    def add_watch(
        self,
        symbol: str,
        name: str = "",
        source: str = "desktop",
        owner: str = SHARED_OWNER,
    ) -> dict[str, Any]:
        """Star a symbol for ONE owner (idempotent — re-adding updates
        name/source). Two owners can star the same symbol independently."""
        symbol = (symbol or "").strip().upper()
        if not symbol:
            raise ValueError("watchlist: symbol is empty")
        name = (name or "").strip()[:120]
        source = (source or "desktop").strip()[:40] or "desktop"
        owner = (owner or "").strip()[:200] or SHARED_OWNER
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO watchlist (owner, symbol, name, source, created)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(owner, symbol) DO UPDATE SET name=excluded.name,"
                " source=excluded.source",
                (owner, symbol, name, source, _now()),
            )
        self.log_recent(f"★ {symbol} added to watchlist ({source})", owner=owner)
        return {"symbol": symbol, "name": name, "source": source, "owner": owner}

    def remove_watch(self, symbol: str, owner: str = SHARED_OWNER) -> bool:
        symbol = (symbol or "").strip().upper()
        owner = (owner or "").strip()[:200] or SHARED_OWNER
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM watchlist WHERE owner=? AND symbol=?", (owner, symbol)
            )
            removed = cursor.rowcount > 0
        if removed:
            self.log_recent(f"☆ {symbol} removed from watchlist", owner=owner)
        return removed

    def watchlist(self, owner: str = SHARED_OWNER) -> list[dict[str, Any]]:
        owner = (owner or "").strip()[:200] or SHARED_OWNER
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT owner, symbol, name, source, created FROM watchlist"
                " WHERE owner=? ORDER BY symbol ASC",
                (owner,),
            ).fetchall()
        return [dict(row) for row in rows]

    # -- alerts ---------------------------------------------------------- #

    def add_alert(
        self,
        kind: str,
        title: str,
        detail: str = "",
        severity: str = "med",
        link: str = "",
        owner: str = SHARED_OWNER,
    ) -> dict[str, Any]:
        """Create one DOURMOUSE alert. Raises ValueError on bad kind/severity.

        ``owner`` defaults to the SHARED bucket — system alerts (posted from
        background threads with no user) are visible to EVERYONE. A
        user-targeted alert passes the user's email."""
        kind = (kind or "").strip().lower()
        if kind not in ALERT_KINDS:
            raise ValueError(
                f"alerts: unknown kind {kind!r} (allowed: {sorted(ALERT_KINDS)})"
            )
        severity = (severity or "med").strip().lower()
        if severity not in SEVERITIES:
            raise ValueError(f"alerts: unknown severity {severity!r} (allowed: {sorted(SEVERITIES)})")
        title = (title or "").strip()[:160]
        if not title:
            raise ValueError("alerts: title is empty")
        detail = (detail or "").strip()[:400]
        link = (link or "").strip()[:200]
        owner = (owner or "").strip()[:200] or SHARED_OWNER
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO alerts (owner, kind, title, detail, severity, link, created)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (owner, kind, title, detail, severity, link, _now()),
            )
            alert_id = cursor.lastrowid
        return self._alert_row(alert_id)

    def _alert_row(self, alert_id: int) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
        return self._serialize_alert(row) if row is not None else {}

    @staticmethod
    def _serialize_alert(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "owner": row["owner"],
            "kind": row["kind"],
            "title": row["title"],
            "detail": row["detail"],
            "severity": row["severity"],
            "link": row["link"],
            "created": row["created"],
            "dismissed": bool(row["dismissed"]),
            "priority": int(row["priority"]),
        }

    def alerts(
        self, owner: str = SHARED_OWNER, include_dismissed: bool = False
    ) -> list[dict[str, Any]]:
        """Active alerts visible to ONE viewer: the shared/system bucket
        (owner '*') plus the viewer's own. Highest priority first, then
        newest first. ``include_dismissed`` returns everything for review."""
        owner = (owner or "").strip()[:200] or SHARED_OWNER
        muted = self.muted_sources(owner)
        query = "SELECT * FROM alerts WHERE owner IN (?, ?)"
        params: tuple = (owner, SHARED_OWNER)
        if not include_dismissed:
            query += " AND dismissed=0"
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                query + " ORDER BY priority DESC, id DESC", params
            ).fetchall()
        out = [self._serialize_alert(r) for r in rows]
        if not include_dismissed:
            out = [a for a in out if a["kind"] not in muted]
        return out

    def dismiss_alert(self, alert_id: int, owner: str = SHARED_OWNER) -> bool:
        """Dismiss ONE alert — only your own or a shared/system one. Another
        user's alert is refused (ownership-guarded UPDATE)."""
        owner = (owner or "").strip()[:200] or SHARED_OWNER
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE alerts SET dismissed=1 WHERE id=? AND owner IN (?, ?)",
                (int(alert_id), owner, SHARED_OWNER),
            )
            return cursor.rowcount > 0

    def set_priority(
        self, alert_id: int, priority: int, owner: str = SHARED_OWNER
    ) -> bool:
        """Reprioritize ONE alert — ownership-guarded like dismiss."""
        priority = max(-2, min(2, int(priority)))
        owner = (owner or "").strip()[:200] or SHARED_OWNER
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE alerts SET priority=? WHERE id=? AND owner IN (?, ?)",
                (priority, int(alert_id), owner, SHARED_OWNER),
            )
            return cursor.rowcount > 0

    def mute(self, kind: str, owner: str = SHARED_OWNER) -> None:
        kind = (kind or "").strip().lower()
        if kind not in ALERT_KINDS:
            raise ValueError(f"alerts: unknown kind {kind!r} (allowed: {sorted(ALERT_KINDS)})")
        muted = set(self.muted_sources(owner))
        muted.add(kind)
        self.set_pref(_MUTED_PREFS_KEY, json.dumps(sorted(muted)), owner=owner)

    def unmute(self, kind: str, owner: str = SHARED_OWNER) -> None:
        kind = (kind or "").strip().lower()
        muted = set(self.muted_sources(owner))
        muted.discard(kind)
        self.set_pref(_MUTED_PREFS_KEY, json.dumps(sorted(muted)), owner=owner)

    def muted_sources(self, owner: str = SHARED_OWNER) -> list[str]:
        raw = self.get_pref(_MUTED_PREFS_KEY, "[]", owner=owner)
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
        return [k for k in parsed if k in ALERT_KINDS]

    # -- prefs ----------------------------------------------------------- #

    def set_pref(self, key: str, value: Any, owner: str = SHARED_OWNER) -> None:
        key = (key or "").strip()
        if not key:
            raise ValueError("prefs: key is empty")
        owner = (owner or "").strip()[:200] or SHARED_OWNER
        encoded = json.dumps(value, default=str)
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO prefs (owner, key, value) VALUES (?, ?, ?)"
                " ON CONFLICT(owner, key) DO UPDATE SET value=excluded.value",
                (owner, key, encoded),
            )

    def get_pref(self, key: str, default: Any = None, owner: str = SHARED_OWNER) -> Any:
        key = (key or "").strip()
        owner = (owner or "").strip()[:200] or SHARED_OWNER
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM prefs WHERE owner=? AND key=?", (owner, key)
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return default

    def prefs(self, owner: str = SHARED_OWNER) -> dict[str, Any]:
        owner = (owner or "").strip()[:200] or SHARED_OWNER
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT key, value FROM prefs WHERE owner=?", (owner,)
            ).fetchall()
        out: dict[str, Any] = {}
        for row in rows:
            try:
                out[row["key"]] = json.loads(row["value"])
            except (json.JSONDecodeError, TypeError):
                out[row["key"]] = row["value"]
        return out

    # -- recent activity -------------------------------------------------- #

    def log_recent(self, what: str, owner: str = SHARED_OWNER) -> dict[str, Any]:
        what = (what or "").strip()[:300]
        if not what:
            raise ValueError("recent: what is empty")
        owner = (owner or "").strip()[:200] or SHARED_OWNER
        at = _now()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO recent (owner, at, what) VALUES (?, ?, ?)", (owner, at, what)
            )
            record = {"id": cursor.lastrowid, "at": at, "what": what, "owner": owner}
        return record

    def recent(self, limit: int = 25, owner: str = SHARED_OWNER) -> list[dict[str, Any]]:
        limit = max(1, min(200, int(limit)))
        owner = (owner or "").strip()[:200] or SHARED_OWNER
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT id, at, what, owner FROM recent WHERE owner=?"
                " ORDER BY id DESC LIMIT ?",
                (owner, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    # -- last workspace (per device, per owner) -------------------------- #

    def set_workspace(
        self, device: str, workspace: str, owner: str = SHARED_OWNER
    ) -> dict[str, Any]:
        device = (device or "").strip()[:80]
        workspace = (workspace or "").strip()[:200]
        if not device or not workspace:
            raise ValueError("workspace: device and workspace are required")
        owner = (owner or "").strip()[:200] or SHARED_OWNER
        at = _now()
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO workspace (owner, device, workspace, at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(owner, device) DO UPDATE SET workspace=excluded.workspace,"
                " at=excluded.at",
                (owner, device, workspace, at),
            )
        return {"device": device, "workspace": workspace, "at": at, "owner": owner}

    def workspace_for(
        self, device: str, owner: str = SHARED_OWNER
    ) -> dict[str, Any] | None:
        device = (device or "").strip()[:80]
        owner = (owner or "").strip()[:200] or SHARED_OWNER
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT owner, device, workspace, at FROM workspace"
                " WHERE owner=? AND device=?",
                (owner, device),
            ).fetchone()
        return dict(row) if row is not None else None

    def workspaces(self, owner: str = SHARED_OWNER) -> list[dict[str, Any]]:
        owner = (owner or "").strip()[:200] or SHARED_OWNER
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT owner, device, workspace, at FROM workspace WHERE owner=?"
                " ORDER BY at DESC, rowid DESC",
                (owner,),
            ).fetchall()
        return [dict(row) for row in rows]

    # -- full snapshot (for GET /api/state) ------------------------------- #

    def snapshot(self, owner: str = SHARED_OWNER) -> dict[str, Any]:
        return {
            "owner": owner,
            "watchlist": self.watchlist(owner),
            "alerts": self.alerts(owner),
            "muted": self.muted_sources(owner),
            "prefs": self.prefs(owner),
            "recent": self.recent(25, owner),
            "workspaces": self.workspaces(owner),
        }


def default_store() -> StateStore:
    """The persistent store the real serving path mounts (same workspace
    convention as sessions/uploads: ``<workspace>/state/dourmouse.db``)."""
    raw = os.environ.get("DOURMOUSE_WORKSPACE")
    root = Path(raw).expanduser() if raw else Path(__file__).resolve().parent.parent / "workspace"
    return StateStore(root / "state" / "dourmouse.db")
