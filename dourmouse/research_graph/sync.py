"""Move research records into the object graph (R1 migration, finding #095).

``sync_record`` maps one ``ResearchRecord`` onto graph objects and edges with
deterministic ids, so running it again is a no-op and running it after the
record grew adds only what is new. ``migrate_legacy`` runs it over every row of
the old one-JSON-blob ``research_records`` table. The old table is read, never
modified or dropped: the migration cannot lose what it has not copied.

Mapping (every field of every Claim lands somewhere; the tests check each):

  question            -> project + research_question (part_of project)
  plan sub-questions  -> research_question, question decomposes_into it
  stage               -> task "research pipeline" (about the question)
  claim.url           -> source
  claim.document_hash -> document (raw_sha256 when the claim has a final_url,
                         i.e. was made after #089; legacy_text_hash before),
                         derived_from the requested source and, after a
                         redirect, the final-URL source too
  claim.passage       -> passage (part_of document), with its location
  (the quote in use)  -> evidence extracted_from passage, claim supported_by it
  claim               -> claim (versioned: a later status change is a revision),
                         answers its sub-question
  contradiction       -> contradiction about both claims,
                         claim_a contradicted_by claim_b
  synthesis           -> result answers the question (one per distinct text,
                         so every past synthesis stays in the history)
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from dourmouse.research_pipeline.core import Claim, ResearchRecord, contradiction_key

from .model import OBJECT_TYPES, Mutability
from .store import GraphStore

MIGRATOR = "migration:research_records"


def _h(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:24]


def claim_fingerprint(claim: Claim) -> str:
    """The same shape stages._claim_fingerprint and Contradiction ids use."""
    return f"{claim.source_id}:{hashlib.sha256(claim.claim.encode('utf-8')).hexdigest()[:12]}"


def ids_for(question: str) -> dict[str, str]:
    return {"project": "proj-" + _h(question), "question": "q-" + _h(question), "task": "task-" + _h(question, "pipeline")}


def _ensure(store: GraphStore, obj_type: str, obj_id: str, body: dict[str, Any], *, by: str, project_id: str) -> None:
    """Create, or bring a VERSIONED object's current version in line with
    ``body`` by revising it. An IMMUTABLE object whose content differs is a
    real integrity error and is raised, never overwritten."""
    try:
        cur = store.get(obj_type, obj_id)
    except KeyError:
        store.put(obj_type, obj_id, body, created_by=by, project_id=project_id)
        return
    if cur.body == body:
        return
    if OBJECT_TYPES[obj_type].mutability is Mutability.IMMUTABLE:
        store.put(obj_type, obj_id, body, created_by=by, project_id=project_id)  # raises the conflict
        return
    changes = {k: v for k, v in body.items() if cur.body.get(k) != v}
    store.revise(obj_type, obj_id, changes, created_by=by)


def sync_record(store: GraphStore, record: ResearchRecord, *, by: str = MIGRATOR) -> dict[str, str]:
    """All-or-nothing: one transaction, so a failure part-way leaves the
    graph exactly as it was."""
    with store.transaction():
        return _sync_record(store, record, by=by)


def _sync_record(store: GraphStore, record: ResearchRecord, *, by: str) -> dict[str, str]:
    ids = ids_for(record.question)
    pid = ids["project"]
    _ensure(store, "project", pid, {"name": record.question}, by=by, project_id=pid)
    _ensure(store, "research_question", ids["question"], {"text": record.question}, by=by, project_id=pid)
    store.link(("research_question", ids["question"]), "part_of", ("project", pid), created_by=by)
    _ensure(store, "task", ids["task"], {"title": "research pipeline", "stage": record.stage.name}, by=by, project_id=pid)
    store.link(("task", ids["task"]), "about", ("research_question", ids["question"]), created_by=by)

    sub_ids: dict[str, str] = {}
    for sub in record.plan:
        sid = "q-" + _h(record.question, sub)
        sub_ids[sub] = sid
        _ensure(store, "research_question", sid, {"text": sub}, by=by, project_id=pid)
        store.link(("research_question", ids["question"]), "decomposes_into", ("research_question", sid), created_by=by)

    for url in record.sources:
        _ensure(store, "source", "src-" + _h(url), {"url": url}, by=by, project_id=pid)

    claim_ids: dict[str, str] = {}
    claim_tasks: list[tuple[str, str]] = []
    for c in record.claims:
        author = c.agent or by
        src_id = "src-" + _h(c.url)
        _ensure(store, "source", src_id, {"url": c.url}, by=author, project_id=pid)
        # A document IS its bytes: its body holds only the hash. The URLs it
        # was reached by are sources, linked by edges, so the same bytes met
        # through two URLs are one document with two derived_from edges
        # rather than a conflicting immutable write.
        if c.final_url:
            doc_body: dict[str, Any] = {"raw_sha256": c.document_hash}
        else:
            doc_body = {"legacy_text_hash": c.document_hash}
        doc_id = "doc-" + _h(c.document_hash)
        _ensure(store, "document", doc_id, doc_body, by=author, project_id=pid)
        store.link(("document", doc_id), "derived_from", ("source", src_id), created_by=author)
        if c.final_url and c.final_url != c.url:
            final_src = "src-" + _h(c.final_url)
            _ensure(store, "source", final_src, {"url": c.final_url}, by=author, project_id=pid)
            store.link(("document", doc_id), "derived_from", ("source", final_src), created_by=author)

        pas_id = "pas-" + _h(doc_id, c.passage, c.location)
        _ensure(store, "passage", pas_id, {"text": c.passage, "location": c.location}, by=author, project_id=pid)
        store.link(("passage", pas_id), "part_of", ("document", doc_id), created_by=author)

        fp = claim_fingerprint(c)
        clm_id = "clm-" + _h(fp)
        claim_ids[fp] = clm_id
        ev_id = "ev-" + _h(pas_id, clm_id)
        _ensure(store, "evidence", ev_id, {"stance": "supports"}, by=author, project_id=pid)
        store.link(("evidence", ev_id), "extracted_from", ("passage", pas_id), created_by=author)

        body: dict[str, Any] = {
            "text": c.claim, "status": c.status, "agent": c.agent,
            "retrieved_at": c.retrieved_at, "location": c.location, "legacy_source_id": c.source_id,
        }
        if c.sub_question:
            body["sub_question"] = c.sub_question
        _ensure(store, "claim", clm_id, body, by=author, project_id=pid)
        store.link(("claim", clm_id), "supported_by", ("evidence", ev_id), created_by=author)
        target = sub_ids.get(c.sub_question) if c.sub_question else None
        if c.sub_question and target is None:
            target = "q-" + _h(record.question, c.sub_question)
            _ensure(store, "research_question", target, {"text": c.sub_question}, by=by, project_id=pid)
            store.link(("research_question", ids["question"]), "decomposes_into", ("research_question", target), created_by=by)
        store.link(("claim", clm_id), "answers", ("research_question", target or ids["question"]), created_by=author)
        if c.task_id:
            claim_tasks.append((c.task_id, clm_id))

    con_ids: dict[str, str] = {}
    for k in record.contradictions:
        a, b = claim_ids.get(k.claim_a_id), claim_ids.get(k.claim_b_id)
        con_id = "con-" + _h(k.claim_a_id, k.claim_b_id, k.sub_question)
        con_ids[contradiction_key(k)] = con_id
        body = {"note": k.note or "(no note recorded)"}
        if k.sub_question:
            body["sub_question"] = k.sub_question
        _ensure(store, "contradiction", con_id, body, by=by, project_id=pid)
        if a and b:
            store.link(("contradiction", con_id), "about", ("claim", a), created_by=by)
            store.link(("contradiction", con_id), "about", ("claim", b), created_by=by)
            store.link(("claim", a), "contradicted_by", ("claim", b), created_by=by)

    # R3 (finding #096): follow-up tasks, the backward edge made visible.
    task_ids: dict[str, str] = {}
    for t in record.tasks:
        tid = "task-" + _h(record.question, t.task_id)
        task_ids[t.task_id] = tid
        _ensure(store, "task", tid, {"title": t.title, "stage": t.stage, "status": t.status}, by=by, project_id=pid)
        if t.sub_question:
            sq = sub_ids.get(t.sub_question) or "q-" + _h(record.question, t.sub_question)
            try:
                store.link(("task", tid), "about", ("research_question", sq), created_by=by)
            except KeyError:
                store.link(("task", tid), "about", ("research_question", ids["question"]), created_by=by)
        if t.spawned_by and t.spawned_by in con_ids:
            store.link(("contradiction", con_ids[t.spawned_by]), "spawned", ("task", tid), created_by=by)
    for task_id, clm_id in claim_tasks:
        if task_id in task_ids:
            store.link(("task", task_ids[task_id]), "produced", ("claim", clm_id), created_by=by)

    # Every synthesis the record has had: a revised answer never erases the
    # earlier one, and each stays linked to the question it answered.
    for text in dict.fromkeys(record.synthesis_history or ((record.synthesis,) if record.synthesis else ())):
        if text.strip():
            res_id = "res-" + _h(record.question, text)
            _ensure(store, "result", res_id, {"summary": text}, by=by, project_id=pid)
            store.link(("result", res_id), "answers", ("research_question", ids["question"]), created_by=by)
    return ids


def migrate_legacy(db_path: str | Path, store: GraphStore | None = None) -> dict[str, int]:
    """Copy every ``research_records`` row into the graph in the same file.
    Reads only; the legacy table is left exactly as it was."""
    from dourmouse.research_pipeline.store import _record_from_dict

    path = Path(db_path)
    store = store or GraphStore(path)
    conn = sqlite3.connect(path)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_records'"
        ).fetchone()
        rows = conn.execute("SELECT body FROM research_records").fetchall() if exists else []
    finally:
        conn.close()
    migrated = 0
    for (body,) in rows:
        sync_record(store, _record_from_dict(json.loads(body)))
        migrated += 1
    return {"records": migrated}
