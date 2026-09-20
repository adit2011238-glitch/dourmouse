"""Persistent, append-only log of real inter-agent traffic (finding #066,
"agent ecosystem" Real Piece #1).

Two real, already-shaped, already-agent-tagged event streams get a durable
home here, closing the "message_bus dies on restart" gap named repeatedly
across the agent-ecosystem design review:

- every real ``message_bus`` post (from ``on_post``, exactly like the
  existing memory mirror in ``webui.py`` -- same observer, same swallow-
  exceptions discipline)
- every real ``delegate_parallel`` branch lifecycle event (``start``/
  ``result``, from the same ``event_sink`` the chat SSE and ActivityTracker
  already consume)

Deliberately NOT in scope here (named, not silently skipped): per-tool-call
reasoning/transcript capture for nested branches. ``tool_use``/``tool_
result`` events only carry the TOOL name, not the calling agent or a call-
instance id, and ``dispatch.py``'s ``thinking_delta`` only fires at
``ctx.depth == 0`` today -- reconstructing a full per-agent chain-of-thought
transcript needs those upstream events tagged first (real, separate,
still-not-built follow-on). This store persists WHO talked to WHOM and
WHICH branch ran WHERE, which is real and enough to back a durable activity
log and a "what happened while I was away" surface -- not yet a full replay
of an agent's own reasoning.

Same one-connection-per-operation, WAL-mode, workspace-relative-DEFAULT_DB
discipline as every other real store in this codebase (device_wiki/store.py,
research_pipeline/store.py, sentry.py's SentryStore).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from dourmouse.config import workspace_dir

DEFAULT_DB = workspace_dir() / "office" / "office_log.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    bus_id      TEXT NOT NULL,
    from_agent  TEXT NOT NULL,
    to_agent    TEXT NOT NULL,
    subject     TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL DEFAULT '',
    ts          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);

CREATE TABLE IF NOT EXISTS fanout_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    phase       TEXT NOT NULL,
    branch_index INTEGER,
    total       INTEGER,
    agent       TEXT NOT NULL DEFAULT '',
    ok          INTEGER,
    error       TEXT NOT NULL DEFAULT '',
    elapsed_s   REAL,
    ts          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fanout_run ON fanout_events(run_id);
CREATE INDEX IF NOT EXISTS idx_fanout_ts ON fanout_events(ts);
"""


class OfficeLogger:
    """Thread-safe, append-only SQLite log. A pure observer, same contract
    as ``message_bus.on_post``/``ActivityTracker.on_event``: a raising
    write must never break the real bus post or the real chat turn it came
    from, so every public write method swallows its own exceptions."""

    def __init__(self, path: str | Path = DEFAULT_DB) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self._path), timeout=30.0)

    # -- writes ------------------------------------------------------------ #

    def log_message(self, msg: dict[str, Any]) -> None:
        """Observer for ``message_bus.on_post`` -- ``msg`` is the exact dict
        ``MessageBus.post`` returns/passes to its observers."""
        try:
            with self._lock, self._conn() as conn:
                conn.execute(
                    "INSERT INTO messages (bus_id, from_agent, to_agent, subject, body, ts) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        str(msg.get("id", "")),
                        str(msg.get("from", "")),
                        str(msg.get("to", "")),
                        str(msg.get("subject", "")),
                        str(msg.get("body", "")),
                        time.time(),
                    ),
                )
                conn.commit()
        except Exception:
            pass  # an observer must never break the bus (see on_post)

    def on_event(self, entry: dict[str, Any]) -> None:
        """Observer for the chat ``event_sink`` -- only
        ``delegate_parallel_branch`` events are persisted here; everything
        else is a no-op (see module docstring for why)."""
        try:
            if entry.get("type") != "delegate_parallel_branch":
                return
            with self._lock, self._conn() as conn:
                conn.execute(
                    "INSERT INTO fanout_events "
                    "(run_id, phase, branch_index, total, agent, ok, error, elapsed_s, ts) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(entry.get("run_id", "")),
                        str(entry.get("phase", "")),
                        entry.get("index"),
                        entry.get("total"),
                        str(entry.get("agent", "")),
                        None if entry.get("ok") is None else int(bool(entry.get("ok"))),
                        str(entry.get("error", "")),
                        entry.get("elapsed_s"),
                        time.time(),
                    ),
                )
                conn.commit()
        except Exception:
            pass  # an observer must never break a real chat turn

    # -- reads ------------------------------------------------------------- #

    def recent_messages(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT bus_id, from_agent, to_agent, subject, body, ts "
                "FROM messages ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {"id": r[0], "from": r[1], "to": r[2], "subject": r[3], "body": r[4], "ts": r[5]}
            for r in rows
        ]

    def recent_fanout_events(self, limit: int = 100, run_id: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        with self._lock, self._conn() as conn:
            if run_id:
                rows = conn.execute(
                    "SELECT run_id, phase, branch_index, total, agent, ok, error, elapsed_s, ts "
                    "FROM fanout_events WHERE run_id=? ORDER BY id DESC LIMIT ?",
                    (run_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT run_id, phase, branch_index, total, agent, ok, error, elapsed_s, ts "
                    "FROM fanout_events ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [
            {
                "run_id": r[0], "phase": r[1], "index": r[2], "total": r[3], "agent": r[4],
                "ok": None if r[5] is None else bool(r[5]), "error": r[6], "elapsed_s": r[7], "ts": r[8],
            }
            for r in rows
        ]
