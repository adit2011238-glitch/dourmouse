"""Research as a background job (PL-10 T1, and shared foundation 1).

Research used to run inside a chat turn and ran out of steps. Now a research
job is a goal in the goals engine whose tasks are native job steps
("job:research", see goal_runtime.JOB_HANDLERS), created as the job advances:

  brief          context brief; parks for the owner to check it when asked to
  papers         search the three indexes, rank, choose the papers to read
  card (x N)     read one paper and make its card (one task per paper)
  questions      question, goals, sub-questions; parks until the owner approves
  cycle (1..)    one answer round each, until the stop rule says stop
  present        the report: answers, disagreements, references, export

Everything the job learns is kept in ``JobStore`` (one JSON body per job, the
same one-row-per-record SQLite shape as ResearchStore), the texts go into the
library through ``library_port``, and the finished answers are also written as
a ResearchRecord so the RESEARCH screen's claims and evidence graph show them.

The owner's two decisions are parks, not polls: the goal shows "Needs you" with
what to do, and approving (RESEARCH screen, or the GOALS screen's APPROVE)
moves the job on. Nothing is approved automatically.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dourmouse.config import workspace_dir

from .brief import Brief, build_brief, store_brief
from .cards import PaperCard, make_card, store_card
from .citations import DEFAULT_STYLE, STYLES
from .cycle import Budget, CycleContext, CycleState, Spend, build_report, render_markdown, run_round
from .library_port import get_library
from .papers import Paper, PaperClient, merge_papers, query_terms, rank_papers
from .questions import QuestionSet, approve, derive_questions, edit
from .reading import read_paper, store_reading

KIND = "research"
AGENT = f"job:{KIND}"
MAX_TOPIC_CHARS = 300
SEARCH_QUERIES = 4
PER_SOURCE = 10

PARK_BRIEF = ("Check the context brief on the RESEARCH screen, add any corrections, then press CONTINUE "
              "(or APPROVE here) to start the paper search.")
PARK_QUESTIONS = ("Approve the research questions on the RESEARCH screen (edit them there first if needed), "
                  "or APPROVE here to use them as they are.")


def default_db() -> Path:
    return workspace_dir() / "research_pipeline" / "jobs.db"


class JobStore:
    """One row per job, the whole job state as JSON. ``update`` is a
    read-modify-write under an IMMEDIATE transaction, so the worker thread and
    an owner's edit never overwrite each other."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_db()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._lock, self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS research_jobs (id TEXT PRIMARY KEY, topic TEXT NOT NULL, "
                         "body TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)")

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)

    def create(self, job_id: str, topic: str, body: dict[str, Any]) -> None:
        now = time.time()
        with self._lock, self._conn() as conn:
            conn.execute("INSERT INTO research_jobs (id, topic, body, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                         (job_id, topic, json.dumps(body), now, now))

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT body FROM research_jobs WHERE id=?", (job_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def update(self, job_id: str, fn: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT body FROM research_jobs WHERE id=?", (job_id,)).fetchone()
                if row is None:
                    raise KeyError(f"no research job {job_id!r}")
                body = json.loads(row[0])
                fn(body)
                conn.execute("UPDATE research_jobs SET body=?, updated_at=? WHERE id=?",
                             (json.dumps(body), time.time(), job_id))
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return body

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._conn() as conn:
            rows = conn.execute("SELECT id, topic, created_at, updated_at FROM research_jobs "
                                "ORDER BY created_at DESC LIMIT ?", (int(limit),)).fetchall()
        return [{"id": r[0], "topic": r[1], "created_at": r[2], "updated_at": r[3]} for r in rows]


# ---------------------------------------------------------------- seams (tests replace these)

_jobs: JobStore | None = None


def jobs() -> JobStore:
    global _jobs
    if _jobs is None or _jobs.path != default_db():
        _jobs = JobStore()
    return _jobs


def _client() -> PaperClient:
    return PaperClient()


def _complete() -> Callable[[str], str]:
    from .hypotheses import default_complete

    return default_complete()


_read = read_paper


def _now_year() -> int:
    return datetime.now(timezone.utc).year


def _goal_store() -> Any:
    from dourmouse.goals import get_goal_store

    return get_goal_store()


# ---------------------------------------------------------------- starting


def start_job(topic: str, *, style: str = DEFAULT_STYLE, pause_after_brief: bool = True,
              budget: dict[str, Any] | None = None, store: Any = None) -> str:
    """Create the goal and its first task; the goals worker does the rest."""
    topic = " ".join(str(topic or "").split())
    if not topic:
        raise ValueError("a research topic is needed")
    if len(topic) > MAX_TOPIC_CHARS:
        raise ValueError(f"the topic is longer than {MAX_TOPIC_CHARS} characters")
    style = (style or DEFAULT_STYLE).lower()
    if style not in STYLES:
        raise ValueError(f"citation style must be one of {', '.join(STYLES)}")
    b = Budget.from_dict(budget)
    store = store or _goal_store()
    goal = store.create_goal(f"Research: {topic}", resource_limits={"kind": KIND, "budget": b.to_dict()})
    body = {"topic": topic, "style": style, "pause_after_brief": bool(pause_after_brief), "budget": b.to_dict(),
            "spend": {"model_calls": 0, "papers_read": 0, "started_at": time.time()}, "tasks": {}, "brief": None,
            "papers": {}, "ranked": [], "chosen": [], "cards": {}, "questions": None, "cycle": None,
            "report": None, "markdown": "", "log": [], "service_requests": {}}
    jobs().create(goal["id"], topic, body)
    _add_task(store, goal["id"], "brief", "Context brief: an encyclopedia article and the two most cited reviews")
    store.update_goal_status(goal["id"], "EXECUTING")
    return str(goal["id"])


def _add_task(store: Any, job_id: str, stage: str, description: str, *, depends_on: list[str] | None = None,
              **extra: Any) -> str:
    task = store.create_task(job_id, description, depends_on=depends_on, assigned_agent=AGENT, max_attempts=2)

    def note(body: dict[str, Any]) -> None:
        body["tasks"][task["id"]] = {"stage": stage, **extra}

    jobs().update(job_id, note)
    return str(task["id"])


def _log(job_id: str, line: str) -> None:
    jobs().update(job_id, lambda b: b["log"].append(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {line}"))


def _spend(body: dict[str, Any]) -> Spend:
    s = body.get("spend") or {}
    return Spend(int(s.get("model_calls") or 0), int(s.get("papers_read") or 0), float(s.get("started_at") or 0.0))


def _counting(complete: Callable[[str], str], counter: list[int]) -> Callable[[str], str]:
    def wrapped(prompt: str) -> str:
        counter[0] += 1
        return complete(prompt)
    return wrapped


# ---------------------------------------------------------------- the handler


def handle(store: Any, goal: dict[str, Any], task: dict[str, Any], approved: bool) -> dict[str, Any]:
    job_id = goal["id"]
    body = jobs().get(job_id)
    if body is None:
        return {"verified": False, "final_text": f"no research job {job_id!r} in the job store"}
    meta = body["tasks"].get(task["id"])
    if meta is None:
        return {"verified": False, "final_text": "this task is not one of the research job's own steps"}
    stage = meta["stage"]
    steps: dict[str, Callable[..., dict[str, Any]]] = {
        "brief": _step_brief, "papers": _step_papers, "card": _step_card, "questions": _step_questions,
        "cycle": _step_cycle, "present": _step_present,
    }
    if stage not in steps:
        return {"verified": False, "final_text": f"unknown research step {stage!r}"}
    return steps[stage](store, job_id, task, meta, approved)


def _step_brief(store: Any, job_id: str, task: dict[str, Any], meta: dict[str, Any], approved: bool) -> dict[str, Any]:
    body = jobs().get(job_id) or {}
    if body.get("brief") is None:
        client = _client()
        brief, readings, reviews = build_brief(body["topic"], client, _complete(), read=_read)
        store_brief(brief, get_library(), job_id=job_id, readings=readings)
        for key, paper in zip(("R1", "R2"), reviews, strict=False):
            if key in readings:
                store_reading(readings[key], paper, get_library(), job_id=job_id)

        def save(b: dict[str, Any]) -> None:
            b["brief"] = brief.to_dict()
            b["spend"]["model_calls"] = int(b["spend"].get("model_calls") or 0) + brief.model_calls
            for p in reviews:
                b["papers"][p.key] = p.to_dict()
            b["review_keys"] = [p.key for p in reviews]
            for svc, n in client.requests_sent.items():
                b["service_requests"][svc] = int(b["service_requests"].get(svc) or 0) + n

        body = jobs().update(job_id, save)
        _log(job_id, f"context brief: {len(brief.items)} items kept, {len(brief.rejected)} dropped (quote not in "
                     f"its source); gaps: {'; '.join(brief.gaps) or 'none'}")
    brief = Brief.from_dict(body["brief"])
    if body.get("pause_after_brief") and not approved:
        return {"park": PARK_BRIEF}
    _add_task(store, job_id, "papers", "Find and rank papers (OpenAlex, Semantic Scholar, arXiv)")
    return {"verified": True, "final_text": f"Context brief with {len(brief.items)} quoted items "
                                            f"({len(brief.rejected)} dropped); sources: {', '.join(brief.sources) or 'none'}",
            "evidence": "every brief item's quote was found in its named source"}


def choose_papers(topic: str, brief: Brief, client: PaperClient, *, initial_papers: int, now_year: int,
                  known: list[Paper] | None = None) -> dict[str, Any]:
    """Search every query the brief proposed in all three indexes, merge, rank
    and choose the papers to read. Shared by the job and the benchmark."""
    queries = (brief.search_terms or [topic])[:SEARCH_QUERIES]
    found: list[Paper] = list(known or [])
    notes: list[str] = []
    answered = 0
    for q in queries:
        r = client.search(q, per_source=PER_SOURCE)
        answered += len(r.counts)
        found.extend(r.papers)
        notes.append(f"\"{q}\": {r.note()}")
    papers = merge_papers(found)
    terms = query_terms(topic, *(i.label for i in brief.items if i.kind == "term"))
    ranked = rank_papers(papers, terms, now_year=now_year)
    chosen = [p for p, s in ranked if s.relevance > 0][:initial_papers]
    return {"papers": papers, "ranked": ranked, "chosen": chosen, "notes": notes, "answered": answered}


def _step_papers(store: Any, job_id: str, task: dict[str, Any], meta: dict[str, Any], approved: bool) -> dict[str, Any]:
    body = jobs().get(job_id) or {}
    brief = Brief.from_dict(body["brief"]) if body.get("brief") else Brief(topic=body["topic"])
    client = _client()
    known = [Paper.from_dict(body["papers"][k]) for k in body.get("review_keys") or [] if k in body["papers"]]
    budget = Budget.from_dict(body.get("budget"))
    got = choose_papers(body["topic"], brief, client, initial_papers=budget.initial_papers, now_year=_now_year(),
                        known=known)
    notes = got["notes"]
    if got["answered"] == 0:
        return {"verified": False, "final_text": "every paper service failed for every query: " + " | ".join(notes)}
    papers, ranked, chosen = got["papers"], got["ranked"], got["chosen"]

    def save(b: dict[str, Any]) -> None:
        for p, s in ranked[:40]:
            b["papers"][p.key] = p.to_dict()
        b["ranked"] = [{"key": p.key, "score": s.to_dict()} for p, s in ranked[:40]]
        b["chosen"] = [p.key for p in chosen]
        for svc, n in client.requests_sent.items():
            b["service_requests"][svc] = int(b["service_requests"].get(svc) or 0) + n

    jobs().update(job_id, save)
    _log(job_id, "paper search: " + " | ".join(notes) + f"; {len(papers)} distinct papers, {len(chosen)} chosen")
    card_tasks = [_add_task(store, job_id, "card", f"Read and conclude: {p.title[:120]}", paper_key=p.key)
                  for p in chosen]
    _add_task(store, job_id, "questions", "Define the research question and sub-questions (needs your approval)",
              depends_on=card_tasks or None)
    return {"verified": True, "final_text": f"{len(papers)} distinct papers found, {len(chosen)} chosen to read",
            "evidence": " | ".join(notes)}


def _step_card(store: Any, job_id: str, task: dict[str, Any], meta: dict[str, Any], approved: bool) -> dict[str, Any]:
    body = jobs().get(job_id) or {}
    paper = Paper.from_dict(body["papers"][meta["paper_key"]])
    budget, spend = Budget.from_dict(body.get("budget")), _spend(body)
    if spend.model_calls >= budget.max_model_calls:
        _log(job_id, f"not carded (model call budget spent): {paper.title}")
        return {"verified": True, "final_text": f"Skipped, budget spent: {paper.title}"}
    reading = _read(paper)
    library = get_library()
    store_reading(reading, paper, library, job_id=job_id)
    counter = [0]
    card, _calls = make_card(paper, reading, body["topic"], _counting(_complete(), counter), now_year=_now_year())
    card = store_card(card, library, job_id=job_id)

    def save(b: dict[str, Any]) -> None:
        b["cards"][card.card_id] = card.to_dict()
        b["spend"]["model_calls"] = int(b["spend"].get("model_calls") or 0) + counter[0]
        b["spend"]["papers_read"] = int(b["spend"].get("papers_read") or 0) + 1

    jobs().update(job_id, save)
    _log(job_id, f"card {card.grade.letter} ({card.read_from}, {len(card.findings)} finding(s), "
                 f"{len(card.rejected)} quote(s) rejected): {paper.title}")
    return {"verified": True, "final_text": f"Card {card.grade.letter}: {paper.title} ({len(card.findings)} quoted findings, "
                                            f"read from {card.read_from})",
            "evidence": f"{card.quotes_checked} quotes checked against the paper text, {len(card.rejected)} rejected"}


def _cards(body: dict[str, Any]) -> dict[str, PaperCard]:
    return {cid: PaperCard.from_dict(c) for cid, c in (body.get("cards") or {}).items()}


def _step_questions(store: Any, job_id: str, task: dict[str, Any], meta: dict[str, Any], approved: bool) -> dict[str, Any]:
    body = jobs().get(job_id) or {}
    if body.get("questions") is None:
        brief = Brief.from_dict(body["brief"]) if body.get("brief") else None
        qs = derive_questions(body["topic"], brief, list(_cards(body).values()), _complete())

        def save(b: dict[str, Any]) -> None:
            b["questions"] = qs.to_dict()
            b["spend"]["model_calls"] = int(b["spend"].get("model_calls") or 0) + qs.model_calls

        body = jobs().update(job_id, save)
        _log(job_id, f"questions proposed: {len(qs.sub_questions)} sub-question(s)" + (f"; {qs.note}" if qs.note else ""))
    qs = QuestionSet.from_dict(body["questions"])
    if qs.status != "approved":
        if not approved:
            return {"park": PARK_QUESTIONS}
        qs = approve(qs)
        jobs().update(job_id, lambda b: b.__setitem__("questions", qs.to_dict()))
        _log(job_id, "questions approved by the owner")
    _add_task(store, job_id, "cycle", "Answer round 1", round=1)
    return {"verified": True, "final_text": f"Approved: {qs.question} ({len(qs.sub_questions)} sub-questions)"}


def _step_cycle(store: Any, job_id: str, task: dict[str, Any], meta: dict[str, Any], approved: bool) -> dict[str, Any]:
    body = jobs().get(job_id) or {}
    qs = QuestionSet.from_dict(body["questions"])
    cards = _cards(body)
    state = CycleState.from_dict(body.get("cycle"))
    spend = _spend(body)
    library = get_library()
    client = _client()
    saved_cards: dict[str, dict[str, Any]] = {}
    ctx = CycleContext(topic=body["topic"], job_id=job_id, questions=qs, cards=cards, library=library, client=client,
                       complete=_complete(), budget=Budget.from_dict(body.get("budget")), spend=spend,
                       now_year=_now_year(), read=_read, on_card=lambda c: saved_cards.__setitem__(c.card_id, c.to_dict()))
    new, stop = run_round(ctx, state)

    def save(b: dict[str, Any]) -> None:
        b["cycle"] = state.to_dict()
        b["cards"].update(saved_cards)
        for c in ctx.cards.values():
            b["papers"].setdefault(c.paper_key, c.paper.to_dict())
        b["spend"] = {"model_calls": spend.model_calls, "papers_read": spend.papers_read, "started_at": spend.started_at}
        for svc, n in client.requests_sent.items():
            b["service_requests"][svc] = int(b["service_requests"].get(svc) or 0) + n

    jobs().update(job_id, save)
    _log(job_id, f"answer round {state.round}: {new} new claim(s)" + (f"; stop: {stop}" if stop else ""))
    if stop:
        _add_task(store, job_id, "present", "Write the answers, disagreements and references")
    else:
        _add_task(store, job_id, "cycle", f"Answer round {state.round + 1}", round=state.round + 1)
    return {"verified": True, "final_text": f"Round {state.round}: {new} new claim(s)" + (f"; stopped: {stop}" if stop else ""),
            "evidence": "every kept sentence's citations were checked against the cited quotes"}


def _step_present(store: Any, job_id: str, task: dict[str, Any], meta: dict[str, Any], approved: bool) -> dict[str, Any]:
    body = jobs().get(job_id) or {}
    qs = QuestionSet.from_dict(body["questions"])
    cards = _cards(body)
    state = CycleState.from_dict(body.get("cycle"))
    report = build_report(qs, cards, state, style=body.get("style") or DEFAULT_STYLE, spend=_spend(body))
    markdown = render_markdown(report)
    get_library().add(f"report:{job_id}", markdown, {"kind": "research_report", "job_id": job_id})
    record_question = write_record(qs, cards, state, markdown)

    def save(b: dict[str, Any]) -> None:
        b["report"] = report
        b["markdown"] = markdown
        b["record_question"] = record_question

    jobs().update(job_id, save)
    claims = sum(len(s["evidence"]) for a in report["answers"] for s in a["sentences"])
    _log(job_id, f"answers written: {claims} cited claim(s), {len(report['references'])} reference(s)")
    return {"verified": True, "final_text": f"Answers for {len(report['answers'])} sub-questions, {claims} cited claims, "
                                            f"{len(report['references'])} references. Stopped: {report['stop_reason']}"}


# ---------------------------------------------------------------- the RESEARCH screen's record


def write_record(qs: QuestionSet, cards: dict[str, PaperCard], state: CycleState, markdown: str) -> str:
    """The answers as a ResearchRecord, so the screen's claims list and evidence
    graph show them. Returns the record's key (the research question)."""
    from .core import Claim, Contradiction, ResearchRecord
    from .stages import _claim_fingerprint, _source_id_for_url
    from .store import ResearchStore
    from .store import default_db as record_db

    index = {f.id: (c, f) for c in cards.values() for f in c.findings}
    rec = ResearchRecord(question=qs.question)
    rec.set_plan([s.text for s in qs.sub_questions])
    used = [(int(i), str(fid)) for i, fid, _r in state.claims if str(fid) in index]
    urls = list(dict.fromkeys(index[fid][0].paper.url or index[fid][0].paper_key for _i, fid in used))
    if not urls:
        return ""
    rec.add_sources(urls)
    claim_by_fid: dict[str, Claim] = {}
    for i, fid in used:
        if fid in claim_by_fid:
            continue  # one claim object per quoted finding, under the first sub-question it answered
        card, f = index[fid]
        pdf = card.read_from == "pdf" and card.raw_sha256
        claim = Claim(
            claim=f.text, source_id=_source_id_for_url(card.read_url or card.paper.url),
            url=card.paper.url or card.paper_key,
            document_hash=card.raw_sha256 if pdf else hashlib.sha256(f.quote.encode("utf-8")).hexdigest(),
            location=f.location, passage=f.quote, retrieved_at=card.made_at or time.time(), agent="research_job",
            sub_question=qs.sub_questions[i].text if i < len(qs.sub_questions) else "",
            final_url=card.read_url if pdf else "",
        )
        rec.add_claim(claim)
        claim_by_fid.setdefault(fid, claim)
    for d in state.disagreements:
        if d.a in claim_by_fid and d.b in claim_by_fid:
            rec.add_contradiction(Contradiction(_claim_fingerprint(claim_by_fid[d.a]), _claim_fingerprint(claim_by_fid[d.b]),
                                                qs.sub_questions[d.sub_index].text, d.note))
    rec.set_synthesis(markdown)
    ResearchStore(record_db()).save(rec, now=time.time())
    return qs.question


# ---------------------------------------------------------------- owner actions


def _parked_task(store: Any, job_id: str, stage: str) -> dict[str, Any] | None:
    body = jobs().get(job_id) or {}
    for t in store.list_tasks(job_id):
        if t["status"] == "WAITING_FOR_APPROVAL" and (body["tasks"].get(t["id"]) or {}).get("stage") == stage:
            return t
    return None


def correct_brief(job_id: str, owner_notes: str) -> dict[str, Any]:
    notes = str(owner_notes or "").strip()
    if len(notes) > 4000:
        raise ValueError("corrections are longer than 4,000 characters")

    def save(b: dict[str, Any]) -> None:
        if b.get("brief") is None:
            raise ValueError("the brief is not written yet")
        if b.get("questions") is not None:
            raise ValueError("the questions are already set; the brief can no longer change them")
        b["brief"]["owner_notes"] = notes

    return jobs().update(job_id, save)


def continue_after_brief(job_id: str, store: Any = None) -> bool:
    store = store or _goal_store()
    task = _parked_task(store, job_id, "brief")
    return bool(task) and store.resolve_task_approval(task["id"], True, "brief checked on the RESEARCH screen")


def edit_questions(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    def save(b: dict[str, Any]) -> None:
        if b.get("questions") is None:
            raise ValueError("the questions are not proposed yet")
        b["questions"] = edit(QuestionSet.from_dict(b["questions"]), payload).to_dict()

    return jobs().update(job_id, save)


def approve_questions(job_id: str, store: Any = None) -> bool:
    store = store or _goal_store()
    task = _parked_task(store, job_id, "questions")
    if task is None:
        return False

    def save(b: dict[str, Any]) -> None:
        b["questions"] = approve(QuestionSet.from_dict(b["questions"])).to_dict()

    jobs().update(job_id, save)
    _log(job_id, "questions approved by the owner")
    return bool(store.resolve_task_approval(task["id"], True, "questions approved on the RESEARCH screen"))


def job_view(job_id: str, store: Any = None) -> dict[str, Any] | None:
    """Everything the RESEARCH screen shows for one job."""
    body = jobs().get(job_id)
    if body is None:
        return None
    store = store or _goal_store()
    goal = store.get_goal(job_id) or {}
    tasks = store.list_tasks(job_id) if goal else []
    waiting = next(((body["tasks"].get(t["id"]) or {}).get("stage", "") for t in tasks
                    if t["status"] == "WAITING_FOR_APPROVAL"), "")
    brief = Brief.from_dict(body["brief"]) if body.get("brief") else None
    cards = sorted(_cards(body).values(), key=lambda c: -c.grade.score)
    return {
        "id": job_id, "topic": body["topic"], "style": body.get("style", DEFAULT_STYLE),
        "status": goal.get("status", "UNKNOWN"), "blocked_reason": goal.get("blocked_reason") or "",
        "waiting_for": waiting,
        "steps": [{"stage": (body["tasks"].get(t["id"]) or {}).get("stage", ""), "description": t["description"],
                   "status": t["status"], "error": t["last_error"] or ""} for t in tasks],
        "brief": {"markdown": brief.as_markdown(), "owner_notes": brief.owner_notes, "gaps": brief.gaps,
                  "rejected": brief.rejected} if brief else None,
        "papers_chosen": [body["papers"][k] for k in body.get("chosen") or [] if k in body["papers"]],
        "cards": [c.to_dict() for c in cards],
        "questions": body.get("questions"),
        "report": body.get("report"),
        "budget": body.get("budget"), "spend": body.get("spend"), "service_requests": body.get("service_requests"),
        "log": list(body.get("log") or [])[-60:],
    }


def export(job_id: str, fmt: str = "markdown", style: str | None = None) -> tuple[str, str, str]:
    """(filename, mime, content). A different style re-renders the stored answers."""
    body = jobs().get(job_id)
    if body is None:
        raise KeyError(job_id)
    if not body.get("report"):
        raise ValueError("this job has no answers yet")
    stem = "".join(ch if ch.isalnum() else "-" for ch in body["topic"].lower()).strip("-")[:40] or "research"
    report = body["report"]
    if style and style != report.get("style"):
        if style not in STYLES:
            raise ValueError(f"style must be one of {', '.join(STYLES)}")
        report = build_report(QuestionSet.from_dict(body["questions"]), _cards(body),
                              CycleState.from_dict(body.get("cycle")), style=style, spend=_spend(body))
    if fmt == "json":
        return f"{stem}.json", "application/json", json.dumps(report, indent=2, ensure_ascii=False)
    if fmt != "markdown":
        raise ValueError("format must be markdown or json")
    return f"{stem}.md", "text/markdown", render_markdown(report)


def _register() -> None:
    from dourmouse.goal_runtime import register_job_handler

    register_job_handler(KIND, handle)


_register()
