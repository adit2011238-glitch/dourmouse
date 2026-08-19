"""ATLAS strategy proposals — LLM-authored strategy code, human-gated (v8.16).

The execution-safety model decided for this feature (2026-08-18): a trading
idea — whether typed by the user in chat or written by the autonomous idea
generator (see atlas_generator.py, not yet built) — becomes REAL PYTHON CODE
via an LLM, and that code NEVER runs until a human explicitly approves it in
the UI. Approval is the only path from "written" to "executable". This is
one unified model for both idea sources, chosen over a constrained-parameter-
template alternative specifically so a genuinely novel rule (not expressible
in a fixed vocabulary) can still be tested — at the cost of needing a real
review gate, which this module treats as load-bearing, not decorative.

Two independent layers enforce that gate, on purpose (defense in depth, the
same shape as the v8.15 scheduler-bypass fix):

1. Static, at proposal-creation time: `_static_safety_check` AST-parses the
   generated code and refuses anything outside a small allowed-import list
   and a required `run(load, params)` top-level function shape. A proposal
   that fails this is marked "rejected_unsafe" and can never be approved —
   it doesn't even reach the human review queue. This is a cheap pre-filter,
   not the real boundary (a determined author can write dangerous PURE
   PYTHON that imports nothing dangerous — e.g. a deliberate infinite loop,
   or CPU/memory exhaustion) — layer 2 is the real one.
2. Kernel-enforced, at run time: execution goes through
   dourmouse.sandbox.run_sandboxed (the same macOS Seatbelt sandbox
   run_command already uses) — network denied unless the run explicitly
   targets the desktop engine, writes confined to the workspace, credential
   dirs unreadable, hard timeout. On a system without sandbox-exec this
   honestly refuses to run (Rule 2.2) — it never silently executes
   unsandboxed, approval or not.

Strategy code contract: the generated module must define
``def run(load, params: dict) -> dict``, where ``load(key)`` fetches one
data series (same key format as atlas-strategy-lab's engine_api.py:
"fx:EURUSD:d1", "commodity:GC", "fundamental:X", "events", "rate:USD" — see
that file's ``_keys_with_rows()`` for the full set) and the return dict must
carry at least ``mean_return``, ``std_dev``, ``n_obs`` (everything else —
sharpe, win_rate, max_drawdown, t_stat — is optional; computed downstream
from these three if the strategy didn't provide them, never fabricated if
the strategy also omitted the inputs needed to compute them).

Execution targets:
- "desktop": calls the atlas-strategy-lab engine_api.py service (real data,
  real market history). Requires a NEW endpoint on that service to accept
  reviewed-but-dynamic code — engine_api.py's current three functions are
  hardcoded by design; extending it is a deliberate, separate decision
  because that repo has another real contributor (see the ENGINE_TARGET
  docstring below) — NOT wired until that's confirmed.
- "local": same sandboxed harness, pointed at ATLAS_DATA_PATH on this
  machine. Honestly NOT CONFIGURED if no local data registry is present —
  never substitutes different-shaped data from the Mac's separate ATLAS
  research repo (dispatch agents/atlas) silently; that repo's data layout
  is genuinely incompatible with the fx:/commodity:/fundamental: key
  convention, not just unconfigured.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dourmouse.sandbox import run_sandboxed, sandbox_available

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

NVIDIA_BASE_URL = os.environ.get("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
NVIDIA_MODEL = os.environ.get("NVIDIA_MODEL", "nvidia/llama-3.1-nemotron-ultra-253b-v1")

#: ENGINE_TARGET: "desktop" execution is a client for a NOT-YET-BUILT
#: engine_api.py endpoint (see module docstring — held pending confirmation,
#: that repo has a second real contributor). Calling execute() with
#: target="desktop" today returns an honest NOT CONFIGURED, not a guess.
DESKTOP_ENGINE_URL = os.environ.get("ATLAS_ENGINE_URL", "http://127.0.0.1:8790")
DESKTOP_ENGINE_TOKEN = os.environ.get("ATLAS_ENGINE_TOKEN", "")

_RUN_TIMEOUT_SECONDS = 90

#: Import allowlist for generated strategy code (layer 1 — see module
#: docstring; the real boundary is the sandbox at run time, this is a cheap
#: pre-filter that keeps obviously-wrong proposals out of the review queue).
_ALLOWED_IMPORTS = {"pandas", "numpy", "math", "statistics", "datetime", "json"}


def _workspace_root() -> Path:
    """Same convention as sandbox.py/general_roster.py (kept independent to
    avoid an import cycle with either)."""
    raw = os.environ.get("DOURMOUSE_WORKSPACE")
    root = Path(raw).expanduser() if raw else Path(__file__).resolve().parent.parent / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _store_path() -> Path:
    p = _workspace_root() / "atlas_lab"
    p.mkdir(parents=True, exist_ok=True)
    return p / "proposals.json"


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass
class BacktestRun:
    id: str = ""
    proposal_id: str = ""
    target: str = "local"  # "local" | "desktop"
    status: str = "running"  # running | done | failed
    metrics: dict[str, Any] = field(default_factory=dict)
    verdict: str = ""
    explanation: str = ""
    error: str = ""
    started_at: str = ""
    finished_at: str = ""


@dataclass
class StrategyProposal:
    id: str = ""
    source: str = "chat"  # "chat" | "generator"
    status: str = "pending"  # pending | approved | rejected | rejected_unsafe
    strategy_name: str = ""
    prompt: str = ""          # what the user typed, or the generator's own rationale
    explanation: str = ""     # plain-English "what this does and why it might work"
    code: str = ""            # the generated run(load, params) module source
    params: dict[str, Any] = field(default_factory=dict)
    safety_note: str = ""     # set when _static_safety_check refuses it
    reviewer_note: str = ""   # set on reject, optional
    created_at: str = ""
    decided_at: str = ""
    runs: list[str] = field(default_factory=list)  # BacktestRun ids


# --------------------------------------------------------------------------- #
# Persistence (flat JSON, same convention as workspace/tasks.json)
# --------------------------------------------------------------------------- #

_LOCK = threading.Lock()


def _load_all() -> dict[str, Any]:
    p = _store_path()
    if not p.is_file():
        return {"proposals": {}, "runs": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"proposals": {}, "runs": {}}
    data.setdefault("proposals", {})
    data.setdefault("runs", {})
    return data


def _save_all(data: dict[str, Any]) -> None:
    p = _store_path()
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.replace(p)  # atomic on POSIX — never leaves a half-written store


def _make_id(prefix: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{ts}_{os.urandom(3).hex()}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Layer 1 — static safety pre-filter (AST, before the review queue)
# --------------------------------------------------------------------------- #

def _static_safety_check(code: str) -> str:
    """Returns "" if the code is acceptable to even show a reviewer,
    otherwise a human-readable reason it was refused outright.

    Deliberately conservative: anything this can't confidently classify as
    safe is refused, not passed through. This is NOT the security boundary
    (see module docstring) — it exists so an obviously-hostile or malformed
    proposal never even reaches a human's approve/reject click.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"generated code does not parse: {exc}"

    has_run_def = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "run" and node.col_offset == 0:
            has_run_def = True
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = node.module if isinstance(node, ast.ImportFrom) else None
            names = [mod] if mod else [n.name for n in node.names]
            for n in names:
                top = (n or "").split(".")[0]
                if top not in _ALLOWED_IMPORTS:
                    return (
                        f"import of {n!r} is not in the allowed list "
                        f"({sorted(_ALLOWED_IMPORTS)}) — refused before review"
                    )
        # Dunder/reflection tricks that are the classic sandbox-escape
        # vectors in pure-Python restricted execution — refuse outright
        # rather than try to enumerate every escape (Rule 2.2: an incomplete
        # blocklist that looks complete is worse than an honest refusal).
        if isinstance(node, ast.Attribute) and node.attr in (
            "__globals__", "__builtins__", "__subclasses__", "__base__",
            "__bases__", "__mro__", "__import__", "__loader__",
        ):
            return f"use of {node.attr!r} is refused — not analyzable as safe"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in (
            "eval", "exec", "compile", "open", "__import__", "getattr", "setattr",
        ):
            return f"call to {node.func.id}() is refused — not permitted in strategy code"

    if not has_run_def:
        return "generated code has no top-level `def run(load, params):` — required entry point"
    return ""


# --------------------------------------------------------------------------- #
# LLM: idea -> strategy code
# --------------------------------------------------------------------------- #

_CODE_SYSTEM_PROMPT = """You are a quantitative strategy engineer. Given a trading idea in natural language, output ONE JSON object, no other text, with this exact schema:

{
  "strategy_name": "short descriptive name",
  "explanation": "2-4 sentences, plain English: what the strategy does and why it might work, for a non-expert",
  "params": {"pair": "EURUSD", "lookback": 20, "...": "any numeric/string params your code below reads from the params dict"},
  "code": "the full Python source of a module-level function named exactly `run`"
}

Strict rules for the "code" field:
- Define exactly one top-level function: def run(load, params):
- `load(key)` is provided by the caller — call it to get one pandas DataFrame of market data. Valid keys look like "fx:EURUSD:d1", "fx:EURUSD:h1", "commodity:GC", "fundamental:X", "events", "rate:USD". Use only pairs/keys implied by the user's idea; default to "fx:EURUSD:d1" if unspecified.
- `params` is the params dict from above — read your own thresholds/lookback/etc from it, do not hardcode magic numbers you already put in params.
- You may import ONLY: pandas, numpy, math, statistics, datetime, json. No other imports of any kind — no os, sys, subprocess, socket, requests, urllib, io, pickle, __import__, eval, exec, open. Code using anything else will be refused before a human ever sees it.
- Return a dict with AT LEAST these keys: "mean_return" (float, per-trade or per-period mean net return as a decimal, e.g. 0.002 for 0.2%), "std_dev" (float), "n_obs" (int, number of observations/trades the stats are computed from). Include "sharpe", "win_rate", "max_drawdown", "t_stat" too if you can compute them honestly from what you observed — omit any of these you cannot compute rather than inventing a plausible-looking number.
- If the data you need isn't available or a computation is undefined (e.g. n_obs=0), return {"mean_return": 0.0, "std_dev": 0.0, "n_obs": 0, "note": "explain why here"} rather than raising or fabricating.
- No network access, no file access, no subprocess — the sandbox denies these regardless, so don't rely on them.

Output ONLY the JSON object. The "code" value is a JSON string (escape newlines as \\n) containing valid, complete, self-contained Python.
"""


def _llm_chat(prompt: str, system: str) -> str:
    from openai import OpenAI

    api_key = os.environ.get("NVIDIA_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "NVIDIA_API_KEY is not set — the strategy-proposal LLM needs it (.env)."
        )
    client = OpenAI(api_key=api_key, base_url=NVIDIA_BASE_URL, timeout=90)
    response = client.chat.completions.create(
        model=NVIDIA_MODEL,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=4000,
    )
    return (response.choices[0].message.content or "").strip()


#: Live-observed (2026-08-18): a code-heavy structured response occasionally
#: comes back not-quite-valid JSON (an unescaped quote inside the "code"
#: string, or truncation on a longer strategy) even though the SAME prompt
#: usually parses fine — ordinary LLM structured-output variance, not a
#: deterministic bug in the parser (confirmed: re-running the exact prompt
#: that failed in the UI parsed clean on the next call). A retry is the
#: correct fix, not a cleverer parser — there is no parser that reliably
#: repairs an LLM's own occasional malformed output.
_CODEGEN_ATTEMPTS = 3


def _parse_llm_json(raw: str) -> dict[str, Any]:
    text = raw
    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in text:
        text = text.split("```", 1)[1].split("```", 1)[0].strip()
    return json.loads(text)


def _generate_spec_once(prompt: str) -> dict[str, Any]:
    """One LLM attempt at idea -> strategy spec. Raises RuntimeError on a
    malformed response (bad JSON or a missing field) — the caller retries."""
    raw = _llm_chat(prompt, system=_CODE_SYSTEM_PROMPT)
    try:
        spec = _parse_llm_json(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"LLM did not return valid JSON. Raw:\n{raw[:1200]}") from exc

    for k in ("strategy_name", "explanation", "code"):
        if k not in spec or not str(spec[k]).strip():
            raise RuntimeError(f"LLM output missing required field {k!r}. Raw:\n{raw[:1200]}")
    return spec


def propose_from_idea(prompt: str, source: str = "chat") -> dict[str, Any]:
    """The one entry point for turning an idea into a queued proposal —
    used both by the chat endpoint and (later) the autonomous generator, so
    both sources are gated by literally the same code path."""
    if not prompt.strip():
        raise ValueError("empty idea prompt")

    # Retried, not re-parsed harder: a malformed response (unescaped quote,
    # truncation) is ordinary LLM structured-output variance — re-asking
    # the SAME prompt routinely succeeds on the next attempt (confirmed
    # live). There is no parser fix for a response the model didn't
    # actually produce correctly.
    spec = None
    last_exc: RuntimeError | None = None
    for attempt in range(_CODEGEN_ATTEMPTS):
        try:
            spec = _generate_spec_once(prompt)
            break
        except RuntimeError as exc:
            last_exc = exc
    if spec is None:
        raise RuntimeError(
            f"LLM produced a malformed response {_CODEGEN_ATTEMPTS} times in a row "
            f"— giving up rather than retrying forever. Last error: {last_exc}"
        )

    code = spec["code"]
    safety_note = _static_safety_check(code)

    proposal = StrategyProposal(
        id=_make_id("prop"),
        source=source,
        status="rejected_unsafe" if safety_note else "pending",
        strategy_name=str(spec["strategy_name"])[:120],
        prompt=prompt,
        explanation=str(spec["explanation"])[:2000],
        code=code,
        params=spec.get("params", {}) if isinstance(spec.get("params"), dict) else {},
        safety_note=safety_note,
        created_at=_now(),
        decided_at=_now() if safety_note else "",
    )

    with _LOCK:
        data = _load_all()
        data["proposals"][proposal.id] = asdict(proposal)
        _save_all(data)

    return asdict(proposal)


# --------------------------------------------------------------------------- #
# Review queue
# --------------------------------------------------------------------------- #

def list_proposals(status: str | None = None) -> list[dict]:
    data = _load_all()
    items = list(data["proposals"].values())
    if status:
        items = [p for p in items if p.get("status") == status]
    items.sort(key=lambda p: p.get("created_at", ""), reverse=True)
    return items


def get_proposal(proposal_id: str) -> dict | None:
    data = _load_all()
    return data["proposals"].get(proposal_id)


def reject_proposal(proposal_id: str, reason: str = "") -> dict | None:
    with _LOCK:
        data = _load_all()
        p = data["proposals"].get(proposal_id)
        if p is None:
            return None
        if p["status"] not in ("pending",):
            return p  # already decided — don't overwrite a prior decision
        p["status"] = "rejected"
        p["reviewer_note"] = reason
        p["decided_at"] = _now()
        _save_all(data)
        return p


def approve_and_run(proposal_id: str, target: str = "local") -> dict[str, Any]:
    """Approve a pending proposal AND execute it — approval with no run
    would leave the human with nothing to look at, and the spec is that
    NOTHING runs before approval, not that approval and running are two
    separate clicks. Returns the BacktestRun record."""
    with _LOCK:
        data = _load_all()
        p = data["proposals"].get(proposal_id)
        if p is None:
            raise KeyError(f"no such proposal: {proposal_id}")
        if p["status"] == "rejected_unsafe":
            raise ValueError("refused: this proposal failed the safety pre-filter and cannot be approved")
        if p["status"] not in ("pending", "approved"):
            raise ValueError(f"proposal is {p['status']!r}, not approvable")
        p["status"] = "approved"
        if not p.get("decided_at"):
            p["decided_at"] = _now()
        _save_all(data)
        code, params = p["code"], p["params"]

    run = _execute(proposal_id, code, params, target)

    with _LOCK:
        data = _load_all()
        data["runs"][run["id"]] = run
        data["proposals"].setdefault(proposal_id, {}).setdefault("runs", [])
        if proposal_id in data["proposals"]:
            data["proposals"][proposal_id]["runs"].append(run["id"])
        _save_all(data)

    return run


def approve_and_run_async(proposal_id: str, target: str = "local") -> dict[str, Any]:
    """HTTP-facing wrapper around approve_and_run: marking approved + the
    handful of store writes are fast (do them inline, synchronously, so a
    caller sees an honest error immediately for a bad id/state), but actual
    execution can take up to _RUN_TIMEOUT_SECONDS (sandboxed subprocess) —
    that part runs in a background thread. Returns a "running" placeholder
    run record immediately; poll get_run/list_runs for the real result.
    Same approve-then-run contract as approve_and_run — this is a latency
    wrapper, not a different gate."""
    with _LOCK:
        data = _load_all()
        p = data["proposals"].get(proposal_id)
        if p is None:
            raise KeyError(f"no such proposal: {proposal_id}")
        if p["status"] == "rejected_unsafe":
            raise ValueError("refused: this proposal failed the safety pre-filter and cannot be approved")
        if p["status"] not in ("pending", "approved"):
            raise ValueError(f"proposal is {p['status']!r}, not approvable")
        p["status"] = "approved"
        if not p.get("decided_at"):
            p["decided_at"] = _now()
        code, params = p["code"], p["params"]

        placeholder = asdict(BacktestRun(
            id=_make_id("run"), proposal_id=proposal_id, target=target,
            status="running", started_at=_now(),
        ))
        data["runs"][placeholder["id"]] = placeholder
        p.setdefault("runs", []).append(placeholder["id"])
        _save_all(data)

    def _worker() -> None:
        result = _execute(proposal_id, code, params, target, run_id=placeholder["id"])
        with _LOCK:
            data2 = _load_all()
            data2["runs"][placeholder["id"]] = result
            _save_all(data2)

    threading.Thread(target=_worker, daemon=True, name=f"atlas-run-{placeholder['id']}").start()
    return placeholder


def get_run(run_id: str) -> dict | None:
    data = _load_all()
    return data["runs"].get(run_id)


def list_runs(proposal_id: str | None = None) -> list[dict]:
    data = _load_all()
    items = list(data["runs"].values())
    if proposal_id:
        items = [r for r in items if r.get("proposal_id") == proposal_id]
    items.sort(key=lambda r: r.get("started_at", ""), reverse=True)
    return items


# --------------------------------------------------------------------------- #
# Layer 2 — sandboxed execution
# --------------------------------------------------------------------------- #

_HARNESS_TEMPLATE = '''
import json, sys
sys.path.insert(0, {strategy_dir!r})
import strategy_module as _sm

def _load(key):
    {loader_body}

try:
    result = _sm.run(_load, {params_json})
    if not isinstance(result, dict):
        raise TypeError(f"run() returned {{type(result).__name__}}, expected dict")
    print("===RESULT===")
    print(json.dumps(result, default=str))
except Exception as exc:
    print("===ERROR===")
    print(str(exc))
'''

_LOCAL_LOADER_BODY = """raise RuntimeError(
        "local data registry not configured on this machine — "
        "ATLAS_DATA_PATH is not set to a data_registry.py root. "
        "This is an honest NOT CONFIGURED, not a fabricated result."
    )"""

_DESKTOP_LOADER_BODY = """
    import urllib.request, json as _json
    req = urllib.request.Request(
        {desktop_url!r} + "/api/data/" + key,
        headers={{"X-Engine-Token": {desktop_token!r}}} if {desktop_token!r} else {{}},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = _json.loads(resp.read().decode())
    if not payload.get("ok"):
        raise RuntimeError(f"desktop engine refused key {{key!r}}: {{payload.get('error')}}")
    import pandas as pd
    return pd.DataFrame(payload["sample"])
"""


def _execute(
    proposal_id: str, code: str, params: dict, target: str, run_id: str | None = None,
) -> dict[str, Any]:
    run = BacktestRun(
        id=run_id or _make_id("run"),
        proposal_id=proposal_id,
        target=target,
        started_at=_now(),
    )

    if target == "desktop":
        run.status = "failed"
        run.error = (
            "NOT CONFIGURED: desktop execution needs a new engine_api.py endpoint "
            "that doesn't exist yet (held pending — that repo has another real "
            "contributor, see atlas_proposals.py module docstring). Use "
            "target='local', or ask to have the desktop endpoint built."
        )
        run.finished_at = _now()
        return asdict(run)

    if not sandbox_available():
        run.status = "failed"
        run.error = (
            "NOT CONFIGURED: sandboxed execution requires macOS sandbox-exec, "
            "unavailable on this system. Refusing to run generated code "
            "unsandboxed — a silent fallback would defeat the whole point of "
            "the review gate."
        )
        run.finished_at = _now()
        return asdict(run)

    work_dir = _workspace_root() / "atlas_lab" / "tmp" / run.id
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "strategy_module.py").write_text(code, encoding="utf-8")

    loader_body = _LOCAL_LOADER_BODY
    harness = _HARNESS_TEMPLATE.format(
        strategy_dir=str(work_dir),
        loader_body=loader_body,
        params_json=json.dumps(params),
    )
    harness_path = work_dir / "harness.py"
    harness_path.write_text(harness, encoding="utf-8")

    # shlex.quote is not optional here: this repo's own volume name has a
    # trailing space ("/Volumes/ATLAS /..."), and run_sandboxed's `command`
    # goes through `/bin/sh -c` — an unquoted path silently word-splits
    # into "/bin/sh: /Volumes/ATLAS: No such file or directory" (exit 127),
    # caught live by this module's own test suite.
    output = run_sandboxed(
        f"{shlex.quote(sys.executable)} {shlex.quote(str(harness_path))}",
        cwd=str(work_dir),
        timeout=_RUN_TIMEOUT_SECONDS,
        allow_network=False,  # local target never needs network
    )

    metrics, err = _parse_harness_output(output)
    run.finished_at = _now()
    if err:
        run.status = "failed"
        run.error = err
    else:
        run.status = "done"
        run.metrics = metrics
        run.verdict = _verdict_from_metrics(metrics)

    if run.status == "done":
        try:
            run.explanation = _explain_run(proposal_id, run)
        except RuntimeError as exc:
            run.explanation = f"(explanation unavailable: {exc})"

    return asdict(run)


def _parse_harness_output(output: str) -> tuple[dict, str]:
    if "===RESULT===" in output:
        tail = output.split("===RESULT===", 1)[1].strip()
        try:
            return json.loads(tail.splitlines()[0] if tail.splitlines() else tail), ""
        except (json.JSONDecodeError, IndexError):
            return {}, f"sandbox returned unparseable result: {tail[:500]}"
    if "===ERROR===" in output:
        tail = output.split("===ERROR===", 1)[1].strip()
        return {}, f"strategy code raised: {tail[:500]}"
    return {}, f"sandbox produced no recognizable output: {output[:500]}"


def _verdict_from_metrics(m: dict) -> str:
    n = m.get("n_obs", 0) or 0
    if not n:
        return "NO DATA"
    sharpe = m.get("sharpe")
    mean_ret = m.get("mean_return")
    if sharpe is not None:
        if sharpe > 0.5:
            return "CANDIDATE"
        if sharpe > 0:
            return "HOLD"
        return "FAILED"
    if mean_ret is not None:
        return "CANDIDATE" if mean_ret > 0 else "FAILED"
    return "INCONCLUSIVE"


def _explain_run(proposal_id: str, run: BacktestRun) -> str:
    """Post-hoc plain-English explanation of the RESULT (why it worked or
    failed) — separate from the proposal's pre-hoc explanation of what the
    idea WAS. Honest: given only the numbers actually produced, never asked
    to embellish."""
    prop = get_proposal(proposal_id) or {}
    prompt = (
        f"A trading strategy called {prop.get('strategy_name', '?')!r} was just "
        f"backtested. Original idea: {prop.get('prompt', '?')}\n"
        f"Result metrics: {json.dumps(run.metrics)}\n"
        f"Verdict: {run.verdict}\n\n"
        "In 2-3 plain-English sentences, explain why this result makes sense "
        "given the metrics (or why it doesn't/is inconclusive). Do not invent "
        "numbers not given above. If n_obs is 0 or very low, say so plainly "
        "rather than reading meaning into noise."
    )
    return _llm_chat(prompt, system="You are a quant analyst explaining a backtest result honestly and concisely.")
