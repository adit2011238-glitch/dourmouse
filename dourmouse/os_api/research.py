"""Backend for the OS shell's RESEARCH screen (finding #151).

All routes are read-only over what the research pipeline already stored:

* ``GET /api/os/research/loop``: the research loop as it really is. Each of the
  fifteen stages is "built" only when the tool that does the work is registered
  in this server's registry; the backward edge is "built" only when the
  follow-up tool is registered AND ``run_backward_edge`` imports. The counts are
  read from the stored graph. Nothing is a constant that looks live.
* ``GET /api/os/research/questions``: the questions (with their pipeline stage
  and open tasks from the resumable record), the hypotheses and the counts.
* ``GET /api/os/research/question?id=``: one question in full (claims with
  passage and source URL, contradictions) plus its record's stage and plan.
* ``GET /api/os/research/graph?question=<id>`` or ``?hypothesis=<id>``: the
  neighbourhood of one object as nodes and typed edges, capped.
* ``GET /api/os/research/export?question=<id>&format=markdown|json``: the record
  and every stored claim with its passage and source URL, returned as text. The
  browser turns it into a download; the server writes no file.

A claim's ``status`` is ACTIVE or REJECTED. "Contested" means it has a
``contradicted_by`` edge. There is no stored "verified" flag: every stored claim
passed the verbatim substring check on the fetched page when it was extracted.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import ApiError, Request, route

_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
_NODE_CAP = 40
_CLAIM_CAP = 500

# (title, tools that do the work, what the built state rests on)
_STAGES: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    ("question", "Question", ("research_plan",), "research_plan opens a record for the question"),
    ("decomposition", "Decomposition", ("research_plan",), "research_plan splits it into sub-questions"),
    ("plan", "Research plan", ("research_plan",), "research_plan stores the plan"),
    ("discover", "Source discovery", ("research_discover_sources",), "research_discover_sources searches and fetches"),
    ("validate", "Source validation", (), "no tool judges a source's credibility; only the verbatim passage check exists, inside extraction"),
    ("extract", "Evidence extraction", ("research_extract_evidence",), "research_extract_evidence keeps a claim only if its quote is a verbatim substring of the page"),
    ("hypothesis", "Hypothesis gen", ("research_hypothesize",), "research_hypothesize"),
    ("critique", "Criticism", ("research_critique",), "research_critique"),
    ("design", "Experiment design", ("research_design_experiment",), "research_design_experiment"),
    ("run", "Experiment run", ("research_experiment",), "research_experiment"),
    ("stats", "Statistics", (), "describe_samples exists in code but no registered tool calls it"),
    ("replicate", "Replication", ("research_replicate",), "research_replicate"),
    ("contradict", "Contradiction", ("research_detect_contradictions",), "research_detect_contradictions"),
    ("revise", "Revision", ("research_follow_up",), "research_follow_up runs the backward edge"),
    ("synthesize", "Synthesis", ("research_synthesize",), "research_synthesize"),
)


def _tool_names(req: Request) -> set[str]:
    reg = getattr(req.server, "registry", None)
    try:
        return set(reg.tool_names) if reg is not None else set()
    except Exception:  # noqa: BLE001 - an unreadable registry is "nothing built", never a fake yes
        return set()


def _graph():  # type: ignore[no-untyped-def]
    from dourmouse.research_graph.store import GraphStore, default_db

    return GraphStore(default_db())


def _records() -> dict[str, Any]:
    """question text -> ResearchRecord, for every stored record. Empty on any failure."""
    try:
        from dourmouse.research_pipeline.store import ResearchStore, default_db

        store = ResearchStore(default_db())
        out = {}
        for text in store.list_questions()[:200]:
            rec = store.load(text)
            if rec is not None:
                out[text] = rec
        return out
    except Exception:  # noqa: BLE001
        return {}


def _check_id(value: str, what: str) -> str:
    value = (value or "").strip()
    if not _ID.match(value):
        raise ApiError(400, f"bad {what} id")
    return value


@route("GET", "/api/os/research/loop")
def loop(req: Request) -> tuple[int, dict[str, Any]]:
    tools = _tool_names(req)
    stages = []
    for key, title, needs, why in _STAGES:
        built = bool(needs) and all(t in tools for t in needs)
        stages.append({"key": key, "title": title, "built": built, "tools": list(needs), "detail": why})
    back_tools = ("research_follow_up",)
    back_fn = False
    try:
        from dourmouse.research_pipeline.stages import run_backward_edge  # noqa: F401

        back_fn = True
    except Exception:  # noqa: BLE001
        back_fn = False
    back = {"built": back_fn and all(t in tools for t in back_tools), "tools": list(back_tools), "max_follow_ups": 3}
    counts: dict[str, int] = {}
    error = ""
    try:
        store = _graph()
        for t in ("research_question", "claim", "source", "passage", "hypothesis", "experiment", "experiment_run", "contradiction", "task"):
            counts[t] = store.count(t)
    except Exception as exc:  # noqa: BLE001 - reported, not hidden
        error = f"{type(exc).__name__}: {exc}"[:200]
    return 200, {
        "ok": True, "stages": stages, "back": back, "counts": counts, "error": error,
        "built": sum(1 for s in stages if s["built"]), "total": len(stages),
        "registry_tools": len(tools),
    }


@route("GET", "/api/os/research/questions")
def questions(req: Request) -> tuple[int, dict[str, Any]]:
    from dourmouse.research_graph import view

    data = view.overview(_graph())
    recs = _records()
    for q in data["questions"]:
        rec = recs.get(q["text"])
        q["stage"] = rec.stage.name if rec is not None else ""
        q["open_tasks"] = len(rec.open_tasks()) if rec is not None else 0
    return 200, {"ok": True, **data}


def _detail(qid: str) -> dict[str, Any]:
    from dourmouse.research_graph import view

    try:
        d = view.question_detail(_graph(), qid)
    except KeyError:
        raise ApiError(404, f"no research question {qid!r}") from None
    rec = _records().get(d["text"])
    d["stage"] = rec.stage.name if rec is not None else ""
    d["open_tasks"] = len(rec.open_tasks()) if rec is not None else 0
    d["plan"] = list(rec.plan) if rec is not None else []
    d["sources"] = list(rec.sources)[:50] if rec is not None else []
    d["synthesis"] = (rec.synthesis or "")[:6000] if rec is not None else ""
    d["claims"] = d["claims"][:_CLAIM_CAP]
    for c in d["claims"]:
        c["contested"] = bool(c.get("contradicted_by"))
    return d


@route("GET", "/api/os/research/question")
def question(req: Request) -> tuple[int, dict[str, Any]]:
    return 200, {"ok": True, **_detail(_check_id(req.arg("id"), "question"))}


def _label(obj: Any) -> str:
    b = obj.body
    for k in ("text", "statement", "note", "protocol", "title", "summary", "name", "url", "stance", "status"):
        if b.get(k):
            return str(b[k])[:80]
    return obj.id


@route("GET", "/api/os/research/graph")
def graph(req: Request) -> tuple[int, dict[str, Any]]:
    """Neighbourhood of one question or hypothesis, three hops, at most 40 nodes."""
    qid, hid = req.arg("question").strip(), req.arg("hypothesis").strip()
    if bool(qid) == bool(hid):
        raise ApiError(400, "pass exactly one of question or hypothesis")
    kind, oid = ("research_question", _check_id(qid, "question")) if qid else ("hypothesis", _check_id(hid, "hypothesis"))
    store = _graph()
    try:
        root = store.get(kind, oid)
    except KeyError:
        raise ApiError(404, f"no {kind} {oid!r}") from None
    nodes: dict[tuple[str, str], dict[str, Any]] = {}
    edges: list[dict[str, str]] = []
    seen_edges: set[tuple[str, str, str, str, str]] = set()
    truncated = False

    def add_node(ref: tuple[str, str]) -> bool:
        nonlocal truncated
        if ref in nodes:
            return True
        if len(nodes) >= _NODE_CAP:
            truncated = True
            return False
        try:
            obj = store.get(ref[0], ref[1])
        except KeyError:
            return False
        nodes[ref] = {"id": obj.id, "type": obj.type, "label": _label(obj)}
        return True

    def add_edge(e: Any) -> None:
        key = (e.src_type, e.src_id, e.relation, e.dst_type, e.dst_id)
        if key in seen_edges:
            return
        if not (add_node((e.src_type, e.src_id)) and add_node((e.dst_type, e.dst_id))):
            return
        seen_edges.add(key)
        edges.append({"src": e.src_id, "src_type": e.src_type, "relation": e.relation, "dst": e.dst_id, "dst_type": e.dst_type})

    add_node(root.ref)
    frontier = [root.ref]
    skip = {"part_of", "derived_from", "extracted_from", "cites", "sent"}
    if kind == "research_question":
        skip.add("supported_by")  # evidence rows are drill-down; claims carry the passage
    for _hop in range(3):
        nxt: list[tuple[str, str]] = []
        for ref in frontier:
            for e in store.edges(src=ref)[:60] + store.edges(dst=ref)[:60]:
                if e.relation in skip:
                    continue
                add_edge(e)
                for r in ((e.src_type, e.src_id), (e.dst_type, e.dst_id)):
                    if r != ref and r in nodes and r not in frontier and r not in nxt:
                        nxt.append(r)
        frontier = nxt
    return 200, {"ok": True, "root": {"id": root.id, "type": root.type}, "nodes": list(nodes.values()), "edges": edges,
                 "truncated": truncated, "cap": _NODE_CAP}


def _markdown(d: dict[str, Any]) -> str:
    lines = [f"# {d['text']}", ""]
    if d.get("stage"):
        lines += [f"Pipeline stage: {d['stage']}", ""]
    if d.get("sub_questions"):
        lines += ["## Sub-questions", ""] + [f"- {s}" for s in d["sub_questions"]] + [""]
    if d.get("synthesis"):
        lines += ["## Synthesis", "", d["synthesis"], ""]
    lines += [f"## Claims ({len(d['claims'])})", ""]
    for c in d["claims"]:
        tag = "contested" if c.get("contested") else str(c.get("status") or "").lower()
        lines.append(f"- {c['text']} [{tag}]")
        if c.get("passage"):
            lines.append("  > " + " ".join(c["passage"].split()))
        if c.get("source"):
            lines.append(f"  Source: {c['source']}")
    if d.get("contradictions"):
        lines += ["", f"## Contradictions ({len(d['contradictions'])})", ""]
        lines += [f"- {c['note']} [{c['status']}]" for c in d["contradictions"]]
    return "\n".join(lines) + "\n"


@route("GET", "/api/os/research/export")
def export(req: Request) -> tuple[int, dict[str, Any]]:
    d = _detail(_check_id(req.arg("question"), "question"))
    fmt = (req.arg("format", "markdown") or "markdown").lower()
    if fmt not in ("markdown", "json"):
        raise ApiError(400, "format must be markdown or json")
    stem = re.sub(r"[^A-Za-z0-9]+", "-", d["text"]).strip("-")[:40].lower() or "research"
    if fmt == "json":
        return 200, {"ok": True, "filename": f"{stem}.json", "mime": "application/json", "content": json.dumps(d, indent=2, ensure_ascii=False)}
    return 200, {"ok": True, "filename": f"{stem}.md", "mime": "text/markdown", "content": _markdown(d)}
