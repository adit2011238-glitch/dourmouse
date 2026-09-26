"""Finding #151: the RESEARCH screen's read-only routes (os_api/research.py)."""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from dourmouse.general_roster import build_general_registry


def _seed(g):
    b = "t"
    g.put("project", "p1", {"name": "Coffee"}, created_by=b, project_id="p1")
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
    g.put("hypothesis", "h1", {"statement": "coffee lowers risk"}, created_by=b)
    g.put("experiment", "ex1", {"protocol": "seeded sample", "status": "succeeded"}, created_by=b)
    g.link(("hypothesis", "h1"), "tested_by", ("experiment", "ex1"), created_by=b)


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("DOURMOUSE_CONFIG_DIR", str(tmp_path / "cfg"))
    from dourmouse.webui import run_server

    srv = run_server(build_general_registry(), port=0, client=None, config=None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


@pytest.fixture
def seeded(server):
    from dourmouse.research_graph.store import GraphStore, default_db

    _seed(GraphStore(default_db()))
    return server


def call(srv, path):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    conn.request("GET", path)
    resp = conn.getresponse()
    data = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, data


class TestLoop:
    def test_built_means_the_tool_is_registered_and_the_backward_edge_is_checked(self, server):
        status, d = call(server, "/api/os/research/loop")
        assert status == 200 and d["total"] == 15
        by = {s["key"]: s for s in d["stages"]}
        assert by["extract"]["built"] is True and by["hypothesis"]["built"] is True
        assert by["validate"]["built"] is False and by["stats"]["built"] is False, "no tool backs these"
        assert d["built"] == sum(1 for s in d["stages"] if s["built"])
        assert d["back"]["built"] is True and d["back"]["max_follow_ups"] == 3

    def test_a_tool_missing_from_the_registry_is_not_built(self, server):
        server.registry._tools.pop("research_critique", None)
        by = {s["key"]: s for s in call(server, "/api/os/research/loop")[1]["stages"]}
        assert by["critique"]["built"] is False

    def test_empty_graph_counts_are_zero_not_missing(self, server):
        d = call(server, "/api/os/research/loop")[1]
        assert d["counts"]["claim"] == 0 and d["error"] == ""


class TestQuestions:
    def test_empty(self, server):
        d = call(server, "/api/os/research/questions")[1]
        assert d["questions"] == [] and d["hypotheses"] == []

    def test_lists_questions_with_real_counts(self, seeded):
        d = call(seeded, "/api/os/research/questions")[1]
        q = d["questions"][0]
        assert q["text"] == "Is coffee good for you?" and q["claims"] == 2 and q["open_contradictions"] == 1
        assert q["stage"] == ""  # no pipeline record for it: reported as unknown, not invented
        assert d["hypotheses"][0]["statement"] == "coffee lowers risk"

    def test_question_detail_marks_contested_and_carries_passage_and_source(self, seeded):
        status, d = call(seeded, "/api/os/research/question?id=q1")
        assert status == 200
        c0 = [c for c in d["claims"] if c["id"] == "c0"][0]
        assert c0["contested"] is True and c0["passage"] == "a short rise" and c0["source"] == "https://a.example/bp"
        assert [c for c in d["claims"] if c["id"] == "c1"][0]["contested"] is False

    def test_unknown_and_malformed_ids(self, seeded):
        assert call(seeded, "/api/os/research/question?id=nope")[0] == 404
        assert call(seeded, "/api/os/research/question?id=a%20b")[0] == 400
        assert call(seeded, "/api/os/research/question")[0] == 400


class TestGraph:
    def test_question_neighbourhood_has_typed_edges_and_no_passages(self, seeded):
        d = call(seeded, "/api/os/research/graph?question=q1")[1]
        types = {n["type"] for n in d["nodes"]}
        assert {"research_question", "claim", "contradiction"} <= types and "passage" not in types
        rels = {e["relation"] for e in d["edges"]}
        assert {"decomposes_into", "answers", "about", "contradicted_by"} <= rels

    def test_hypothesis_neighbourhood(self, seeded):
        d = call(seeded, "/api/os/research/graph?hypothesis=h1")[1]
        assert {(e["src"], e["relation"], e["dst"]) for e in d["edges"]} == {("h1", "tested_by", "ex1")}

    def test_exactly_one_root_and_404(self, seeded):
        assert call(seeded, "/api/os/research/graph")[0] == 400
        assert call(seeded, "/api/os/research/graph?question=q1&hypothesis=h1")[0] == 400
        assert call(seeded, "/api/os/research/graph?hypothesis=zzz")[0] == 404

    def test_node_count_is_capped(self, seeded):
        from dourmouse.research_graph.store import GraphStore, default_db

        g = GraphStore(default_db())
        for i in range(60):
            g.put("claim", f"cx{i}", {"text": f"claim {i}"}, created_by="t")
            g.link(("claim", f"cx{i}"), "answers", ("research_question", "q1"), created_by="t")
        d = call(seeded, "/api/os/research/graph?question=q1")[1]
        assert len(d["nodes"]) <= 40 and d["truncated"] is True


class TestExport:
    def test_markdown_has_claims_passages_and_source_urls(self, seeded):
        d = call(seeded, "/api/os/research/export?question=q1")[1]
        assert d["filename"].endswith(".md") and "https://a.example/bp" in d["content"]
        assert "[contested]" in d["content"] and "> a short rise" in d["content"]

    def test_json_and_bad_format(self, seeded):
        d = call(seeded, "/api/os/research/export?question=q1&format=json")[1]
        assert json.loads(d["content"])["id"] == "q1"
        assert call(seeded, "/api/os/research/export?question=q1&format=pdf")[0] == 400
        assert call(seeded, "/api/os/research/export?question=nope")[0] == 404
