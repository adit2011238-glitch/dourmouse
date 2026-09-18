"""Persistent Goal/Task store — the durable backbone of the autonomous
agent runtime (docs/GODSPEED_ROADMAP.md Phase 2).

Every other subsystem in this codebase that runs "in the background"
(``schedules.py``'s ``SchedulerRunner``, ``live_runtime.py``) is either a
single recurring tool call or a hardcoded poll — neither can hold a
multi-step plan, resume it after a crash, or tell an inspector which step
it is currently on (confirmed gap, see docs/ARCHITECTURE.md "What's
missing for the new Goal/Task runtime"). This module is the missing
piece: a real persisted task graph a worker can pick up from cold, one
step at a time, forever, independent of any HTTP connection.

Deliberately mirrors ``state_store.StateStore``'s established shape
(SQLite + WAL, one connection per operation in file mode, a single
shared in-memory connection for hermetic tests, honest ``ValueError`` on
bad input, deterministic ordering) rather than inventing a new
persistence idiom. A goal's task graph and its full event history are
the ONLY authoritative record of "what did the agent actually do" — nothing
here is a cache of state that lives somewhere else.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: Goal lifecycle — exactly the states a real autonomous run needs to be
#: honest about, not just "running"/"done". A goal is never silently
#: treated as complete just because one model response ended.
GOAL_STATES = frozenset({
    "CREATED", "PLANNING", "READY", "EXECUTING", "VERIFYING",
    "WAITING_FOR_APPROVAL", "WAITING_FOR_AUTHENTICATION", "BLOCKED",
    "PAUSED", "RECOVERING", "REPLANNING", "COMPLETED", "FAILED",
    "CANCELLED", "EXPIRED",
})
#: Goal states the worker actively advances on its own each tick.
GOAL_ACTIVE_STATES = frozenset({"PLANNING", "READY", "EXECUTING", "VERIFYING", "RECOVERING", "REPLANNING"})
#: Goal states that mean "stopped, nothing more happens without a human or a new goal."
GOAL_TERMINAL_STATES = frozenset({"COMPLETED", "FAILED", "CANCELLED", "EXPIRED"})

TASK_STATES = frozenset({
    "PENDING", "READY", "RUNNING", "VERIFYING", "WAITING_FOR_APPROVAL",
    "BLOCKED", "RETRYING", "COMPLETED", "FAILED", "CANCELLED",
})
TASK_TERMINAL_STATES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})

PRIORITIES = frozenset({"critical", "high", "normal", "low", "background"})

#: Event types an inspector actually needs (spec: "expose structured
#: operational information ... never raw hidden chain-of-thought").
EVENT_TYPES = frozenset({
    "goal_created", "goal_status_changed", "task_created", "task_status_changed",
    "tool_call", "tool_result", "verification", "approval_requested",
    "approval_resolved", "recovery_attempted", "replanned", "note",
})

_TABLE_DDL = {
    "goals": (
        "CREATE TABLE goals ("
        " id TEXT PRIMARY KEY, owner TEXT NOT NULL DEFAULT '*',"
        " objective TEXT NOT NULL, status TEXT NOT NULL,"
        " priority TEXT NOT NULL DEFAULT 'normal',"
        " success_criteria TEXT NOT NULL DEFAULT '[]',"
        " resource_limits TEXT NOT NULL DEFAULT '{}',"
        " deadline TEXT, current_task_id TEXT, blocked_reason TEXT,"
        " result TEXT, session_id TEXT,"
        " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    ),
    "tasks": (
        "CREATE TABLE tasks ("
        " id TEXT PRIMARY KEY, goal_id TEXT NOT NULL, parent_task_id TEXT,"
        " description TEXT NOT NULL, status TEXT NOT NULL,"
        " depends_on TEXT NOT NULL DEFAULT '[]', assigned_agent TEXT,"
        " attempt_count INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3,"
        " last_error TEXT, result TEXT,"
        " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    ),
    "goal_events": (
        "CREATE TABLE goal_events ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, goal_id TEXT NOT NULL, task_id TEXT,"
        " type TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}', at TEXT NOT NULL)"
    ),
}
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_tasks_goal ON tasks(goal_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_goal ON goal_events(goal_id)",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class GoalStore:
    """SQLite-backed Goal/Task persistence. Thread-safe, crash-safe (WAL),
    same connection-handling shape as ``state_store.StateStore``."""

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self.path = Path(path) if path is not None else None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._db_path = str(self.path)
            self._conn = None
        else:
            self._db_path = ":memory:"
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._lock = threading.Lock()
        self._closed = False
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError("goal store is closed")
        if self._conn is not None:
            self._conn.row_factory = sqlite3.Row
            return self._conn
        connection = sqlite3.connect(self._db_path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            for table, ddl in _TABLE_DDL.items():
                exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
                if not exists:
                    connection.execute(ddl)
            for stmt in _INDEXES:
                connection.execute(stmt)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
                self._closed = True
                return
            if self.path is not None:
                try:
                    with self._connect() as connection:
                        connection.execute("PRAGMA wal_checkpoint(FULL)")
                except sqlite3.Error:
                    pass
            self._closed = True

    # -- goals ------------------------------------------------------------ #

    def create_goal(
        self,
        objective: str,
        owner: str = "*",
        priority: str = "normal",
        success_criteria: list[str] | None = None,
        resource_limits: dict[str, Any] | None = None,
        deadline: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        objective = (objective or "").strip()
        if not objective:
            raise ValueError("goals: objective is empty")
        priority = (priority or "normal").strip().lower()
        if priority not in PRIORITIES:
            raise ValueError(f"goals: unknown priority {priority!r} (allowed: {sorted(PRIORITIES)})")
        goal_id = _new_id("goal")
        now = _now()
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO goals (id, owner, objective, status, priority,"
                " success_criteria, resource_limits, deadline, session_id, created_at, updated_at)"
                " VALUES (?, ?, ?, 'CREATED', ?, ?, ?, ?, ?, ?, ?)",
                (
                    goal_id, (owner or "*").strip() or "*", objective, priority,
                    json.dumps(success_criteria or []), json.dumps(resource_limits or {}),
                    deadline, session_id, now, now,
                ),
            )
        self._log_event(goal_id, None, "goal_created", {"objective": objective})
        return self.get_goal(goal_id)  # type: ignore[return-value]

    def get_goal(self, goal_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
        return self._serialize_goal(row) if row is not None else None

    def list_goals(self, owner: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM goals WHERE 1=1"
        params: list[Any] = []
        if owner is not None:
            query += " AND owner=?"
            params.append(owner)
        if status is not None:
            query += " AND status=?"
            params.append(status)
        query += " ORDER BY created_at DESC"
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._serialize_goal(r) for r in rows]

    def active_goals(self) -> list[dict[str, Any]]:
        """Goals the worker should keep advancing on its own this tick."""
        placeholders = ",".join("?" for _ in GOAL_ACTIVE_STATES)
        with self._lock, self._connect() as connection:
            # placeholders is only "?" marks, one per entry in the fixed, hardcoded
            # GOAL_ACTIVE_STATES frozenset, never a value -- the real values are
            # bound below via tuple(GOAL_ACTIVE_STATES).
            rows = connection.execute(
                f"SELECT * FROM goals WHERE status IN ({placeholders}) ORDER BY"  # noqa: S608
                " CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2"
                " WHEN 'low' THEN 3 ELSE 4 END, created_at ASC",
                tuple(GOAL_ACTIVE_STATES),
            ).fetchall()
        return [self._serialize_goal(r) for r in rows]

    def update_goal_status(
        self, goal_id: str, status: str, blocked_reason: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> bool:
        status = (status or "").strip().upper()
        if status not in GOAL_STATES:
            raise ValueError(f"goals: unknown status {status!r} (allowed: {sorted(GOAL_STATES)})")
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE goals SET status=?, blocked_reason=?, result=COALESCE(?, result),"
                " updated_at=? WHERE id=?",
                (status, blocked_reason, json.dumps(result) if result is not None else None, _now(), goal_id),
            )
            changed = cursor.rowcount > 0
        if changed:
            self._log_event(goal_id, None, "goal_status_changed", {"status": status, "blocked_reason": blocked_reason})
        return changed

    def set_current_task(self, goal_id: str, task_id: str | None) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE goals SET current_task_id=?, updated_at=? WHERE id=?",
                (task_id, _now(), goal_id),
            )

    def cancel_goal(self, goal_id: str) -> bool:
        """Mark a goal cancelled and cancel every non-terminal task under
        it. The worker checks goal status before starting any task, so
        this is safe even if a task is mid-flight this tick — it simply
        will not be picked up again."""
        with self._lock, self._connect() as connection:
            # Both "NOT IN (...)" placeholder strings below are only "?" marks,
            # one per entry in a fixed, hardcoded state frozenset, never a value.
            cursor = connection.execute(
                "UPDATE goals SET status='CANCELLED', updated_at=? WHERE id=? AND status NOT IN ({})".format(  # noqa: S608
                    ",".join("?" for _ in GOAL_TERMINAL_STATES)
                ),
                (_now(), goal_id, *GOAL_TERMINAL_STATES),
            )
            changed = cursor.rowcount > 0
            if changed:
                placeholders = ",".join("?" for _ in TASK_TERMINAL_STATES)
                connection.execute(
                    f"UPDATE tasks SET status='CANCELLED', updated_at=? WHERE goal_id=? AND status NOT IN ({placeholders})",  # noqa: S608
                    (_now(), goal_id, *TASK_TERMINAL_STATES),
                )
        if changed:
            self._log_event(goal_id, None, "goal_status_changed", {"status": "CANCELLED"})
        return changed

    @staticmethod
    def _serialize_goal(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "owner": row["owner"], "objective": row["objective"],
            "status": row["status"], "priority": row["priority"],
            "success_criteria": json.loads(row["success_criteria"]),
            "resource_limits": json.loads(row["resource_limits"]),
            "deadline": row["deadline"], "current_task_id": row["current_task_id"],
            "blocked_reason": row["blocked_reason"],
            "result": json.loads(row["result"]) if row["result"] else None,
            "session_id": row["session_id"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    # -- tasks -------------------------------------------------------------- #

    def create_task(
        self,
        goal_id: str,
        description: str,
        depends_on: list[str] | None = None,
        assigned_agent: str | None = None,
        parent_task_id: str | None = None,
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        description = (description or "").strip()
        if not description:
            raise ValueError("tasks: description is empty")
        if self.get_goal(goal_id) is None:
            raise ValueError(f"tasks: unknown goal {goal_id!r}")
        task_id = _new_id("task")
        now = _now()
        status = "READY" if not depends_on else "PENDING"
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO tasks (id, goal_id, parent_task_id, description, status,"
                " depends_on, assigned_agent, max_attempts, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id, goal_id, parent_task_id, description, status,
                    json.dumps(depends_on or []), assigned_agent, max(1, int(max_attempts)), now, now,
                ),
            )
        self._log_event(goal_id, task_id, "task_created", {"description": description, "depends_on": depends_on or []})
        return self.get_task(task_id)  # type: ignore[return-value]

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._serialize_task(row) if row is not None else None

    def list_tasks(self, goal_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE goal_id=? ORDER BY created_at ASC", (goal_id,)
            ).fetchall()
        return [self._serialize_task(r) for r in rows]

    def ready_tasks(self, goal_id: str) -> list[dict[str, Any]]:
        """Tasks whose dependencies are ALL completed and are themselves
        still pending/ready — what the worker is allowed to run next.
        Promotes PENDING -> READY the moment dependencies clear, so a
        caller never has to re-derive this by hand."""
        tasks = self.list_tasks(goal_id)
        by_id = {t["id"]: t for t in tasks}
        ready: list[dict[str, Any]] = []
        for t in tasks:
            if t["status"] not in ("PENDING", "READY"):
                continue
            deps = t["depends_on"]
            if all(by_id.get(d, {}).get("status") == "COMPLETED" for d in deps):
                if t["status"] == "PENDING":
                    self.update_task_status(t["id"], "READY")
                    t = self.get_task(t["id"])  # type: ignore[assignment]
                ready.append(t)  # type: ignore[arg-type]
        return ready

    def update_task_status(
        self, task_id: str, status: str, result: dict[str, Any] | None = None,
        error: str | None = None, increment_attempt: bool = False,
    ) -> bool:
        status = (status or "").strip().upper()
        if status not in TASK_STATES:
            raise ValueError(f"tasks: unknown status {status!r} (allowed: {sorted(TASK_STATES)})")
        task = self.get_task(task_id)
        if task is None:
            return False
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE tasks SET status=?, result=COALESCE(?, result), last_error=?,"
                " attempt_count=attempt_count + ?, updated_at=? WHERE id=?",
                (
                    status, json.dumps(result) if result is not None else None, error,
                    1 if increment_attempt else 0, _now(), task_id,
                ),
            )
        self._log_event(task["goal_id"], task_id, "task_status_changed", {"status": status, "error": error})
        return True

    @staticmethod
    def _serialize_task(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "goal_id": row["goal_id"], "parent_task_id": row["parent_task_id"],
            "description": row["description"], "status": row["status"],
            "depends_on": json.loads(row["depends_on"]), "assigned_agent": row["assigned_agent"],
            "attempt_count": row["attempt_count"], "max_attempts": row["max_attempts"],
            "last_error": row["last_error"],
            "result": json.loads(row["result"]) if row["result"] else None,
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    # -- events (the real, inspectable audit trail) ------------------------- #

    def log_event(self, goal_id: str, type: str, detail: dict[str, Any] | None = None, task_id: str | None = None) -> dict[str, Any]:
        if type not in EVENT_TYPES:
            raise ValueError(f"goal_events: unknown type {type!r} (allowed: {sorted(EVENT_TYPES)})")
        return self._log_event(goal_id, task_id, type, detail or {})

    def _log_event(self, goal_id: str, task_id: str | None, type: str, detail: dict[str, Any]) -> dict[str, Any]:
        at = _now()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO goal_events (goal_id, task_id, type, detail, at) VALUES (?, ?, ?, ?, ?)",
                (goal_id, task_id, type, json.dumps(detail, default=str), at),
            )
            event_id = cursor.lastrowid
        return {"id": event_id, "goal_id": goal_id, "task_id": task_id, "type": type, "detail": detail, "at": at}

    def goal_events(self, goal_id: str, limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(2000, int(limit)))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM goal_events WHERE goal_id=? ORDER BY id ASC LIMIT ?",
                (goal_id, limit),
            ).fetchall()
        return [
            {
                "id": r["id"], "goal_id": r["goal_id"], "task_id": r["task_id"],
                "type": r["type"], "detail": json.loads(r["detail"]), "at": r["at"],
            }
            for r in rows
        ]

    # -- full view (for the API / UI) --------------------------------------- #

    def goal_snapshot(self, goal_id: str) -> dict[str, Any] | None:
        goal = self.get_goal(goal_id)
        if goal is None:
            return None
        goal["tasks"] = self.list_tasks(goal_id)
        goal["events"] = self.goal_events(goal_id, limit=200)
        return goal

    def all_events(self, since: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        """The cross-goal audit trail (2026-09-18): ``goal_events`` only
        ever answered "what happened on ONE goal" -- the founding spec's
        own audit-trail requirement ("the user should be able to inspect
        what the assistant actually did") is a global question, not a
        per-goal one. Same table, same real data, a second query shape
        over it -- not a second logging system.

        ``since`` is an ISO-8601 string, matching ``at``'s own real stored
        type (``_now()``'s format) -- NOT a Unix timestamp. ISO-8601's
        zero-padded fields sort correctly as plain text, so SQLite's
        ordinary string comparison already does the right thing; no
        epoch conversion needed or wanted.
        """
        limit = max(1, min(2000, int(limit)))
        query = "SELECT * FROM goal_events WHERE 1=1"
        params: list[Any] = []
        if since:
            query += " AND at>=?"
            params.append(since)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [
            {
                "id": r["id"], "goal_id": r["goal_id"], "task_id": r["task_id"],
                "type": r["type"], "detail": json.loads(r["detail"]), "at": r["at"],
            }
            for r in rows
        ]

    def export_events_markdown(self, goal_id: str | None = None, since: str | None = None, limit: int = 500) -> str:
        """A real, human-readable, audit-ready report -- the founding
        spec's own words ("Implement an event log/audit trail... The user
        should be able to inspect what the assistant actually did") and
        the same shape a real SOC-style audit export takes. Renders
        exactly what's in the database; never summarizes or drops an
        entry, since a redacted-looking audit trail is worse than a
        verbose one. ``since`` is an ISO-8601 string, matching
        ``all_events``'s own convention -- see its docstring for why.
        """
        events = self.goal_events(goal_id, limit=limit) if goal_id else self.all_events(since=since, limit=limit)
        if goal_id and since:
            events = [e for e in events if e["at"] >= since]
        title = f"# Dourmouse Audit Trail\n\n"
        scope = f"Goal `{goal_id}`" if goal_id else "All goals"
        lines = [title, f"Scope: {scope}", f"Entries: {len(events)}", ""]
        # goal_events() returns oldest-first; all_events() returns
        # newest-first (see their own docstrings) -- normalize to
        # chronological order for a report a human reads top to bottom.
        ordered = list(reversed(events)) if not goal_id else events
        for e in ordered:
            when = datetime.fromisoformat(e["at"]).strftime("%Y-%m-%d %H:%M:%S UTC")
            where = f"goal {e['goal_id']}" + (f" / task {e['task_id']}" if e["task_id"] else "")
            lines.append(f"## {when} ({e['type']})")
            lines.append(f"*{where}*")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(e["detail"], indent=2, default=str))
            lines.append("```")
            lines.append("")
        return "\n".join(lines)


def default_store() -> GoalStore:
    """The persistent store the real serving path mounts (same workspace
    convention as sessions/state: ``<workspace>/state/goals.db``)."""
    raw = os.environ.get("DOURMOUSE_WORKSPACE")
    root = Path(raw).expanduser() if raw else Path(__file__).resolve().parent.parent / "workspace"
    return GoalStore(root / "state" / "goals.db")


# --------------------------------------------------------------------------- #
# Process-wide singleton — tool handlers and the goal runtime share ONE store,
# same pattern as message_bus.get_message_bus()/set_message_bus().
# --------------------------------------------------------------------------- #

_lock = threading.Lock()
_DEFAULT_STORE: GoalStore | None = None


def get_goal_store() -> GoalStore:
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        with _lock:
            if _DEFAULT_STORE is None:
                _DEFAULT_STORE = default_store()
    return _DEFAULT_STORE


def set_goal_store(store: GoalStore | None) -> None:
    """Replace the process singleton (test isolation). None resets it so
    the next get_goal_store() builds a fresh file-backed default."""
    global _DEFAULT_STORE
    with _lock:
        _DEFAULT_STORE = store
