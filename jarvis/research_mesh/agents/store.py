"""SQLite persistence for field-agent qualification state.

Why SQLite, one connection per operation:

- The pipeline can be killed at any moment (power loss, crash, user interrupt)
  and must resume from the exact persisted state. SQLite with WAL gives atomic
  single-row commits — a write either fully lands or it doesn't, so a crash
  mid-update can never leave a half-applied state transition.
- One connection per operation (open -> commit -> close) avoids cross-thread
  connection sharing entirely, which is the classic source of SQLite
  "database is locked" races. WAL mode additionally lets concurrent readers
  proceed during a writer.

The whole AgentRecord serializes into one row (JSON body) so every field of the
state machine round-trips losslessly; attempts are embedded in the same body so
a record is always internally consistent.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from .core import AgentRecord, Status, StudyPhase

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    domain      TEXT NOT NULL,
    field       TEXT NOT NULL,
    body        TEXT NOT NULL,          -- full AgentRecord as JSON
    status      TEXT NOT NULL,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (domain, field)
);
CREATE INDEX IF NOT EXISTS idx_agents_status ON agents(status);
"""


class AgentStore:
    """Thread-safe, resumable store keyed by (domain, field)."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init(self) -> None:
        with self._lock:
            conn = self._conn()
            try:
                conn.executescript(_SCHEMA)
                conn.commit()
            finally:
                conn.close()

    # -- writes -----------------------------------------------------------

    def save(self, record: AgentRecord, now: float) -> None:
        body = json.dumps(_record_to_dict(record), default=str)
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO agents (domain, field, body, status, updated_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(domain, field) DO UPDATE SET "
                    "  body=excluded.body, status=excluded.status, "
                    "  updated_at=excluded.updated_at",
                    (record.domain, record.field, body, record.status.name, now),
                )
                conn.commit()
            finally:
                conn.close()

    # -- reads ------------------------------------------------------------

    def load(self, domain: str, field: str) -> Optional[AgentRecord]:
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT body FROM agents WHERE domain=? AND field=?",
                    (domain, field),
                ).fetchone()
            finally:
                conn.close()
        return _record_from_dict(json.loads(row[0])) if row else None

    def load_all(self) -> list[AgentRecord]:
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute("SELECT body FROM agents").fetchall()
            finally:
                conn.close()
        return [_record_from_dict(json.loads(r[0])) for r in rows]

    def count_by_status(self) -> dict[str, int]:
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT status, COUNT(*) FROM agents GROUP BY status"
                ).fetchall()
            finally:
                conn.close()
        return {s: n for s, n in rows}


def _record_to_dict(r: AgentRecord) -> dict:
    return {
        "domain": r.domain,
        "field": r.field,
        "status": r.status.name,
        "study_minutes_used": r.study_minutes_used,
        "phase": r.phase.name if r.phase else None,
        "dossier": _dossier_to_dict(r.dossier) if r.dossier else None,
        "current_iteration": r.current_iteration,
        "attempts_on_iteration": r.attempts_on_iteration,
        "passed_iterations": list(r.passed_iterations),
        "history": [_attempt_to_dict(a) for a in r.history],
        "remediation_budget_left": r.remediation_budget_left,
    }


def _record_from_dict(d: dict) -> AgentRecord:
    return AgentRecord(
        domain=d["domain"],
        field=d["field"],
        status=Status[d["status"]],
        study_minutes_used=d.get("study_minutes_used", 0),
        phase=StudyPhase[d["phase"]] if d.get("phase") else None,
        dossier=_dossier_from_dict(d["dossier"]) if d.get("dossier") else None,
        current_iteration=d.get("current_iteration"),
        attempts_on_iteration=d.get("attempts_on_iteration", 0),
        passed_iterations=tuple(d.get("passed_iterations", [])),
        history=tuple(_attempt_from_dict(a) for a in d.get("history", [])),
        remediation_budget_left=d.get("remediation_budget_left", 0),
    )


def _dossier_to_dict(d) -> dict:
    from .core import StudyDossier

    assert isinstance(d, StudyDossier)
    return {
        "field": d.field,
        "domain": d.domain,
        "concept_cards": [
            {"concept": c.concept, "summary": c.summary,
             "confidence": c.confidence, "sources": list(c.sources)}
            for c in d.concept_cards
        ],
        "canonical_literature": list(d.canonical_literature),
        "worked_examples": list(d.worked_examples),
        "practice_scores": list(d.practice_scores),
        "held_out": list(d.held_out),
        "complete": d.complete,
    }


def _dossier_from_dict(d: dict):
    from .core import ConceptCard, StudyDossier

    return StudyDossier(
        field=d["field"],
        domain=d["domain"],
        concept_cards=tuple(
            ConceptCard(concept=c["concept"], summary=c["summary"],
                        confidence=c["confidence"], sources=tuple(c["sources"]))
            for c in d.get("concept_cards", [])
        ),
        canonical_literature=tuple(d.get("canonical_literature", [])),
        worked_examples=tuple(d.get("worked_examples", [])),
        practice_scores=tuple(d.get("practice_scores", [])),
        held_out=tuple(d.get("held_out", [])),
        complete=d.get("complete", False),
    )


def _attempt_to_dict(a) -> dict:
    from .core import ExamAttempt

    assert isinstance(a, ExamAttempt)
    return {
        "iteration_id": a.iteration_id,
        "attempt": a.attempt,
        "score": a.score,
        "passed": a.passed,
        "citations_verified": a.citations_verified,
        "fabricated_citations": a.fabricated_citations,
        "feedback": a.feedback,
        "transcript": list(a.transcript),
    }


def _attempt_from_dict(d: dict):
    from .core import ExamAttempt

    return ExamAttempt(
        iteration_id=d["iteration_id"],
        attempt=d.get("attempt", 0),
        score=d.get("score", 0.0),
        passed=d.get("passed", False),
        citations_verified=d.get("citations_verified", False),
        fabricated_citations=d.get("fabricated_citations", 0),
        feedback=d.get("feedback", ""),
        transcript=tuple(d.get("transcript", [])),
    )
