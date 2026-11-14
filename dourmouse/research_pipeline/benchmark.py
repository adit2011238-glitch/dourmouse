"""The research benchmark (PL-10 T1, step 8 of the build).

Ten fixed questions (benchmark_questions.json), each with a reference list of
known good sources whose DOIs and arXiv ids were checked against Crossref and
arXiv. One item runs the same building blocks a research job uses (brief,
``research_job.choose_papers``, cards, answer rounds until the stop rule, the
report) with the item's fixed question set in place of the owner's approval.

Measured per item, never estimated:

* citation precision: of every claim in the answers (one cited quote backing
  one sentence), the share whose quote is found again, verbatim, in the stored
  source: the cached raw PDF bytes re-read by their URL for a paper read in
  full, the index's abstract for one read from its abstract. Target 95 percent.
* source recall: the share of the reference list the paper search FOUND (in
  its ranked candidates) and the share it READ (made a card for). Target 70
  percent (found).
* time, model calls, requests sent per service, papers read, abstract-only.

The owner's grade of each answer is a field left empty for the owner.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .brief import build_brief
from .cards import PaperCard
from .cycle import Budget, CycleContext, CycleState, Spend, build_report, read_and_card, render_markdown, run_round
from .papers import PaperClient, normalize_arxiv_id, normalize_doi
from .questions import QuestionSet, SubQuestion
from .reading import PaperText, quote_in_text, read_paper, split_pages

QUESTIONS_FILE = Path(__file__).with_name("benchmark_questions.json")
PRECISION_TARGET = 0.95
RECALL_TARGET = 0.70


def load_items(path: Path = QUESTIONS_FILE) -> list[dict[str, Any]]:
    return list(json.loads(path.read_text(encoding="utf-8"))["items"])


def _ref_ids(ref: dict[str, Any]) -> set[str]:
    out = set()
    if ref.get("doi"):
        out.add("doi:" + normalize_doi(ref["doi"]))
    if ref.get("arxiv"):
        out.add("arxiv:" + normalize_arxiv_id(ref["arxiv"]))
    return out


def _paper_ids(p: Any) -> set[str]:
    out = set()
    if p.doi:
        out.add("doi:" + p.doi)
    for d in p.other_dois:
        out.add("doi:" + d)
    if p.arxiv_id:
        out.add("arxiv:" + p.arxiv_id)
    return out


def source_recall(references: list[dict[str, Any]], found: list[Any], read: list[Any]) -> dict[str, Any]:
    found_ids = set().union(*(_paper_ids(p) for p in found)) if found else set()
    read_ids = set().union(*(_paper_ids(p) for p in read)) if read else set()
    hit_found = [r for r in references if _ref_ids(r) & found_ids]
    hit_read = [r for r in references if _ref_ids(r) & read_ids]
    n = len(references) or 1
    return {"references": len(references), "found": len(hit_found), "read": len(hit_read),
            "found_share": len(hit_found) / n, "read_share": len(hit_read) / n,
            "missed": [r.get("title") for r in references if not (_ref_ids(r) & found_ids)]}


def source_text(card: PaperCard, load_cached: Callable[[str], Any] | None = None) -> str:
    """The text a claim's quote must be found in again: the cached raw PDF
    re-read by its URL, or the abstract the index gave."""
    if card.read_from == "pdf" and card.read_url:
        if load_cached is None:
            from .acquire import load_cached as load_cached_default

            load_cached = load_cached_default
        doc = load_cached(card.read_url)
        if doc is None:
            return ""
        if card.raw_sha256 and doc.raw_sha256 != card.raw_sha256:
            return ""  # the cache holds different bytes now: not the source the claim was made from
        return split_pages(doc.text)[0]
    if card.read_from == "abstract":
        return card.paper.abstract
    return ""


def citation_precision(report: dict[str, Any], cards: dict[str, PaperCard],
                       load_cached: Callable[[str], Any] | None = None) -> dict[str, Any]:
    by_key = {c.paper_key: c for c in cards.values()}
    total = verified = 0
    failures: list[str] = []
    cache: dict[str, str] = {}
    for a in report["answers"]:
        for s in a["sentences"]:
            for e in s["evidence"]:
                total += 1
                card = by_key.get(e["paper_key"])
                if card is None:
                    failures.append(f"no card for {e['paper_key']}")
                    continue
                if card.card_id not in cache:
                    cache[card.card_id] = source_text(card, load_cached)
                if quote_in_text(e["quote"], cache[card.card_id]):
                    verified += 1
                else:
                    failures.append(f"{e['paper_key']}: \"{e['quote'][:80]}\"")
    return {"claims": total, "verified": verified, "precision": (verified / total) if total else None,
            "failures": failures[:20]}


def run_item(item: dict[str, Any], *, client: PaperClient, complete: Callable[[str], str], library: Any,
             now_year: int, budget: Budget | None = None, read: Callable[[Any], PaperText] = read_paper,
             clock: Callable[[], float] = time.time, load_cached: Callable[[str], Any] | None = None) -> dict[str, Any]:
    from .research_job import choose_papers

    started = clock()
    budget = budget or Budget()
    calls = [0]

    def counted(prompt: str) -> str:
        calls[0] += 1
        return complete(prompt)

    job_id = f"bench-{item['id']}"
    brief, _readings, reviews = build_brief(item["topic"], client, counted, read=read)
    got = choose_papers(item["topic"], brief, client, initial_papers=budget.initial_papers, now_year=now_year,
                        known=reviews)
    questions = QuestionSet(question=item["question"], status="approved",
                            note="fixed benchmark questions (no owner approval needed)",
                            sub_questions=[SubQuestion(t, "benchmark") for t in item["sub_questions"]])
    spend = Spend(started_at=clock())
    ctx = CycleContext(topic=item["topic"], job_id=job_id, questions=questions, cards={}, library=library,
                       client=client, complete=counted, budget=budget, spend=spend, now_year=now_year, read=read,
                       clock=clock)
    for paper in got["chosen"]:
        if ctx.exhausted():
            break
        read_and_card(ctx, paper)
    state = CycleState()
    while not state.stop_reason:
        run_round(ctx, state)
    report = build_report(questions, ctx.cards, state, spend=spend)
    precision = citation_precision(report, ctx.cards, load_cached)
    recall = source_recall(item["references"], got["papers"], [c.paper for c in ctx.cards.values()])
    requests = dict(getattr(client, "requests_sent", {}) or {})
    return {
        "id": item["id"], "question": item["question"], "precision": precision, "recall": recall,
        "seconds": round(clock() - started, 1), "model_calls": calls[0], "requests": requests,
        "papers_read": len(ctx.cards), "abstract_only": sum(1 for c in ctx.cards.values() if c.read_from == "abstract"),
        "stop_reason": state.stop_reason, "search_notes": got["notes"], "brief_gaps": brief.gaps,
        "owner_grade": None, "report": report, "markdown": render_markdown(report),
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    claims = sum(r["precision"]["claims"] for r in results)
    verified = sum(r["precision"]["verified"] for r in results)
    refs = sum(r["recall"]["references"] for r in results)
    found = sum(r["recall"]["found"] for r in results)
    read = sum(r["recall"]["read"] for r in results)
    return {
        "items": len(results), "claims": claims, "verified": verified,
        "citation_precision": verified / claims if claims else None,
        "source_recall_found": found / refs if refs else None, "source_recall_read": read / refs if refs else None,
        "precision_target": PRECISION_TARGET, "recall_target": RECALL_TARGET,
        "seconds": sum(r["seconds"] for r in results), "model_calls": sum(r["model_calls"] for r in results),
    }


def summary_markdown(results: list[dict[str, Any]]) -> str:
    s = summarize(results)

    def pct(x: float | None) -> str:
        return "n/a" if x is None else f"{100 * x:.0f}%"

    lines = ["| item | claims | precision | found | read | papers (abstract only) | model calls | seconds | stop |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['id']} | {r['precision']['claims']} | {pct(r['precision']['precision'])} | "
                     f"{r['recall']['found']}/{r['recall']['references']} | {r['recall']['read']}/{r['recall']['references']} | "
                     f"{r['papers_read']} ({r['abstract_only']}) | {r['model_calls']} | {r['seconds']} | {r['stop_reason']} |")
    lines += ["", f"Overall: citation precision {pct(s['citation_precision'])} of {s['claims']} claims "
                  f"(target {pct(PRECISION_TARGET)}); source recall {pct(s['source_recall_found'])} found, "
                  f"{pct(s['source_recall_read'])} read (target {pct(RECALL_TARGET)} found); "
                  f"{s['model_calls']} model calls; {s['seconds']:.0f} s.",
              "Owner grades: not given yet."]
    return "\n".join(lines) + "\n"
