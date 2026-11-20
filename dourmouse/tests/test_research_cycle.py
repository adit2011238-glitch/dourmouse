"""PL-10 T1 steps 6 and 7: the answer cycle, its stop rule, claim-level citation
checks, disagreements, numbers through describe_samples, and the report."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from dourmouse.research_pipeline import cycle as Y
from dourmouse.research_pipeline.cards import Finding, PaperCard, finding_id, grade_paper, store_card
from dourmouse.research_pipeline.library_port import InterimLibrary
from dourmouse.research_pipeline.papers import Paper, PaperSourceError, SearchResult
from dourmouse.research_pipeline.questions import QuestionSet, SubQuestion
from dourmouse.research_pipeline.reading import PaperText, Section


def _card(key_doi, title, authors, year, findings, venue_type="journal", cited=200):
    p = Paper(title=title, authors=authors, year=year, doi=key_doi, venue="Psych Sci", venue_type=venue_type,
              cited_by=cited)
    fs = tuple(Finding(finding_id(p.key, q), t, q, "Results, PDF page 2") for t, q in findings)
    g = grade_paper(p, read_from="pdf", findings=len(fs), now_year=2026)
    return PaperCard(f"card-{key_doi[-1]}", p, "pdf", "", "", "", "", fs, (), "", g, len(fs))


A = _card("10.1/a", "Spacing effects in learning", ("Nicholas J. Cepeda", "Ed Vul", "Hal Pashler"), 2008, [
    ("The optimal gap increased with the retention interval.", "The optimal gap increased as the test delay increased"),
    ("Final recall was 64% with the best gap.", "final recall was 64% with the best gap"),
])
B = _card("10.1/b", "Spaced practice in classrooms", ("Doug Rohrer",), 2015, [
    ("Spaced practice raised delayed classroom test scores.", "spaced practice improved scores on a delayed classroom test"),
    ("Massed practice gave final recall of 37%.", "massed practice produced final recall of 37%"),
])
C = _card("10.1/c", "Spacing in later life", ("Ann Older",), 2021, [
    ("Spacing did not improve recall for older adults.", "spacing did not improve recall for older adults"),
], venue_type="repository", cited=2)


def _refs(prompt: str) -> dict[str, str]:
    """quote -> E ref, read back from the evidence block the cycle wrote."""
    return {m.group(2): m.group(1) for m in re.finditer(r"\[(E\d+)\][^\n]*\n\s+Quote: \"([^\"]+)\"", prompt)}


class Model:
    """Answers the three prompt kinds the cycle sends."""

    def __init__(self, answers=None, contradiction="CONTRADICTION: no\nNOTE: compatible", card=None):
        self.answers = answers or {}
        self.contradiction = contradiction
        self.card = card
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if "CONTRADICTION: yes or no" in prompt:
            return self.contradiction
        if "making a short, factual card" in prompt or "make a short, factual card" in prompt:
            return json.dumps(self.card or {})
        found = re.search(r"SUB-QUESTION: (.*)", prompt)
        assert found is not None
        sub = found.group(1)
        refs = _refs(prompt)
        spec = self.answers.get(sub, {"sentences": []})
        out: dict[str, list[Any]] = {"sentences": [], "numbers": []}
        for text, quotes in spec.get("sentences", []):
            out["sentences"].append({"text": text, "cites": [refs[q] for q in quotes if q in refs]})
        for qty, unit, written in spec.get("numbers", []):
            out["numbers"].append({"quantity": qty, "unit": unit, "values": [
                {"evidence": refs[q], "as_written": w} for q, w in written if q in refs]})
        return json.dumps(out)


class Client:
    def __init__(self, neighbours=None, fail=False):
        self.neighbours = neighbours or []
        self.fail = fail
        self.calls: list[str] = []

    def s2_neighbours(self, paper, direction="references", limit=20):
        self.calls.append(f"{direction}:{paper.key}")
        if self.fail:
            raise PaperSourceError("semantic_scholar", "rate limited (HTTP 429)", 429)
        return self.neighbours if direction == "references" else []

    def search(self, query, per_source=8):
        self.calls.append(f"search:{query}")
        return SearchResult(papers=[], counts={"openalex": 0})


S1 = "Does the optimal gap depend on the test delay?"
S2 = "Does spaced practice improve delayed classroom test scores?"
S3 = "Does spacing improve recall for older adults?"
S4 = "What final recall did each practice schedule produce?"


def _ctx(tmp_path, model, *, subs=(S1, S2), cards=(A, B, C), client=None, budget=None, read=None):
    lib = InterimLibrary(tmp_path / "lib.db")
    stored = {c.card_id: store_card(c, lib, job_id="job1") for c in cards}
    qs = QuestionSet(question="How should students space practice?", status="approved",
                     sub_questions=[SubQuestion(s, cards=("card-a",) if s == S1 else ()) for s in subs])
    now = [1000.0]
    ctx = Y.CycleContext(topic="spacing effect", job_id="job1", questions=qs, cards=stored, library=lib,
                         client=client or Client(), complete=model, budget=budget or Y.Budget(),
                         spend=Y.Spend(), now_year=2026, read=read or (lambda p: None), clock=lambda: now[0])
    return ctx, now


ANSWERS = {
    S1: {"sentences": [
        ("The optimal gap increased as the test delay increased, and final recall was 64% with the best gap.",
         ["The optimal gap increased as the test delay increased", "final recall was 64% with the best gap"]),
        ("Final recall was 99% with the best gap.", ["final recall was 64% with the best gap"]),
        ("Sleep matters a great deal for memory.", ["The optimal gap increased as the test delay increased"]),
    ]},
    S2: {"sentences": [
        ("Spaced practice improved scores on a delayed classroom test.",
         ["spaced practice improved scores on a delayed classroom test"]),
        ("The evidence is mixed for older adults.", []),
    ]},
}


def test_round_one_answers_checks_every_citation_and_records_claims(tmp_path):
    model = Model(ANSWERS)
    ctx, _ = _ctx(tmp_path, model)
    state = Y.CycleState()
    new, stop = Y.run_round(ctx, state)
    a1, a2 = state.answers[0], state.answers[1]
    assert [s.text[:20] for s in a1.sentences] == ["The optimal gap incr"]
    assert len(a1.sentences[0].cites) == 2
    assert any("99" in d and "not in the cited quotes" in d for d in a1.dropped)
    assert any("Sleep matters" in d and "no cited quote backs it" in d for d in a1.dropped)
    assert a2.sentences[1].cites == []  # a hedge is kept and is not a claim
    assert new == 3 and len(state.claims) == 3 and stop == ""
    assert a1.confidence == "medium" and a1.confidence_reason == "one paper, graded A"
    assert a2.confidence == "medium"  # Rohrer 2015 is graded A too


def test_numbers_go_through_describe_samples_only_when_found_as_written(tmp_path):
    answers = {S4: {"sentences": [("Final recall was 64% with the best gap and 37% after massed practice.",
                                   ["final recall was 64% with the best gap", "massed practice produced final recall of 37%"])],
                    "numbers": [("final recall", "percent", [("final recall was 64% with the best gap", "64%"),
                                                             ("massed practice produced final recall of 37%", "37%"),
                                                             ("massed practice produced final recall of 37%", "37%"),
                                                             ("final recall was 64% with the best gap", "12")])]}}
    ctx, _ = _ctx(tmp_path, Model(answers), subs=(S4,))
    state = Y.CycleState()
    Y.run_round(ctx, state)
    nums = state.answers[0].numbers
    assert len(nums) == 1
    st = nums[0]["statistics"]
    assert [v["as_written"] for v in nums[0]["values"]] == ["64%", "37%"]
    assert st["n"] == 2 and st["mean"] == pytest.approx(50.5)
    assert "not a meta-analysis" in st["method"] and "p_two_sided" not in st


def test_a_second_round_with_nothing_new_converges_without_model_calls(tmp_path):
    model = Model(ANSWERS)
    ctx, _ = _ctx(tmp_path, model)
    state = Y.CycleState()
    Y.run_round(ctx, state)
    before = len(model.prompts)
    new, stop = Y.run_round(ctx, state)
    assert new == 0 and stop == "converged: round 2 added no new claim"
    assert len(model.prompts) == before


def test_disagreement_is_recorded_and_lowers_confidence_and_grades(tmp_path):
    answers = {S2: {"sentences": [
        ("Spaced practice improved scores on a delayed classroom test.", ["spaced practice improved scores on a delayed classroom test"]),
        ("Spacing did not improve recall for older adults.", ["spacing did not improve recall for older adults"]),
    ]}}
    model = Model(answers, contradiction="CONTRADICTION: yes\nNOTE: classroom gains versus no gain in older adults")
    ctx, _ = _ctx(tmp_path, model, subs=(S2,))
    state = Y.CycleState()
    Y.run_round(ctx, state)
    assert len(state.disagreements) == 1
    d = state.disagreements[0]
    assert d.note.startswith("classroom gains")
    assert state.answers[0].confidence == "low"
    assert any("disputed by 1" in r for r in ctx.cards["card-c"].grade.reasons)


def test_thin_evidence_follows_the_citation_graph_and_reads_new_papers(tmp_path):
    new_paper = Paper(title="Spacing improves recall for older adults in memory training", authors=("Bo Elder",),
                      year=2019, doi="10.1/d", venue="Aging", venue_type="journal", cited_by=40,
                      abstract="spacing improved recall for older adults in a memory training study of 80 people")
    text = new_paper.abstract

    def read(p):
        return PaperText(p.key, "abstract", p.url, text=text, sections=(Section("Abstract", 0, len(text)),))

    card_reply = {"findings": [{"text": "Spacing improved recall for older adults.",
                                "quote": "spacing improved recall for older adults in a memory training study"}]}
    answers = {S3: {"sentences": [("Spacing improved recall for older adults in a memory training study.",
                                   ["spacing improved recall for older adults in a memory training study"])]}}
    client = Client(neighbours=[new_paper])
    ctx, _ = _ctx(tmp_path, Model(answers, card=card_reply), subs=(S3,), client=client, read=read)
    state = Y.CycleState()
    Y.run_round(ctx, state)
    assert "references:doi:10.1/c" in client.calls
    assert any(c.paper_key == "doi:10.1/d" for c in ctx.cards.values())
    assert ctx.spend.papers_read == 1 and "doi:10.1/d" in state.tried_papers
    assert state.answers[0].sentences and "1 new paper(s) read" in state.log[0]


def test_a_failing_citation_graph_falls_back_to_search_and_says_so(tmp_path):
    client = Client(fail=True)
    ctx, _ = _ctx(tmp_path, Model({}), subs=(S3,), client=client)
    state = Y.CycleState()
    Y.run_round(ctx, state)
    assert any(c.startswith("search:") for c in client.calls)
    assert any("citation graph: Semantic Scholar: rate limited" in line for line in state.log)


def test_budget_stops_the_cycle_and_says_which(tmp_path):
    ctx, _ = _ctx(tmp_path, Model(ANSWERS), budget=Y.Budget(max_model_calls=1))
    state = Y.CycleState()
    _, stop = Y.run_round(ctx, state)
    assert stop == "budget: 1 of 1 model calls spent"
    ctx2, now = _ctx(tmp_path / "t", Model(ANSWERS), budget=Y.Budget(max_minutes=1))
    ctx2.spend.started_at = 1.0
    now[0] = 1.0 + 120
    _, stop2 = Y.run_round(ctx2, Y.CycleState())
    assert stop2 == "budget: 2 of 1 minutes spent"
    ctx3, _ = _ctx(tmp_path / "r", Model(ANSWERS), budget=Y.Budget(max_rounds=1))
    _, stop3 = Y.run_round(ctx3, Y.CycleState())
    assert stop3 == "budget: 1 of 1 rounds used"


def test_report_and_markdown_in_apa_and_mla(tmp_path):
    ctx, _ = _ctx(tmp_path, Model(ANSWERS))
    state = Y.CycleState()
    Y.run_round(ctx, state)
    Y.run_round(ctx, state)
    report = Y.build_report(ctx.questions, ctx.cards, state, spend=ctx.spend)
    assert report["style"] == "apa" and report["stop_reason"].startswith("converged")
    first = report["answers"][0]["sentences"][0]
    assert first["citation"] == "(Cepeda et al., 2008)"
    assert first["evidence"][0]["location"] == "Results, PDF page 2"
    md = Y.render_markdown(report)
    assert "64% with the best gap (Cepeda et al., 2008)." in md
    assert "## References (APA)" in md and "Cepeda, N. J., Vul, E., & Pashler, H. (2008)." in md
    assert "## Where sources disagree" in md and "## Evidence for each claim" in md
    assert "Stopped: converged: round 2 added no new claim." in md
    assert "—" not in md  # no em dashes
    mla = Y.render_markdown(Y.build_report(ctx.questions, ctx.cards, state, style="mla"))
    assert "(Cepeda et al.)" in mla and "## References (MLA)" in mla


def test_state_and_budget_round_trip():
    s = Y.CycleState(round=2, answers={0: Y.Answer("q", [Y.Sentence("t", ["f-1"])], "low", "r", ["x"], [], ["f-1"], 1)},
                     claims=[[0, "f-1", 1]], judged_pairs=["a|b"], disagreements=[Y.Disagreement(0, "a", "b", "n")],
                     stop_reason="converged", log=["l"], tried_papers=["doi:1"])
    assert Y.CycleState.from_dict(json.loads(json.dumps(s.to_dict()))) == s
    b = Y.Budget.from_dict({"max_rounds": 5, "max_minutes": 30, "bogus": 1, "max_papers": -1, "followup_papers": True})
    assert (b.max_rounds, b.max_minutes, b.max_papers, b.followup_papers) == (5, 30.0, 14, 2)
