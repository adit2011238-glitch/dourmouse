"""This Mac's compute workspace (finding #113).

The owner moved everything onto this Mac (2026-09-24: "only for mac for
everything") and set a large-cloud-only model policy. The ``compute``
agent used to offload inference to a LAN Dell running Qwen3 1.7B, which
broke both rules; it now runs Python work (simulations, experiments,
number crunching) on this Mac through the same job runner the node server
uses (finding #098): each job in its own folder under the workspace, inside
a macOS Seatbelt sandbox (reads allowlisted, writes only in that folder, no
network; a job is refused, never run unsandboxed, when sandbox-exec is
missing), an allowlist environment, CPU, file, process and disk limits, a
timeout that kills the whole process group, a resident-memory limit, captured
stdout/stderr, ``out/metrics.json`` and artifacts hashed, and the
interpreter's environment hash recorded so a result can be reproduced.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any

from dourmouse.config import workspace_dir

_runner: Any = None
_lock = threading.Lock()


def _no_inputs(sha: str) -> bytes:
    raise LookupError("local jobs take no stored inputs; read files from the job's own folder instead")


def runner() -> Any:
    """One job runner per process, rooted in the workspace."""
    global _runner
    with _lock:
        if _runner is None:
            from dourmouse.nodes.node_server import JobRunner

            _runner = JobRunner(workspace_dir() / "compute" / "jobs", sys.executable, _no_inputs)
        return _runner


def run_job(code: str, *, timeout_s: int = 600, memory_mb: int = 4096, wait_s: float = 60.0,
            label: str = "") -> dict[str, Any]:
    """Submit and wait up to ``wait_s`` for it to finish; a longer job keeps
    running and is checked later with job_status."""
    r = runner()
    status: dict[str, Any] = r.submit({"code": code, "timeout_s": timeout_s, "memory_mb": memory_mb, "label": label})
    deadline = time.monotonic() + wait_s
    while status.get("state") in ("queued", "running") and time.monotonic() < deadline:
        time.sleep(0.1)
        status = r.status(status["id"])
    return status


def job_status(job_id: str) -> dict[str, Any]:
    if not job_id or not all(c in "0123456789abcdef" for c in job_id):
        raise ValueError("not a job id")
    result: dict[str, Any] = runner().status(job_id)
    return result


def list_jobs(limit: int = 20) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = runner().list(limit)
    return jobs


def environment() -> dict[str, Any]:
    env: dict[str, Any] = runner().environment()
    return env


def describe(status: dict[str, Any]) -> str:
    lines = [f"job {status['id']}: {status['state']}"
             + (f" (exit {status['exit_code']})" if "exit_code" in status else "")]
    if status.get("error"):
        lines.append("error: " + status["error"])
    if status.get("finished_at") and status.get("started_at"):
        lines.append(f"ran {status['finished_at'] - status['started_at']:.1f}s; memory limit {status.get('memory_limit', '?')}")
    if status.get("metrics"):
        lines.append("metrics: " + ", ".join(f"{k}={v}" for k, v in list(status["metrics"].items())[:20]))
    if status.get("artifacts"):
        lines.append("artifacts: " + ", ".join(status["artifacts"]))
    if status.get("stdout_tail"):
        lines.append("stdout:\n" + status["stdout_tail"])
    if status.get("stderr_tail"):
        lines.append("stderr:\n" + status["stderr_tail"])
    if status.get("state") in ("queued", "running"):
        lines.append("Still running: check it with compute_job_status.")
    env = status.get("environment") or {}
    if env.get("sha256"):
        lines.append(f"environment {env.get('python', '?')} on {env.get('platform', '?')}, hash {env['sha256'][:12]}")
    return "\n".join(lines)


def compute_status() -> dict[str, Any]:
    """What /api/server and the connections list report: the compute node
    is this Mac. Reads the jobs folder only, so a status poll never starts
    the runner or its environment probe."""
    import json

    root = workspace_dir() / "compute" / "jobs"
    states: list[str] = []
    if root.is_dir():
        for f in root.glob("*/status.json"):
            try:
                states.append(str(json.loads(f.read_text(encoding="utf-8")).get("state")))
            except (OSError, ValueError):
                continue
    running = sum(1 for s in states if s in ("queued", "running"))
    detail = f"Python {sys.version.split()[0]}, {len(states)} job(s)" + (f", {running} running" if running else "")
    return {"online": True, "node": "THIS MAC", "latency_ms": None, "model": None, "kind": "local compute",
            "detail": detail, "jobs": len(states), "running": running}
