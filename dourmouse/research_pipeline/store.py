"""SQLite persistence for research records -- same one-connection-per-
operation, WAL-mode discipline as every other real store in this codebase
(research_mesh/store.py, security/sentry.py's SentryStore, goals.py). One
row per question, the whole ResearchRecord serialized as JSON so every
field round-trips losslessly and a killed run resumes exactly where it
stopped, matching this pipeline's own real "auditable, resumable" design
goal.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

from dourmouse.config import workspace_dir

from .core import Claim, Contradiction, ResearchRecord, Stage

# Real gap closed (2026-09-20, named in the standing status report): every
# other domain's own store resolves a workspace-relative default the same
# way (security/sentry.py's own DEFAULT_DB) -- this pipeline's ResearchStore
# previously had no such default, so nothing persisted anywhere unless a
# caller built its own path by hand. Computed once at import time, same as
# sentry.py's own DEFAULT_DB -- callers that need per-test isolation build
# their own ResearchStore(tmp_path) explicitly, same convention already
# established there.
DEFAULT_DB = workspace_dir() / "research_pipeline" / "research.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_records (
    question    TEXT NOT NULL PRIMARY KEY,
    body        TEXT NOT NULL,
    stage       TEXT NOT NULL,
    updated_at  REAL NOT NULL
);
"""


class ResearchStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self._path), timeout=30.0)

    def save(self, record: ResearchRecord, now: float) -> None:
        body = json.dumps(_record_to_dict(record), default=str)
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO research_records (question, body, stage, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(question) DO UPDATE SET "
                "  body=excluded.body, stage=excluded.stage, updated_at=excluded.updated_at",
                (record.question, body, record.stage.name, now),
            )
            conn.commit()

    def load(self, question: str) -> Optional[ResearchRecord]:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT body FROM research_records WHERE question=?", (question,)
            ).fetchone()
        return _record_from_dict(json.loads(row[0])) if row else None

    def list_questions(self) -> list[str]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT question FROM research_records ORDER BY updated_at DESC"
            ).fetchall()
        return [r[0] for r in rows]


def _record_to_dict(r: ResearchRecord) -> dict[str, Any]:
    return {
        "question": r.question,
        "stage": r.stage.name,
        "plan": list(r.plan),
        "sources": list(r.sources),
        "claims": [_claim_to_dict(c) for c in r.claims],
        "contradictions": [_contradiction_to_dict(c) for c in r.contradictions],
        "synthesis": r.synthesis,
    }


def _record_from_dict(d: dict[str, Any]) -> ResearchRecord:
    return ResearchRecord(
        question=d["question"],
        stage=Stage[d["stage"]],
        plan=tuple(d.get("plan", [])),
        sources=tuple(d.get("sources", [])),
        claims=tuple(_claim_from_dict(c) for c in d.get("claims", [])),
        contradictions=tuple(_contradiction_from_dict(c) for c in d.get("contradictions", [])),
        synthesis=d.get("synthesis", ""),
    )


def _claim_to_dict(c: Claim) -> dict[str, Any]:
    return {
        "claim": c.claim, "source_id": c.source_id, "url": c.url,
        "document_hash": c.document_hash, "location": c.location,
        "passage": c.passage, "retrieved_at": c.retrieved_at,
        "agent": c.agent, "status": c.status,
    }


def _claim_from_dict(d: dict[str, Any]) -> Claim:
    return Claim(
        claim=d["claim"], source_id=d["source_id"], url=d["url"],
        document_hash=d["document_hash"], location=d["location"],
        passage=d["passage"], retrieved_at=d["retrieved_at"],
        agent=d["agent"], status=d.get("status", "ACTIVE"),
    )


def _contradiction_to_dict(c: Contradiction) -> dict[str, Any]:
    return {
        "claim_a_id": c.claim_a_id, "claim_b_id": c.claim_b_id,
        "sub_question": c.sub_question, "note": c.note,
    }


def _contradiction_from_dict(d: dict[str, Any]) -> Contradiction:
    return Contradiction(
        claim_a_id=d["claim_a_id"], claim_b_id=d["claim_b_id"],
        sub_question=d["sub_question"], note=d.get("note", ""),
    )
