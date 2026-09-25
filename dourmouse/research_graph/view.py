"""What the research view shows (R8, finding #129).

Read-only summaries over the research graph, shaped for the console's
RESEARCH screen:

- ``overview``: how many of each object exist, the top-level questions
  (each with its sub-questions, claims, sources and open contradictions),
  and the hypotheses with the experiments testing them and their runs.
- ``question_detail``: one question in full: every claim with the passage
  and source behind it, and each contradiction between claims.
"""

from __future__ import annotations

from typing import Any

from .model import OBJECT_TYPES
from .store import GraphStore


def _dst(store: GraphStore, src: tuple[str, str], relation: str) -> list[Any]:
    return store.related(src, relation)


def _src_ids(store: GraphStore, dst: tuple[str, str], relation: str, src_type: str) -> list[str]:
    return [e.src_id for e in store.edges(dst=dst, relation=relation) if e.src_type == src_type]


def overview(store: GraphStore, limit: int = 20) -> dict[str, Any]:
    counts = {t: store.count(t) for t in OBJECT_TYPES}
    questions = []
    for proj in list(reversed(store.find("project")))[:limit]:
        for qid in _src_ids(store, ("project", proj.id), "part_of", "research_question"):
            q = store.get("research_question", qid)
            subs = _dst(store, q.ref, "decomposes_into")
            all_q = [q.id] + [s.id for s in subs]
            claims = {c for qq in all_q for c in _src_ids(store, ("research_question", qq), "answers", "claim")}
            contradictions = set()
            for c in claims:
                contradictions |= set(_src_ids(store, ("claim", c), "about", "contradiction"))
            open_con = [c for c in contradictions
                        if store.get("contradiction", c).body.get("status", "open") not in ("resolved", "settled")]
            questions.append({"id": q.id, "text": q.body["text"], "sub_questions": len(subs), "claims": len(claims),
                              "contradictions": len(contradictions), "open_contradictions": len(open_con),
                              "created_at": q.created_at})
    hypotheses = []
    for h in list(reversed(store.find("hypothesis")))[:limit]:
        hypotheses.append({"id": h.id, "statement": h.body["statement"], "status": h.body.get("status", ""),
                           "experiments": [_experiment(store, e) for e in _dst(store, h.ref, "tested_by")]})
    # Every recent experiment too: one run without a hypothesis is still
    # research that happened.
    experiments = [_experiment(store, e) for e in list(reversed(store.find("experiment")))[:limit]]
    return {"counts": counts, "questions": questions, "hypotheses": hypotheses, "experiments": experiments}


def _experiment(store: GraphStore, exp: Any) -> dict[str, Any]:
    runs = []
    for rid in _src_ids(store, exp.ref, "run_of", "experiment_run"):
        run = store.get("experiment_run", rid)
        metrics = {m.body["name"]: m.body["value"] for m in _dst(store, run.ref, "produced") if m.type == "metric"}
        runs.append({"id": rid, "status": run.body["status"], "metrics": metrics,
                     "replicates": [r.id for r in _dst(store, run.ref, "replicates")]})
    return {"id": exp.id, "protocol": exp.body["protocol"], "status": exp.body.get("status", ""), "runs": runs}


def question_detail(store: GraphStore, question_id: str) -> dict[str, Any]:
    q = store.get("research_question", question_id)
    subs = _dst(store, q.ref, "decomposes_into")
    claims = []
    for qq in [q] + subs:
        for cid in _src_ids(store, qq.ref, "answers", "claim"):
            c = store.get("claim", cid)
            passage = source = None
            for ev in _dst(store, c.ref, "supported_by"):
                for pas in _dst(store, ev.ref, "extracted_from"):
                    passage = pas.body.get("text")
                    for doc in _dst(store, pas.ref, "part_of"):
                        srcs = _dst(store, doc.ref, "derived_from")
                        if srcs:
                            source = srcs[0].body.get("url")
            claims.append({"id": c.id, "text": c.body["text"], "status": c.body.get("status", ""),
                           "sub_question": qq.body["text"] if qq.id != q.id else "", "passage": (passage or "")[:600],
                           "source": source or "", "version": c.version,
                           "contradicted_by": [x.id for x in _dst(store, c.ref, "contradicted_by")]})
    contradictions = {}
    for claim in claims:
        for con in _src_ids(store, ("claim", claim["id"]), "about", "contradiction"):
            obj = store.get("contradiction", con)
            contradictions[con] = {"id": con, "note": obj.body["note"], "status": obj.body.get("status", "open"),
                                   "spawned": [t.body.get("title") for t in _dst(store, obj.ref, "spawned")]}
    return {"id": q.id, "text": q.body["text"], "sub_questions": [s.body["text"] for s in subs], "claims": claims,
            "contradictions": list(contradictions.values())}
