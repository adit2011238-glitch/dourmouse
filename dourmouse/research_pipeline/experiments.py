"""Experiments as first-class research objects (R5, and R4's replication
stage RES-18; finding #127).

The spec's experiment record: protocol, code, dataset, environment hash,
node, timestamps, exit code, artifacts, metrics, logs. Every one of those
is now real and linked in the research graph:

    hypothesis --tested_by--> experiment <--run_of-- experiment_run
    experiment_run --produced--> metric (one per out/metrics.json key)
    experiment_run --produced--> result
    experiment_run --replicates--> experiment_run   (a replication)

Runs execute on this Mac's sandboxed job runner (the `compute` agent's,
finding #113): own folder, scrubbed environment, timeout, memory watchdog,
environment hash. A run that outlives the wait is recorded as running and
brought up to date by ``refresh_run``. A replication runs the same code
again and states plainly whether every metric came out the same.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from dourmouse import compute_local
from dourmouse.research_graph.store import GraphStore
from dourmouse.research_graph.store import default_db as _graph_db

BY = "experiments"


def _store(store: GraphStore | None) -> GraphStore:
    return store or GraphStore(_graph_db())


def _record_run(store: GraphStore, exp_id: str, status: dict[str, Any], project_id: str | None) -> str:
    """Write (or advance) the experiment_run for a job, plus its metrics and
    result once it has finished."""
    run_id = "run-" + status["id"][:16]
    env = status.get("environment") or {}
    body = {
        "status": status.get("state", "unknown"), "environment_hash": str(env.get("sha256", "")),
        "node": "this Mac", "started_at": status.get("started_at"), "finished_at": status.get("finished_at"),
        "exit_code": status.get("exit_code"),
        "logs": ((status.get("stdout_tail") or "") + ("\n[stderr]\n" + status["stderr_tail"]
                                                      if status.get("stderr_tail") else ""))[:4000],
    }
    try:
        current = store.get("experiment_run", run_id)
    except KeyError:  # not recorded yet
        current = None
    if current is None:
        store.put("experiment_run", run_id, body, created_by=BY, project_id=project_id)
        store.link(("experiment_run", run_id), "run_of", ("experiment", exp_id), created_by=BY)
    elif current.body != body:
        store.revise("experiment_run", run_id, body, created_by=BY)
    if status.get("state") in ("succeeded", "failed", "timed_out"):
        for name, value in sorted((status.get("metrics") or {}).items()):
            if name.startswith("_"):
                continue
            store.put("metric", f"met-{run_id}-{name}"[:120], {"name": name, "value": value},
                      created_by=BY, project_id=project_id)
            store.link(("experiment_run", run_id), "produced", ("metric", f"met-{run_id}-{name}"[:120]), created_by=BY)
        summary = f"{status['state']} (exit {status.get('exit_code')})"
        if status.get("metrics"):
            summary += "; " + ", ".join(f"{k}={v}" for k, v in sorted(status["metrics"].items()) if not k.startswith("_"))
        store.put("result", f"res-{run_id}", {"summary": summary[:1000], "data": {"artifacts": status.get("artifacts") or {}}},
                  created_by=BY, project_id=project_id)
        store.link(("experiment_run", run_id), "produced", ("result", f"res-{run_id}"), created_by=BY)
        exp = store.get("experiment", exp_id)
        if exp.body.get("status") != status["state"]:
            store.revise("experiment", exp_id, {"status": status["state"]}, created_by=BY)
    return run_id


def run_experiment(protocol: str, code: str, *, hypothesis_id: str | None = None, project_id: str | None = None,
                   timeout_s: int = 600, memory_mb: int = 4096, wait_s: float = 120.0,
                   store: GraphStore | None = None) -> dict[str, Any]:
    protocol, code = (protocol or "").strip(), code or ""
    if not protocol or not code.strip():
        raise ValueError("an experiment needs a protocol (what it tests and how) and code")
    g = _store(store)
    exp_id = "exp-" + hashlib.sha256((protocol + "\0" + code).encode("utf-8")).hexdigest()[:16]
    g.put("experiment", exp_id, {"protocol": protocol, "code": code, "status": "defined"},
          created_by=BY, project_id=project_id)
    if hypothesis_id:
        g.get("hypothesis", hypothesis_id)  # must exist
        g.link(("hypothesis", hypothesis_id), "tested_by", ("experiment", exp_id), created_by=BY)
    status = compute_local.run_job(code, timeout_s=timeout_s, memory_mb=memory_mb, wait_s=wait_s,
                                   label=f"experiment {exp_id}")
    run_id = _record_run(g, exp_id, status, project_id)
    return {"experiment_id": exp_id, "run_id": run_id, "job_id": status["id"], "state": status["state"],
            "job": status}


def refresh_run(run_id: str, store: GraphStore | None = None) -> dict[str, Any]:
    g = _store(store)
    exp = g.related(("experiment_run", run_id), "run_of")
    if not exp:
        raise ValueError(f"no experiment run {run_id}")
    job_id = run_id[len("run-"):]
    matches = [j for j in compute_local.list_jobs(200) if j["id"].startswith(job_id)]
    if not matches:
        raise ValueError(f"the job behind {run_id} is no longer on this Mac")
    status = compute_local.job_status(matches[0]["id"])
    _record_run(g, exp[0].id, status, exp[0].project_id)
    return {"run_id": run_id, "state": status["state"], "job": status}


def replicate(run_id: str, *, wait_s: float = 120.0, store: GraphStore | None = None) -> dict[str, Any]:
    """RES-18: run the same experiment again and compare every metric."""
    g = _store(store)
    exps = g.related(("experiment_run", run_id), "run_of")
    if not exps:
        raise ValueError(f"no experiment run {run_id}")
    exp = exps[0]
    original = {m.body["name"]: m.body["value"] for m in g.related(("experiment_run", run_id), "produced")
                if m.type == "metric"}
    status = compute_local.run_job(exp.body["code"], wait_s=wait_s, label=f"replication of {run_id}")
    new_run = _record_run(g, exp.id, status, exp.project_id)
    g.link(("experiment_run", new_run), "replicates", ("experiment_run", run_id), created_by=BY)
    if status["state"] in ("queued", "running"):
        return {"run_id": new_run, "state": status["state"], "verdict": "still running"}
    again = {k: v for k, v in (status.get("metrics") or {}).items() if not k.startswith("_")}
    differs = {k: (original.get(k), again.get(k)) for k in sorted(set(original) | set(again))
               if original.get(k) != again.get(k)}
    if status["state"] != "succeeded":
        verdict = f"the replication {status['state']}"
    elif not original and not again:
        verdict = "no metrics to compare (write out/metrics.json to make a run checkable)"
    elif differs:
        verdict = "did NOT replicate: " + "; ".join(f"{k}: {a} then {b}" for k, (a, b) in differs.items())
    else:
        verdict = f"replicated: all {len(original)} metric(s) identical"
    return {"run_id": new_run, "state": status["state"], "verdict": verdict, "differences": differs}


def describe_run(run_id: str, store: GraphStore | None = None) -> str:
    g = _store(store)
    run = g.get("experiment_run", run_id)
    exp = g.related(("experiment_run", run_id), "run_of")
    produced = g.related(("experiment_run", run_id), "produced")
    lines = [f"{run_id}: {run.body['status']} (exit {run.body.get('exit_code')}) on {run.body.get('node')}, "
             f"environment {str(run.body.get('environment_hash', ''))[:12]}"]
    if exp:
        lines.append(f"experiment {exp[0].id}: {exp[0].body['protocol'][:300]}")
    for obj in produced:
        if obj.type == "metric":
            lines.append(f"  metric {obj.body['name']} = {json.dumps(obj.body['value'])}")
        elif obj.type == "result":
            lines.append(f"  result: {obj.body['summary']}")
    if run.body.get("logs"):
        lines.append("logs:\n" + run.body["logs"][-1500:])
    return "\n".join(lines)
