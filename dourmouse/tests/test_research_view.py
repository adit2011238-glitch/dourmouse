"""Finding #129 (R8): the research view summarises the graph faithfully:
questions with their claims and contradictions, each claim traced to its
passage and source, and hypotheses with experiments and runs."""

from __future__ import annotations

from dourmouse.research_graph import view
from dourmouse.research_graph.store import GraphStore


def _graph(tmp_path):
    g = GraphStore(tmp_path / "g.db")
    b = "t"
    g.put("project", "p1", {"name": "Is coffee good for you?"}, created_by=b, project_id="p1")
    g.put("research_question", "q1", {"text": "Is coffee good for you?"}, created_by=b)
    g.link(("research_question", "q1"), "part_of", ("project", "p1"), created_by=b)
    g.put("research_question", "q2", {"text": "Does coffee raise blood pressure?"}, created_by=b)
    g.link(("research_question", "q1"), "decomposes_into", ("research_question", "q2"), created_by=b)
    for n, (text, url, quote) in enumerate((("It raises BP briefly", "https://a.example/bp", "a short rise"),
                                            ("It does not raise BP", "https://b.example/bp", "no change"))):
        g.put("source", f"s{n}", {"url": url}, created_by=b)
        g.put("document", f"d{n}", {"raw_sha256": f"h{n}"}, created_by=b)
        g.link(("document", f"d{n}"), "derived_from", ("source", f"s{n}"), created_by=b)
        g.put("passage", f"pa{n}", {"text": quote}, created_by=b)
        g.link(("passage", f"pa{n}"), "part_of", ("document", f"d{n}"), created_by=b)
        g.put("evidence", f"e{n}", {"stance": "supports"}, created_by=b)
        g.link(("evidence", f"e{n}"), "extracted_from", ("passage", f"pa{n}"), created_by=b)
        g.put("claim", f"c{n}", {"text": text}, created_by=b)
        g.link(("claim", f"c{n}"), "supported_by", ("evidence", f"e{n}"), created_by=b)
        g.link(("claim", f"c{n}"), "answers", ("research_question", "q2"), created_by=b)
    g.put("contradiction", "x1", {"note": "short-term rise vs none"}, created_by=b)
    for n in (0, 1):
        g.link(("contradiction", "x1"), "about", ("claim", f"c{n}"), created_by=b)
    g.link(("claim", "c0"), "contradicted_by", ("claim", "c1"), created_by=b)
    g.put("task", "t1", {"title": "find a meta-analysis", "stage": "discover"}, created_by=b)
    g.link(("contradiction", "x1"), "spawned", ("task", "t1"), created_by=b)
    g.put("hypothesis", "h1", {"statement": "a seeded mean is near zero"}, created_by=b)
    g.put("experiment", "ex1", {"protocol": "seeded sample", "status": "succeeded"}, created_by=b)
    g.link(("hypothesis", "h1"), "tested_by", ("experiment", "ex1"), created_by=b)
    g.put("experiment_run", "r1", {"status": "succeeded"}, created_by=b)
    g.link(("experiment_run", "r1"), "run_of", ("experiment", "ex1"), created_by=b)
    g.put("metric", "m1", {"name": "mean", "value": 0.01}, created_by=b)
    g.link(("experiment_run", "r1"), "produced", ("metric", "m1"), created_by=b)
    return g


def test_overview(tmp_path):
    o = view.overview(_graph(tmp_path))
    assert o["counts"]["claim"] == 2 and o["counts"]["experiment_run"] == 1
    q = o["questions"][0]
    assert (q["text"], q["sub_questions"], q["claims"], q["contradictions"], q["open_contradictions"]) == (
        "Is coffee good for you?", 1, 2, 1, 1)
    h = o["hypotheses"][0]
    assert h["experiments"][0]["runs"][0]["metrics"] == {"mean": 0.01}
    assert [e["id"] for e in o["experiments"]] == ["ex1"]


def test_a_question_traces_every_claim_to_its_source(tmp_path):
    d = view.question_detail(_graph(tmp_path), "q1")
    by_text = {c["text"]: c for c in d["claims"]}
    assert by_text["It raises BP briefly"]["source"] == "https://a.example/bp"
    assert by_text["It raises BP briefly"]["passage"] == "a short rise"
    assert by_text["It raises BP briefly"]["sub_question"] == "Does coffee raise blood pressure?"
    assert by_text["It raises BP briefly"]["contradicted_by"] == ["c1"]
    assert d["contradictions"] == [{"id": "x1", "note": "short-term rise vs none", "status": "open",
                                    "spawned": ["find a meta-analysis"]}]
