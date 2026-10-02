#!/usr/bin/env python3
"""Tool-use benchmark harness (phase G0): does the model call the right tools?

    .venv/bin/python scripts/bench/run.py                    # stub mode (default), no network
    .venv/bin/python scripts/bench/run.py --stub-policy mixed
    .venv/bin/python scripts/bench/run.py --validate         # check tasks against the tool registry
    .venv/bin/python scripts/bench/run.py --real --port 18791 [--cookie "dm_session=..."]

Tasks live in scripts/bench/tasks.json. Each has an id, category, prompt, the
tool names expected, a pass rule and optional argument checks (see
score_task). Tool names are validated against the real registry
(dourmouse.general_roster.build_general_registry), never invented.

STUB MODE. The repo has no server-side stub-chat mode: scripts/os_shell_check.py
--stub-chat is a Playwright route that fakes /api/chat inside a browser, and
does not exist in dourmouse.webui. So stub mode here starts a tiny in-process
HTTP server on an ephemeral 127.0.0.1 port that speaks the same SSE protocol
as /api/chat (events: tool_use, tool_result, assistant_delta, done) and
plays a scripted policy. It proves the harness, scoring and report work. It
says NOTHING about a real model. The default "oracle" policy passes every task
by construction; "decoy" and "mixed" exist to prove the failure report.

REAL MODE talks to a running Dourmouse server over POST /api/chat. It needs
the explicit --real flag and an explicit non-owner --port (never 8765, 9333
or 9334). Spend: every task is a real model turn. Tools that run without
confirmation really execute, so prompts use /tmp/dm_bench paths only. The
runner never answers a confirmation request: it records the gated tool as
called and drops the connection, so confirmation-gated actions are never
approved by the bench.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
TASKS_FILE = HERE / "tasks.json"
OWNER_PORTS = (8765, 9333, 9334)
DEFAULT_REAL_PORT = 18791
PASS_RULES = ("all_of", "any_of", "none_of")
OPS = ("contains", "equals", "regex")
REQUIRED_FIELDS = ("id", "category", "prompt", "expected_tools", "pass_rule")


# --------------------------------------------------------------------------- #
# Task data
# --------------------------------------------------------------------------- #

def load_tasks(path: Path = TASKS_FILE) -> list[dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return list(data["tasks"] if isinstance(data, dict) else data)


def registry_tool_names() -> set[str]:
    sys.path.insert(0, str(ROOT))
    from dourmouse.general_roster import build_general_registry

    registry = build_general_registry()
    return {tool.name for sub in registry.all_subagents() for tool in sub.tools}


def validate_tasks(tasks: list[dict[str, Any]], known_tools: set[str]) -> list[str]:
    """Return a list of problems; empty means the task file is sound."""
    problems: list[str] = []
    seen: set[str] = set()
    for task in tasks:
        tid = task.get("id", "<no id>")
        for field in REQUIRED_FIELDS:
            if field not in task:
                problems.append(f"{tid}: missing field {field}")
        if tid in seen:
            problems.append(f"{tid}: duplicate id")
        seen.add(tid)
        rule = task.get("pass_rule")
        if rule not in PASS_RULES:
            problems.append(f"{tid}: unknown pass_rule {rule!r}")
        expected = task.get("expected_tools", [])
        if rule in ("all_of", "any_of") and not expected:
            problems.append(f"{tid}: {rule} needs expected_tools")
        if rule == "none_of" and not task.get("forbidden_tools"):
            problems.append(f"{tid}: none_of needs forbidden_tools")
        for name in list(expected) + list(task.get("forbidden_tools", [])):
            if name not in known_tools:
                problems.append(f"{tid}: tool {name!r} is not in the registry")
        for check in task.get("arg_checks", []):
            if check.get("op") not in OPS:
                problems.append(f"{tid}: bad arg check op {check.get('op')!r}")
            if check.get("tool") not in known_tools:
                problems.append(f"{tid}: arg check tool {check.get('tool')!r} is not in the registry")
            if check.get("op") == "regex":
                try:
                    re.compile(str(check.get("value")))
                except re.error as exc:
                    problems.append(f"{tid}: bad regex {check.get('value')!r}: {exc}")
    return problems


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #

def _parse_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _check_one(check: dict[str, Any], args: dict[str, Any]) -> bool:
    if check["key"] not in args:
        return False
    actual, want = args[check["key"]], check["value"]
    if check["op"] == "equals":
        return actual == want or str(actual) == str(want)
    if check["op"] == "contains":
        return str(want).lower() in str(actual).lower()
    return re.search(str(want), str(actual)) is not None


def score_task(task: dict[str, Any], calls: list[dict[str, Any]]) -> dict[str, Any]:
    """Score one task. ``calls`` is [{"name": str, "args": dict}, ...] in order.

    Rules: all_of = every expected tool was called; any_of = at least one was;
    none_of = pass when none of forbidden_tools was called (refusal cases; the
    model may call anything else or nothing). forbidden_tools is enforced for
    every rule. An arg check applies only to a tool that was called; at least
    one call of that tool must satisfy it, otherwise the task fails.
    """
    called = [c["name"] for c in calls]
    expected = list(task.get("expected_tools", []))
    forbidden = [n for n in task.get("forbidden_tools", []) if n in called]
    reasons: list[str] = []
    rule = task["pass_rule"]
    if rule == "all_of":
        missing = [n for n in expected if n not in called]
        if missing:
            reasons.append("missing " + ", ".join(missing))
    elif rule == "any_of":
        if not any(n in called for n in expected):
            reasons.append("none of " + ", ".join(expected) + " was called")
    if forbidden:
        reasons.append("called forbidden " + ", ".join(forbidden))
    for check in task.get("arg_checks", []):
        same = [c for c in calls if c["name"] == check["tool"]]
        if not same:
            continue
        if not any(_check_one(check, c.get("args") or {}) for c in same):
            reasons.append(f"{check['tool']}.{check['key']} failed {check['op']} {check['value']!r}")
    return {"passed": not reasons, "reasons": reasons, "called": called}


# --------------------------------------------------------------------------- #
# Stub server (same SSE shape as /api/chat; never a model answer)
# --------------------------------------------------------------------------- #

def _example_value(check: dict[str, Any]) -> Any:
    value = check["value"]
    if check["op"] != "regex":
        return value
    text = re.sub(r"^\(\?i\)", "", str(value))
    text = text.split("|")[0].replace("^", "").replace("$", "")
    return text or "x"


def oracle_calls(task: dict[str, Any]) -> list[dict[str, Any]]:
    """What a perfect model would call, built from the task's own checks."""
    names = list(task.get("expected_tools", []))
    if task["pass_rule"] == "any_of":
        names = names[:1]
    calls = []
    for name in names:
        args = {c["key"]: _example_value(c) for c in task.get("arg_checks", []) if c["tool"] == name}
        calls.append({"name": name, "args": args})
    return calls


def decoy_calls(task: dict[str, Any]) -> list[dict[str, Any]]:
    """A deliberately wrong model: it reaches for the first forbidden tool on
    refusal cases, otherwise for web_search."""
    if task["pass_rule"] == "none_of":
        return [{"name": task["forbidden_tools"][0], "args": {}}]
    return [{"name": "web_search", "args": {"query": task["prompt"][:40]}}]


def stub_calls_for(task: dict[str, Any], policy: str, index: int) -> list[dict[str, Any]]:
    if policy == "oracle":
        return oracle_calls(task)
    if policy == "decoy":
        return decoy_calls(task)
    if policy == "mixed":
        return decoy_calls(task) if index % 5 == 4 else oracle_calls(task)
    raise ValueError(f"unknown stub policy {policy}")


def make_stub_server(tasks: list[dict[str, Any]], policy: str) -> ThreadingHTTPServer:
    by_prompt = {t["prompt"]: (i, t) for i, t in enumerate(tasks)}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:  # silence
            pass

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/api/chat":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            hit = by_prompt.get((body.get("prompt") or "").strip())
            calls = stub_calls_for(hit[1], policy, hit[0]) if hit else []
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            events: list[dict[str, Any]] = [{"type": "brain", "model": "stub/model"}]
            for call in calls:
                events.append({"type": "tool_use", "name": call["name"], "raw_arguments": json.dumps(call["args"])})
                events.append({"type": "tool_result", "name": call["name"], "text": "stub result"})
            events.append({"type": "done", "final_text": "STUB, not a model answer."})
            for ev in events:
                self.wfile.write(("data: " + json.dumps(ev) + "\n\n").encode())
            self.wfile.flush()

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #

def ask(base_url: str, task: dict[str, Any], timeout: float, cookie: str = "") -> dict[str, Any]:
    """POST one prompt to /api/chat and collect tool_use events.

    Stops reading at done, error or confirmation_requested (a gated tool has
    been chosen; the bench never approves it). Returns calls, final text and
    an error string if the transport failed.
    """
    payload = json.dumps({"prompt": task["prompt"], "tab_id": "bench-" + task["id"]}).encode()
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(base_url + "/api/chat", data=payload, headers=headers, method="POST")
    calls: list[dict[str, Any]] = []
    final, error = "", ""
    deadline = time.monotonic() + timeout
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                if time.monotonic() > deadline:
                    error = "timeout"
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:].strip())
                except ValueError:
                    continue
                kind = ev.get("type")
                if kind == "tool_use":
                    calls.append({"name": ev.get("name", ""), "args": _parse_args(ev.get("raw_arguments"))})
                elif kind == "confirmation_requested":
                    break
                elif kind == "error":
                    error = str(ev.get("message", "error"))
                    break
                elif kind == "done":
                    final = str(ev.get("final_text", ""))
                    break
    except (urllib.error.URLError, OSError, ValueError) as exc:
        error = f"{type(exc).__name__}: {exc}"
    return {"calls": calls, "final_text": final, "error": error}


# --------------------------------------------------------------------------- #
# Run and report
# --------------------------------------------------------------------------- #

def run_tasks(tasks: list[dict[str, Any]], base_url: str, timeout: float, cookie: str = "") -> list[dict[str, Any]]:
    results = []
    for task in tasks:
        started = time.monotonic()
        got = ask(base_url, task, timeout, cookie)
        scored = score_task(task, got["calls"])
        if got["error"] and not got["calls"]:
            scored = {**scored, "passed": False, "reasons": scored["reasons"] + ["transport error: " + got["error"]]}
        results.append({
            "id": task["id"], "category": task["category"], "passed": scored["passed"],
            "reasons": scored["reasons"], "called": scored["called"],
            "expected": task["expected_tools"], "forbidden": task.get("forbidden_tools", []),
            "error": got["error"], "seconds": round(time.monotonic() - started, 3),
        })
    return results


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    cats: "OrderedDict[str, list[bool]]" = OrderedDict()
    for r in results:
        cats.setdefault(r["category"], []).append(r["passed"])
    total, passed = len(results), sum(1 for r in results if r["passed"])
    return {
        "total": total, "passed": passed, "pass_rate": round(passed / total, 4) if total else 0.0,
        "by_category": {c: {"total": len(v), "passed": sum(v), "pass_rate": round(sum(v) / len(v), 4)} for c, v in cats.items()},
    }


def format_report(mode: str, summary: dict[str, Any], results: list[dict[str, Any]]) -> str:
    lines = [f"Dourmouse tool-use bench ({mode})"]
    if mode.startswith("stub"):
        lines.append("STUB RUN: scripted policy, not a model. The number below measures the harness only.")
    lines.append(f"Overall: {summary['passed']}/{summary['total']} = {summary['pass_rate'] * 100:.1f}%")
    for cat, s in summary["by_category"].items():
        lines.append(f"  {cat:<10} {s['passed']}/{s['total']}  {s['pass_rate'] * 100:5.1f}%")
    failing = [r for r in results if not r["passed"]]
    if failing:
        lines.append("Failing tasks:")
        for r in failing:
            called = ", ".join(r["called"]) if r["called"] else "(no tool)"
            lines.append(f"  {r['id']}: expected {'/'.join(r['expected']) or 'a refusal'}, model called {called}; {'; '.join(r['reasons'])}")
    else:
        lines.append("Failing tasks: none")
    return "\n".join(lines)


def check_real_args(args: argparse.Namespace) -> str:
    """Return an error message if the --real invocation is unsafe, else ''."""
    if args.port in OWNER_PORTS:
        return f"refusing port {args.port}: 8765, 9333 and 9334 belong to the owner's live app"
    if not 1024 <= args.port <= 65535:
        return "port must be between 1024 and 65535"
    return ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--stub", action="store_true", help="default: in-process scripted stub, no cloud")
    mode.add_argument("--real", action="store_true", help="talk to a running server; real model spend")
    ap.add_argument("--port", type=int, default=DEFAULT_REAL_PORT, help="server port for --real (never 8765/9333/9334)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--cookie", default="", help="Cookie header value if the server needs a session")
    ap.add_argument("--stub-policy", choices=("oracle", "decoy", "mixed"), default="oracle")
    ap.add_argument("--tasks", default=str(TASKS_FILE))
    ap.add_argument("--only", default="", help="comma list of categories or task ids")
    ap.add_argument("--timeout", type=float, default=180.0, help="seconds per task")
    ap.add_argument("--out", default="", help="results JSON path (default scripts/bench/results/<mode>-<time>.json)")
    ap.add_argument("--validate", action="store_true", help="only check tasks against the registry")
    ap.add_argument("--min-pass", type=float, default=0.0, help="exit 1 if the overall pass rate is below this (0-1)")
    args = ap.parse_args(argv)

    tasks = load_tasks(Path(args.tasks))
    if args.validate:
        problems = validate_tasks(tasks, registry_tool_names())
        print("\n".join(problems) if problems else f"OK: {len(tasks)} tasks, every tool exists in the registry")
        return 1 if problems else 0
    if args.only:
        keep = {s.strip() for s in args.only.split(",") if s.strip()}
        tasks = [t for t in tasks if t["id"] in keep or t["category"] in keep]
    if not tasks:
        print("no tasks selected", file=sys.stderr)
        return 2

    server = None
    if args.real:
        err = check_real_args(args)
        if err:
            print(err, file=sys.stderr)
            return 2
        base, label = f"http://{args.host}:{args.port}", "real"
    else:
        server = make_stub_server(tasks, args.stub_policy)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base, label = f"http://127.0.0.1:{server.server_address[1]}", f"stub:{args.stub_policy}"
    try:
        results = run_tasks(tasks, base, args.timeout, args.cookie)
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()

    summary = summarize(results)
    print(format_report(label, summary, results))
    out = Path(args.out) if args.out else HERE / "results" / f"{label.replace(':', '-')}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"mode": label, "summary": summary, "results": results}, indent=1), encoding="utf-8")
    print(f"Results written to {out}")
    return 1 if summary["pass_rate"] < args.min_pass else 0


if __name__ == "__main__":
    raise SystemExit(main())
