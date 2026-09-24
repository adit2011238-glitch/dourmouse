"""Persistent, append-only log of real inter-agent traffic (finding #066,
extended by #067 -- "agent ecosystem" Real Pieces #1 and #2).

Three real event streams get a durable home here, closing both the
"message_bus dies on restart" gap AND the "no full chain-of-thought replay"
gap named repeatedly across the agent-ecosystem design review:

- every real ``message_bus`` post (from ``on_post``, exactly like the
  existing memory mirror in ``webui.py`` -- same observer, same swallow-
  exceptions discipline)
- every real ``delegate_parallel`` branch lifecycle event (``start``/
  ``result``, from the same ``event_sink`` the chat SSE and ActivityTracker
  already consume)
- every real per-run dispatch event (finding #067: ``dispatch.py``'s
  ``_emit_event`` now additively tags ``tool_use``/``tool_result``/
  ``thinking_delta``/``assistant_delta``/``assistant_text``/``brain``
  entries with the REAL calling agent -- ``DispatchContext.forced_agent``,
  or ``"orchestrator"`` for the untargeted top-level turn -- and a real,
  per-run ``call_id`` that tells apart two concurrent runs against the same
  agent, something ``forced_agent`` alone cannot do). This is what makes an
  on-demand transcript viewer real instead of scripted demo content: query
  by ``agent`` or by ``call_id`` and get back that run's own real events,
  in order.

Honest scope limit, not silently skipped: this stores the real event
STREAM (tool calls, reasoning deltas, text) per agent/call_id -- it does not
itself reconstruct a formatted "conversation" (grouping deltas into whole
messages, resolving a meeting's several concurrent call_ids into one
timeline) -- that assembly is a read-side/UI concern layered on top of this
real, ordered, durable data.

Same one-connection-per-operation, WAL-mode, workspace-relative default_db()
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


def default_db() -> Path:
    """Resolved on every call, never at import time (finding #084): an
    import-time constant froze whatever DOURMOUSE_WORKSPACE was when the
    module was first imported, which let the test suite write into the
    real workspace."""
    return workspace_dir() / "office" / "office_log.db"

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

CREATE TABLE IF NOT EXISTS agent_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent       TEXT NOT NULL,
    call_id     TEXT NOT NULL,
    depth       INTEGER,
    type        TEXT NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL DEFAULT '',
    ts          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_events_agent ON agent_events(agent);
CREATE INDEX IF NOT EXISTS idx_agent_events_call ON agent_events(call_id);
CREATE INDEX IF NOT EXISTS idx_agent_events_ts ON agent_events(ts);
"""

#: Finding #067: which dispatch.py event types carry real per-agent
#: reasoning/activity worth persisting for an on-demand transcript.
#: Deliberately excludes fanout lifecycle events (already handled by
#: fanout_events above) and low-signal control events (e.g. "stop").
_AGENT_EVENT_TYPES = frozenset({
    "tool_use", "tool_result", "thinking_delta", "assistant_delta",
    "assistant_text", "brain",
})

#: Per-row text cap -- generous enough for a real tool result or a full
#: assistant_text, small enough that one runaway entry can never blow up
#: the log (same discipline message_bus.post's own 1200-char body cap uses).
_MAX_EVENT_TEXT = 4000


class OfficeLogger:
    """Thread-safe, append-only SQLite log. A pure observer, same contract
    as ``message_bus.on_post``/``ActivityTracker.on_event``: a raising
    write must never break the real bus post or the real chat turn it came
    from, so every public write method swallows its own exceptions."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else default_db()
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
        """Observer for the chat ``event_sink``. ``delegate_parallel_branch``
        events go to ``fanout_events``; the real-agent-tagged types in
        ``_AGENT_EVENT_TYPES`` (finding #067) go to ``agent_events``, but
        only once actually tagged with a real ``call_id`` (an untagged
        event, e.g. from a caller that never passed ``ctx`` to
        ``_emit_event``, has nothing reliable to key on and is skipped
        rather than logged under a fabricated identity). Everything else is
        a no-op."""
        try:
            etype = entry.get("type")
            if etype == "delegate_parallel_branch":
                self._log_fanout(entry)
            elif etype in _AGENT_EVENT_TYPES and entry.get("call_id"):
                self._log_agent_event(entry)
        except Exception:
            pass  # an observer must never break a real chat turn

    def _log_fanout(self, entry: dict[str, Any]) -> None:
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

    def _log_agent_event(self, entry: dict[str, Any]) -> None:
        text = str(entry.get("text") or "")[:_MAX_EVENT_TEXT]
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO agent_events (agent, call_id, depth, type, name, text, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(entry.get("agent", "")),
                    str(entry.get("call_id", "")),
                    entry.get("depth"),
                    str(entry.get("type", "")),
                    str(entry.get("name", "")),
                    text,
                    time.time(),
                ),
            )
            conn.commit()

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

    def transcript(
        self, *, agent: str | None = None, call_id: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Finding #067: the real, on-demand transcript a viewer would
        render -- every persisted dispatch event for one agent (its whole
        history across runs) or one exact ``call_id`` (one specific run),
        OLDEST FIRST (a transcript reads top-to-bottom, unlike the other
        "recent" methods here which are newest-first activity feeds)."""
        limit = max(1, min(int(limit), 2000))
        clauses: list[str] = []
        params: list[Any] = []
        if agent:
            clauses.append("agent=?")
            params.append(agent)
        if call_id:
            clauses.append("call_id=?")
            params.append(call_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                f"SELECT agent, call_id, depth, type, name, text, ts FROM agent_events "
                f"{where} ORDER BY id ASC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [
            {"agent": r[0], "call_id": r[1], "depth": r[2], "type": r[3], "name": r[4], "text": r[5], "ts": r[6]}
            for r in rows
        ]
