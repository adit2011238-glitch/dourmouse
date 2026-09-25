"""Finding #127 (R5 + RES-18): experiments are real graph objects run on
this Mac's job runner, and a replication says plainly whether it held."""

from __future__ import annotations

import pytest

from dourmouse.research_graph.store import GraphStore
from dourmouse.research_pipeline import experiments as ex

DETERMINISTIC = """
import json, os, random
random.seed(7)
xs = [random.gauss(0, 1) for _ in range(1000)]
os.makedirs("out", exist_ok=True)
json.dump({"n": len(xs), "mean": round(sum(xs) / len(xs), 6)}, open("out/metrics.json", "w"))
print("done")
"""

NONDETERMINISTIC = """
import json, os, time
os.makedirs("out", exist_ok=True)
json.dump({"clock": time.time_ns()}, open("out/metrics.json", "w"))
"""


def test_a_run_is_recorded_with_everything_the_spec_names(tmp_path):
    g = GraphStore(tmp_path / "graph.db")
    g.put("hypothesis", "h1", {"statement": "a seeded sample has mean near zero"}, created_by="t")
    r = ex.run_experiment("Seeded gaussian sample; mean should be near zero.", DETERMINISTIC,
                          hypothesis_id="h1", store=g, wait_s=60)
    assert r["state"] == "succeeded"
    run = g.get("experiment_run", r["run_id"])
    assert run.body["exit_code"] == 0 and run.body["node"] == "this Mac" and run.body["environment_hash"]
    assert run.body["started_at"] and run.body["finished_at"] and "done" in run.body["logs"]
    assert [e.id for e in g.related(("hypothesis", "h1"), "tested_by")] == [r["experiment_id"]]
    produced = g.related(("experiment_run", r["run_id"]), "produced")
    metrics = {o.body["name"]: o.body["value"] for o in produced if o.type == "metric"}
    assert metrics["n"] == 1000 and isinstance(metrics["mean"], float)
    assert any(o.type == "result" and "succeeded" in o.body["summary"] for o in produced)
    assert g.get("experiment", r["experiment_id"]).body["status"] == "succeeded"
    assert "metric n = 1000" in ex.describe_run(r["run_id"], store=g)


def test_replication_holds_for_a_deterministic_run_and_fails_for_a_clock(tmp_path):
    g = GraphStore(tmp_path / "graph.db")
    a = ex.run_experiment("seeded", DETERMINISTIC, store=g, wait_s=60)
    rep = ex.replicate(a["run_id"], store=g, wait_s=60)
    assert rep["verdict"] == "replicated: all 2 metric(s) identical"
    assert [o.id for o in g.related(("experiment_run", rep["run_id"]), "replicates")] == [a["run_id"]]
    b = ex.run_experiment("clock", NONDETERMINISTIC, store=g, wait_s=60)
    rep_b = ex.replicate(b["run_id"], store=g, wait_s=60)
    assert rep_b["verdict"].startswith("did NOT replicate: clock:")


def test_a_failing_experiment_is_recorded_as_failed(tmp_path):
    g = GraphStore(tmp_path / "graph.db")
    r = ex.run_experiment("raises", "raise SystemExit(3)", store=g, wait_s=60)
    assert r["state"] == "failed" and g.get("experiment_run", r["run_id"]).body["exit_code"] == 3


def test_an_experiment_needs_a_protocol_and_code(tmp_path):
    with pytest.raises(ValueError):
        ex.run_experiment("", "print(1)", store=GraphStore(tmp_path / "g.db"))


def test_metrics_written_next_to_the_code_are_still_recorded(tmp_path):
    """Finding #130, seen live: the research agent's code wrote metrics.json
    beside main.py rather than into out/; the numbers still count."""
    g = GraphStore(tmp_path / "graph.db")
    r = ex.run_experiment("root metrics", "import json\njson.dump({'mean': 0.5}, open('metrics.json', 'w'))",
                          store=g, wait_s=60)
    metrics = {o.body["name"]: o.body["value"] for o in g.related(("experiment_run", r["run_id"]), "produced")
               if o.type == "metric"}
    assert metrics == {"mean": 0.5} and r["job"]["metrics_source"].startswith("metrics.json (job folder")
