"""PL-10 T1: research runs as a background job in the goals engine.

Drives a whole job through the REAL GoalRuntime (ticked by hand) with a fake
paper client, a fake reader and a scripted model: brief, park, owner's
correction and CONTINUE, paper search, one card per paper, questions, park,
owner's edit and approval, answer rounds until the stop rule, the report, the
RESEARCH screen's record, and export in APA and MLA.
"""

from __future__ import annotations

import json
import re

import pytest

from dourmouse.goal_runtime import GoalRuntime, job_handler
from dourmouse.goals import GoalStore
from dourmouse.research_pipeline import research_job as J
from dourmouse.research_pipeline.papers import OPENALEX, Paper, SearchResult
from dourmouse.research_pipeline.reading import PaperText, Section

WIKI = ("The spacing effect is the finding that learning is more effective when study sessions are spaced out "
        "over time rather than massed together in one session.")
P1 = Paper(title="Spacing effects in learning: a temporal ridgeline", authors=("Nicholas J. Cepeda", "Hal Pashler"),
           year=2008, doi="10.1/p1", venue="Psychological Science", venue_type="journal", cited_by=900,
           abstract="The optimal gap between study sessions increased as the retention interval increased in 1354 learners.")
P2 = Paper(title="Spaced practice of mathematics in classrooms", authors=("Doug Rohrer",), year=2015, doi="10.1/p2",
           venue="Educational Psychology Review", venue_type="journal", cited_by=300,
           abstract="Spaced practice improved scores on a delayed mathematics test by 20 percentage points in classrooms.")
REVIEW = Paper(title="Distributed practice in verbal recall tasks: a review", authors=("N Cepeda",), year=2006,
               doi="10.1/rev", venue="Psychological Bulletin", venue_type="journal", cited_by=2000, is_review=True,
               abstract="Spaced learning produced better retention than massed learning across 254 studies of verbal recall.")


class FakeClient:
    def __init__(self):
        self.requests_sent = {OPENALEX: 1}

    def wikipedia_article(self, topic):
        return "Spacing effect", "https://en.wikipedia.org/wiki/Spacing_effect", WIKI

    def top_reviews(self, topic, n=2):
        return SearchResult(papers=[REVIEW], counts={OPENALEX: 1})

    def search(self, query, per_source=10):
        return SearchResult(papers=[P1, P2], counts={OPENALEX: 2})

    def s2_neighbours(self, paper, direction="references", limit=20):
        return []


def fake_read(p):
    return PaperText(p.key, "abstract", p.url, text=p.abstract, sections=(Section("Abstract", 0, len(p.abstract)),))


class Model:
    def __init__(self):
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if "one-page context brief" in prompt:
            return json.dumps({"key_terms": [{"term": "Spacing effect", "definition": "Spread-out study works better.",
                                              "source": "W", "quote": "learning is more effective when study sessions are spaced out"}],
                               "search_terms": ["spacing effect learning"]})
        if "make a short, factual card" in prompt:
            title = re.search(r"PAPER: (.*?) \(", prompt).group(1)
            p = {P1.title: P1, P2.title: P2, REVIEW.title: REVIEW}[title]
            quote = " ".join(p.abstract.split()[:12])
            return json.dumps({"findings": [{"text": p.abstract, "quote": quote}]})
        if "set the research" in prompt:
            return json.dumps({"question": "How should students space their study?", "goals": ["a schedule"],
                               "sub_questions": [{"text": "Does the optimal gap between study sessions depend on the retention interval?"},
                                                 {"text": "Does spaced practice improve delayed classroom test scores?"},
                                                 {"text": "Is spaced learning better than massed learning for verbal recall?"}]})
        if "SUB-QUESTION:" in prompt and "EVIDENCE:" in prompt:
            refs = re.findall(r"\[(E\d+)\][^\n]*Finding: ([^\n]*)\n\s+Quote: \"([^\"]+)\"", prompt)
            if not refs:
                return json.dumps({"sentences": []})
            ref, _finding, quote = refs[0]
            return json.dumps({"sentences": [{"text": quote + ".", "cites": [ref]}]})
        if "CONTRADICTION: yes or no" in prompt:
            return "CONTRADICTION: no\nNOTE: compatible"
        raise AssertionError("unexpected prompt: " + prompt[:200])


@pytest.fixture()
def env(monkeypatch, tmp_path):
    model = Model()
    monkeypatch.setattr(J, "_client", FakeClient)
    monkeypatch.setattr(J, "_complete", lambda: model)
    monkeypatch.setattr(J, "_read", fake_read)
    monkeypatch.setattr(J, "_now_year", lambda: 2026)
    store = GoalStore(tmp_path / "goals.db")
    monkeypatch.setattr(J, "_goal_store", lambda: store)
    runtime = GoalRuntime(store, registry=None)
    return store, runtime, model


def _tick_until(runtime, store, job_id, statuses, limit=40):
    for _ in range(limit):
        if store.get_goal(job_id)["status"] in statuses:
            return
        runtime.tick()
    raise AssertionError(f"goal stuck at {store.get_goal(job_id)['status']}: "
                         f"{[(t['description'], t['status'], t['last_error']) for t in store.list_tasks(job_id)]}")


def test_the_handler_is_registered_for_job_research():
    assert job_handler("research") is J.handle


def test_start_validates_input(env):
    with pytest.raises(ValueError):
        J.start_job("   ")
    with pytest.raises(ValueError):
        J.start_job("x" * 301)
    with pytest.raises(ValueError):
        J.start_job("spacing", style="chicago")


def test_a_whole_job_with_both_owner_decisions(env):
    store, runtime, model = env
    job_id = J.start_job("spacing effect")
    _tick_until(runtime, store, job_id, {"WAITING_FOR_APPROVAL"})
    view = J.job_view(job_id)
    assert view["waiting_for"] == "brief" and "Spacing effect" in view["brief"]["markdown"]
    assert store.get_goal(job_id)["blocked_reason"] == J.PARK_BRIEF

    J.correct_brief(job_id, "Focus on school students.")
    assert J.continue_after_brief(job_id) is True
    _tick_until(runtime, store, job_id, {"WAITING_FOR_APPROVAL"})
    view = J.job_view(job_id)
    assert view["waiting_for"] == "questions"
    assert len(view["cards"]) == 3 and {c["paper"]["doi"] for c in view["cards"]} == {"10.1/p1", "10.1/p2", "10.1/rev"}
    assert "OWNER'S CORRECTIONS (these override the brief):\nFocus on school students." in \
        next(p for p in model.prompts if "set the research" in p)

    J.edit_questions(job_id, {"question": "How should school students space their study?",
                              "sub_questions": ["Does the optimal gap between study sessions depend on the retention interval?",
                                                "Does spaced practice improve delayed classroom test scores?"]})
    assert J.approve_questions(job_id) is True
    _tick_until(runtime, store, job_id, {"COMPLETED"})

    view = J.job_view(job_id)
    report = view["report"]
    assert report["question"] == "How should school students space their study?"
    assert report["stop_reason"].startswith("converged")
    assert all(a["sentences"] for a in report["answers"])
    assert report["references"][0].startswith("Cepeda, N. J., & Pashler, H. (2008).")
    steps = [s["stage"] for s in view["steps"]]
    assert steps[:2] == ["brief", "papers"] and steps.count("card") == 3 and steps[-1] == "present"
    assert view["spend"]["papers_read"] == 3

    name, mime, md = J.export(job_id)
    assert name == "spacing-effect.md" and mime == "text/markdown" and "## References (APA)" in md
    _, _, mla = J.export(job_id, style="mla")
    assert "## References (MLA)" in mla and "(Cepeda and Pashler)" in mla
    _, _, raw = J.export(job_id, fmt="json")
    assert json.loads(raw)["question"].startswith("How should school")

    from dourmouse.research_pipeline.store import ResearchStore, default_db
    rec = ResearchStore(default_db()).load(report["question"])
    assert rec is not None and rec.stage.name == "SYNTHESIZED"
    assert len(rec.plan) == 2 and rec.claims and all(c.agent == "research_job" for c in rec.claims)


def test_nothing_moves_without_the_owner(env):
    store, runtime, _ = env
    job_id = J.start_job("spacing effect")
    _tick_until(runtime, store, job_id, {"WAITING_FOR_APPROVAL"})
    for _ in range(5):
        runtime.tick()
    assert store.get_goal(job_id)["status"] == "WAITING_FOR_APPROVAL"
    assert [s["stage"] for s in J.job_view(job_id)["steps"]] == ["brief"]
    assert J.approve_questions(job_id) is False  # nothing parked at the questions yet


def test_without_the_brief_pause_the_job_runs_to_the_questions(env):
    store, runtime, _ = env
    job_id = J.start_job("spacing effect", pause_after_brief=False)
    _tick_until(runtime, store, job_id, {"WAITING_FOR_APPROVAL"})
    assert J.job_view(job_id)["waiting_for"] == "questions"
    with pytest.raises(ValueError, match="already set"):
        J.correct_brief(job_id, "too late")


def test_approving_from_the_goals_screen_uses_the_questions_as_they_are(env):
    store, runtime, _ = env
    job_id = J.start_job("spacing effect", pause_after_brief=False)
    _tick_until(runtime, store, job_id, {"WAITING_FOR_APPROVAL"})
    parked = next(t for t in store.list_tasks(job_id) if t["status"] == "WAITING_FOR_APPROVAL")
    assert store.resolve_task_approval(parked["id"], True)
    _tick_until(runtime, store, job_id, {"COMPLETED"})
    assert J.job_view(job_id)["questions"]["status"] == "approved"


def test_declining_blocks_the_job(env):
    store, runtime, _ = env
    job_id = J.start_job("spacing effect")
    _tick_until(runtime, store, job_id, {"WAITING_FOR_APPROVAL"})
    parked = next(t for t in store.list_tasks(job_id) if t["status"] == "WAITING_FOR_APPROVAL")
    store.resolve_task_approval(parked["id"], False, "wrong topic")
    assert store.get_goal(job_id)["status"] == "BLOCKED"


def test_export_before_answers_is_refused(env):
    job_id = J.start_job("spacing effect")
    with pytest.raises(ValueError, match="no answers yet"):
        J.export(job_id)
    with pytest.raises(KeyError):
        J.export("goal_missing")


def test_a_task_that_is_not_a_job_step_fails_honestly(env):
    store, runtime, _ = env
    goal = store.create_goal("stray")
    store.create_task(goal["id"], "stray step", assigned_agent="job:research")
    store.update_goal_status(goal["id"], "EXECUTING")
    runtime.tick()
    runtime.tick()
    t = store.list_tasks(goal["id"])[0]
    assert t["status"] in ("RETRYING", "FAILED") and "no research job" in t["last_error"]


def test_an_unknown_job_kind_fails_honestly(env):
    store, runtime, _ = env
    goal = store.create_goal("stray")
    store.create_task(goal["id"], "x", assigned_agent="job:nonexistent")
    store.update_goal_status(goal["id"], "EXECUTING")
    runtime.tick()
    assert "no job handler is registered for 'nonexistent'" in store.list_tasks(goal["id"])[0]["last_error"]


def test_chat_tools_start_report_and_compute_statistics(env):
    from dourmouse import research_pipeline_tools as rpt

    store, runtime, _ = env
    out = rpt._research_start_job_tool({"topic": "spacing effect", "pause_after_brief": False})
    assert out.startswith("Started research job goal_") and "Nothing has been answered yet" in out
    job_id = re.search(r"job (goal_\w+)", out).group(1)
    _tick_until(runtime, store, job_id, {"WAITING_FOR_APPROVAL"})
    status = rpt._research_job_status_tool({"job_id": job_id})
    assert "waiting for the owner" in status and "cards: 3" in status
    assert "Recent research jobs:" in rpt._research_job_status_tool({})
    assert rpt._research_start_job_tool({"topic": ""}).startswith("ERROR")
    assert rpt._research_statistics_tool({"values": [64, 37]}).startswith("n=2, mean=50.5")
    assert "p=" in rpt._research_statistics_tool({"values": [1.0, 2.0, 3.0], "null_mean": 0})
    assert rpt._research_statistics_tool({"values": [1]}).startswith("Not computed")
    assert rpt._research_statistics_tool({"values": [1, "two"]}).startswith("ERROR")


def test_api_routes_drive_the_owner_decisions(env):
    from dourmouse.os_api import ApiError, Request
    from dourmouse.os_api import research as api

    store, runtime, _ = env
    status, d = api.job_start(Request(server=None, body={"topic": "spacing effect"}))
    job_id = d["id"]
    with pytest.raises(ApiError) as exc:
        api.job_start(Request(server=None, body={"topic": ""}))
    assert exc.value.status == 400
    with pytest.raises(ApiError) as exc:
        api.job_continue(Request(server=None, body={"id": job_id}))
    assert exc.value.status == 409  # not parked yet
    _tick_until(runtime, store, job_id, {"WAITING_FOR_APPROVAL"})
    assert api.job_brief(Request(server=None, body={"id": job_id, "owner_notes": "school level"}))[0] == 200
    assert api.job_continue(Request(server=None, body={"id": job_id}))[0] == 200
    _tick_until(runtime, store, job_id, {"WAITING_FOR_APPROVAL"})
    with pytest.raises(ApiError) as exc:
        api.job_questions(Request(server=None, body={"id": job_id, "question": "", "sub_questions": ["a"]}))
    assert exc.value.status == 400
    status, d = api.job_questions(Request(server=None, body={
        "id": job_id, "question": "How should students space study?",
        "sub_questions": ["Does spaced practice improve delayed classroom test scores?"]}))
    assert d["questions"]["edited_by_owner"] is True
    assert api.job_approve(Request(server=None, body={"id": job_id}))[0] == 200
    _tick_until(runtime, store, job_id, {"COMPLETED"})
    _, listing = api.jobs_list(Request(server=None))
    assert listing["jobs"][0]["answered"] is True
    _, detail = api.job_detail(Request(server=None, query={"id": [job_id]}))
    assert detail["report"]["answers"][0]["sentences"]
    _, exp = api.job_export(Request(server=None, query={"id": [job_id], "style": ["mla"]}))
    assert exp["filename"] == "spacing-effect.md" and "## References (MLA)" in exp["content"]
