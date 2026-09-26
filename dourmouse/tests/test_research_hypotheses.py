"""Finding #131 (R4: RES-13, 14, 15, 17): hypotheses grounded in real
claims, a critic's review recorded, a designed experiment actually run,
and statistics computed without a model."""

from __future__ import annotations

import json

import pytest

from dourmouse.research_graph.store import GraphStore
from dourmouse.research_pipeline import hypotheses as hy


def _graph(tmp_path):
    g = GraphStore(tmp_path / "g.db")
    g.put("research_question", "q1", {"text": "Does caffeine improve reaction time?"}, created_by="t")
    for i, text in enumerate(("A 2019 trial found 200mg caffeine cut reaction time by 18ms.",
                              "Effects shrink in habitual coffee drinkers.")):
        g.put("claim", f"c{i}", {"text": text}, created_by="t")
        g.link(("claim", f"c{i}"), "answers", ("research_question", "q1"), created_by="t")
    return g


def _model(reply):
    seen = []

    def complete(prompt):
        seen.append(prompt)
        return reply if isinstance(reply, str) else json.dumps(reply)
    complete.seen = seen
    return complete


def test_hypotheses_must_rest_on_real_claims(tmp_path):
    g = _graph(tmp_path)
    m = _model({"hypotheses": [
        {"statement": "Caffeine's benefit is smaller for habitual drinkers", "rationale": "tolerance", "claims": [1, 2]},
        {"statement": "Invented with no evidence", "claims": []},
        {"statement": "Cites a claim that does not exist", "claims": [9]},
    ]})
    r = hy.generate_hypotheses("q1", complete=m, store=g)
    assert r["ok"] and r["dropped"] == 2 and len(r["hypotheses"]) == 1
    hid = r["hypotheses"][0]["id"]
    assert sorted(o.id for o in g.related(("hypothesis", hid), "derived_from")) == ["c0", "c1"]
    assert "[1] A 2019 trial" in m.seen[0] and "[2] Effects shrink" in m.seen[0]


def test_no_claims_means_no_hypotheses(tmp_path):
    g = GraphStore(tmp_path / "g.db")
    g.put("research_question", "q2", {"text": "?"}, created_by="t")
    assert hy.generate_hypotheses("q2", complete=_model({}), store=g)["ok"] is False


def test_a_criticism_is_recorded_and_sets_the_verdict(tmp_path):
    g = _graph(tmp_path)
    g.put("hypothesis", "h1", {"statement": "s", "status": "proposed"}, created_by="t")
    r = hy.criticize("h1", complete=_model({"weakest_assumption": "one small trial", "alternative": "placebo",
                                            "refuted_if": "a larger trial finds no effect", "verdict": "weak"}),
                     store=g)
    assert r["verdict"] == "weak" and g.get("hypothesis", "h1").body["status"] == "criticized: weak"
    decision = g.related(("hypothesis", "h1"), "revised_by")[0]
    assert "a larger trial finds no effect" in decision.body["rationale"]
    assert hy.criticize("h1", complete=_model("no json here"), store=g)["ok"] is False
    echoed = {"weakest_assumption": "...", "alternative": "...", "refuted_if": "...",
              "verdict": "plausible|weak|untestable"}
    assert hy.criticize("h1", complete=_model(echoed), store=g)["ok"] is False  # found live


def test_a_designed_experiment_runs_and_its_statistics_are_computed(tmp_path):
    g = _graph(tmp_path)
    g.put("hypothesis", "h1", {"statement": "a seeded normal sample has mean near zero"}, created_by="t")
    code = ("import json, os, random\nrandom.seed(3)\nxs=[random.gauss(0,1) for _ in range(400)]\n"
            "os.makedirs('out', exist_ok=True)\njson.dump({'samples': xs}, open('out/data.json','w'))\n"
            "json.dump({'n': len(xs)}, open('out/metrics.json','w'))")
    r = hy.design_and_run("h1", complete=_model({"protocol": "draw 400 seeded normals", "code": code,
                                                 "null_value": 0}), store=g, wait_s=60)
    assert r["ok"] and r["state"] == "succeeded"
    st = r["statistics"]
    assert st["n"] == 400 and st["ci95"][0] < 0 < st["ci95"][1] and st["p_two_sided"] > 0.05
    assert [o.id for o in g.related(("hypothesis", "h1"), "tested_by")] == [r["experiment_id"]]
    assert any(e.src_id == f"stats-{r['run_id']}" for e in g.edges(dst=("experiment_run", r["run_id"]), relation="about"))


def test_describe_samples_is_correct_and_refuses_too_little_data():
    s = hy.describe_samples([1, 2, 3, 4, 5], null_mean=0)
    assert s["mean"] == 3 and s["sd"] == pytest.approx(1.5811, abs=1e-4) and s["p_two_sided"] < 0.01
    assert "rough" in s["method"]
    assert "error" in hy.describe_samples([1])


def test_without_a_null_value_no_p_value_is_claimed(tmp_path):
    """Found live: a dice-average hypothesis was 'tested' against 0."""
    g = _graph(tmp_path)
    g.put("hypothesis", "h2", {"statement": "dice average is 3.5"}, created_by="t")
    code = ("import json, os, random\nrandom.seed(1)\nxs=[random.randint(1,6) for _ in range(300)]\n"
            "os.makedirs('out', exist_ok=True)\njson.dump({'samples': xs}, open('out/data.json','w'))")
    st = hy.design_and_run("h2", complete=_model({"protocol": "roll dice", "code": code}), store=g,
                           wait_s=60)["statistics"]
    assert "p_two_sided" not in st and st["method"].startswith("description only")
    assert 3.2 < st["mean"] < 3.8


def test_json_is_found_inside_prose_and_fences():
    """Found live: a design wrapped in a ```json fence with prose after it
    that itself contained braces."""
    reply = ('I cannot run it, but here is the design.\n```json\n{"protocol": "p", "code": "d = {1: 2}\\nprint(d)"}\n```\n'
             'Afterwards, check {n: deviation} shrinks.')
    assert hy._json(reply) == {"protocol": "p", "code": "d = {1: 2}\nprint(d)"}
    assert hy._json('prefix {"a": 1} suffix {bad}') == {"a": 1}
    assert hy._json("no object") is None


def test_json_scan_is_bounded_on_unclosed_braces():
    """S30: 100000 unclosed braces took about 4.8s (one full scan per brace)."""
    import time

    t0 = time.perf_counter()
    assert hy._json("{" * 100000) is None
    assert time.perf_counter() - t0 < 1.0
    # a real object after a modest run of bad starts is still found
    assert hy._json("{" * 50 + ' {"a": 1}') == {"a": 1}
    # beyond the attempt cap the scan gives up rather than grinding on
    assert hy._json("{" * (hy._JSON_MAX_ATTEMPTS + 10) + ' {"a": 1}') is None
    # input beyond the length cap is not scanned
    assert hy._json(" " * (hy._JSON_MAX_CHARS + 10) + '{"a": 1}') is None
