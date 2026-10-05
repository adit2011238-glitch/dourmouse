"""G0 tool-use benchmark harness: task data, scoring, stub run, safety refusals."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

RUN = Path(__file__).resolve().parents[2] / "scripts" / "bench" / "run.py"
_spec = importlib.util.spec_from_file_location("bench_run", RUN)
assert _spec is not None and _spec.loader is not None
bench_run = importlib.util.module_from_spec(_spec)
sys.modules["bench_run"] = bench_run
_spec.loader.exec_module(bench_run)


@pytest.fixture(scope="module")
def tasks():
    return bench_run.load_tasks()


def test_forty_tasks_ten_categories(tasks):
    assert len(tasks) == 40
    cats = {t["category"] for t in tasks}
    assert cats == {"files", "browser", "media", "mail", "calendar", "docs", "system", "research", "code", "refusal"}


def test_every_task_references_real_registry_tools(tasks):
    problems = bench_run.validate_tasks(tasks, bench_run.registry_tool_names())
    assert problems == []


def test_validate_catches_an_invented_tool(tasks):
    bad = [{"id": "x", "category": "c", "prompt": "p", "expected_tools": ["no_such_tool"], "pass_rule": "all_of"}]
    problems = bench_run.validate_tasks(bad, {"read_path"})
    assert any("no_such_tool" in p for p in problems)


def test_scoring_rules():
    all_of = {"id": "a", "pass_rule": "all_of", "expected_tools": ["x", "y"]}
    assert bench_run.score_task(all_of, [{"name": "x"}, {"name": "y"}])["passed"]
    assert not bench_run.score_task(all_of, [{"name": "x"}])["passed"]
    any_of = {"id": "b", "pass_rule": "any_of", "expected_tools": ["x", "y"]}
    assert bench_run.score_task(any_of, [{"name": "y"}])["passed"]
    assert not bench_run.score_task(any_of, [{"name": "z"}])["passed"]
    none_of = {"id": "c", "pass_rule": "none_of", "expected_tools": [], "forbidden_tools": ["bad"]}
    assert bench_run.score_task(none_of, [])["passed"]
    assert not bench_run.score_task(none_of, [{"name": "bad"}])["passed"]


def test_arg_checks():
    task = {"id": "d", "pass_rule": "all_of", "expected_tools": ["t"],
            "arg_checks": [{"tool": "t", "key": "k", "op": "contains", "value": "Hello"}]}
    assert bench_run.score_task(task, [{"name": "t", "args": {"k": "say hello there"}}])["passed"]
    assert not bench_run.score_task(task, [{"name": "t", "args": {"k": "bye"}}])["passed"]
    assert not bench_run.score_task(task, [{"name": "t", "args": {}}])["passed"]


def test_oracle_stub_run_passes_everything(tmp_path, capsys):
    out = tmp_path / "r.json"
    assert bench_run.main(["--stub", "--out", str(out)]) == 0
    data = json.loads(out.read_text())
    assert data["summary"]["total"] == 40 and data["summary"]["passed"] == 40
    assert "STUB RUN" in capsys.readouterr().out


def test_decoy_policy_reports_failures_with_tool_called(tmp_path, capsys):
    out = tmp_path / "r.json"
    bench_run.main(["--stub-policy", "decoy", "--out", str(out)])
    data = json.loads(out.read_text())
    failed = [r for r in data["results"] if not r["passed"]]
    assert failed and data["summary"]["pass_rate"] < 1
    text = capsys.readouterr().out
    assert "Failing tasks:" in text and "model called" in text
    assert "refusal" in data["summary"]["by_category"]


def test_mixed_policy_is_partial(tmp_path):
    out = tmp_path / "r.json"
    bench_run.main(["--stub-policy", "mixed", "--out", str(out)])
    rate = json.loads(out.read_text())["summary"]["pass_rate"]
    assert 0 < rate < 1


@pytest.mark.parametrize("port", [8765, 9333, 9334])
def test_real_mode_refuses_owner_ports(port, capsys):
    assert bench_run.main(["--real", "--port", str(port)]) == 2
    assert "refusing port" in capsys.readouterr().err


def test_real_and_stub_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        bench_run.main(["--real", "--stub"])


def test_default_is_stub_and_default_port_is_not_owner():
    assert bench_run.DEFAULT_REAL_PORT not in bench_run.OWNER_PORTS
    # no --real flag means the stub runs: it must succeed without any server on the default port
    assert bench_run.main(["--only", "files", "--out", str(Path("/tmp/dm_bench_unit_stub.json"))]) == 0
