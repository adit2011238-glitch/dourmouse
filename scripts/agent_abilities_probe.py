#!/usr/bin/env python3
"""Agent abilities probe (finding #189): what every agent can do, and whether
every agent can talk to every other agent.

Three phases, all offline and deterministic (a scripted stand-in for the model,
the REAL dispatch loop, registry, gates and nested runs), in a throwaway
workspace so nothing here touches the owner's data:

  A. Tool audit. Every tool of every agent: description, schema shape, who owns
     it, which ones ask the owner first.
  B. Cross-talk matrix. For every ordered pair (caller, target) the caller runs
     pinned to itself and asks the target through ask_agent. Checks the answer
     comes back, the owner is asked exactly when it should be (a conversation
     that reads text written by other people asking an agent that can act), and
     asking yourself is refused.
  C. Safe tool smoke. Every REGULAR tool that is read-only by name and needs no
     arguments is actually called once. Outcome classes only (the text is never
     saved, it can be personal): ok, honest refusal or not configured, raised,
     timed out.

What it does NOT prove: that a real model chooses well. That is the live sweep,
scripts/e2e_peer_sweep.py.

Usage:
  .venv/bin/python scripts/agent_abilities_probe.py [--out probe.json] [--report report.md] [--skip-smoke]
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sys
import tempfile
import time
import warnings
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Before any dourmouse import: a throwaway workspace, no model backend, no fast lane.
_WORKSPACE = tempfile.mkdtemp(prefix="dm-abilities-")
os.environ["DOURMOUSE_WORKSPACE"] = _WORKSPACE
os.environ["DOURMOUSE_LLM_BACKEND"] = "none"
os.environ["DOURMOUSE_FAST_LANE"] = "0"
warnings.filterwarnings("ignore")

from dourmouse import dispatch as dp  # noqa: E402
from dourmouse.dispatch import Permission  # noqa: E402
from dourmouse.general_roster import build_general_registry  # noqa: E402

_SAFE_NAME = re.compile(
    r"^(list_|get_)|_status$|_info$|_inventory$|_catalog$|_projects$|_threads$|_skills$|_changes$|_jobs$"
    r"|_environment$|_universe$|_known_devices$|_now_playing$|^(system_info|repo_map|check_connections|"
    r"email_identity_status|world_brief|world_pulse|device_wiki_status|security_status|read_agent_inbox)$"
)
_UNSAFE_NAME = re.compile(
    r"(send|delete|remove|trash|archive|write|create|add_|append|set_|update|apply|run_|execute|play|pause|"
    r"seek|open|close|move|resize|click|type|press|fill|submit|order|deploy|kill|quit|activate|dismiss|"
    r"incident|remember|publish|scan|dispatch|schedule|cancel|undo|share|download|signin|creds|record|stop|"
    r"start|train|generate|draft|complete|forget|clear|reset|install|enable|disable|lock|inbox|gmail|mail)"
)
_HONEST = ("ERROR", "REFUSED", "NOT CONFIGURED", "NOT SENT", "CONFIRMATION REQUIRED")
_SMOKE_TIMEOUT_S = 25.0


# ------------------------------------------------------------------ phase A


def _plain(text: str) -> str:
    """Agent descriptions quoted in the report: house style has no em or en dashes."""
    return text.replace("\u2014", "-").replace("\u2013", "-")


def audit_tools(registry) -> tuple[list[dict], list[dict]]:
    owners: dict[str, list[str]] = defaultdict(list)
    for sub in registry.all_subagents():
        for tool in sub.tools:
            owners[tool.name].append(sub.name)
    problems: list[dict] = []
    agents: list[dict] = []
    for sub in registry.all_subagents():
        gated = [t.name for t in sub.tools if t.permission is Permission.REQUIRES_CONFIRMATION]
        prohibited = [t.name for t in sub.tools if t.permission is Permission.PROHIBITED]
        shared = [t.name for t in sub.tools if len(owners[t.name]) > 1]
        for tool in sub.tools:
            params = tool.parameters or {}
            props = params.get("properties") or {}
            required = params.get("required") or []
            if len(" ".join((tool.description or "").split())) < 12:
                problems.append({"agent": sub.name, "tool": tool.name, "problem": "description missing or under 12 characters"})
            if params.get("type") != "object":
                problems.append({"agent": sub.name, "tool": tool.name, "problem": "parameters is not an object schema"})
            for name in required:
                if name not in props:
                    problems.append({"agent": sub.name, "tool": tool.name, "problem": f"required argument {name!r} has no property"})
            if tool.permission is Permission.REQUIRES_CONFIRMATION and tool.confirm_prompt is None:
                problems.append({"agent": sub.name, "tool": tool.name, "problem": "asks the owner but has no confirm_prompt"})
        agents.append({
            "agent": sub.name, "domain": sub.domain, "description": _plain(" ".join(sub.description.split())),
            "tools": [t.name for t in sub.tools], "gated": gated, "prohibited": prohibited, "shared": shared,
            "reads_other_peoples_text": sub.name in dp.UNTRUSTED_CONTENT_AGENTS,
            "open_to_untrusted_askers": sub.name in dp.PEER_ASK_OPEN_TARGETS,
        })
    return agents, problems


# ------------------------------------------------------------------ phase B


class _Scripted:
    """A stand-in for the model. The pinned caller asks one target; the asked
    agent answers in one line; the caller then finishes. Decided from the
    messages alone, so it needs no ordered queue and works at any depth."""

    def __init__(self, target: str) -> None:
        self.target = target
        self.calls = 0
        self.chat = type("_Chat", (), {"completions": self})()

    def create(self, **kwargs):
        self.calls += 1
        messages = kwargs["messages"]
        last = messages[-1]
        if last.get("role") == "tool":
            return _reply("done")
        user = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
        if "[ROUTING DIRECTIVE]" in str(user):
            return _reply(f"pong from {self.target}")
        call = dp._OllamaTc("probe-1", "ask_agent", json.dumps({"agent": self.target, "question": "ping"}))
        return dp._OllamaResponse(dp._OllamaMessage("", [call]))


def _reply(text: str):
    return dp._OllamaResponse(dp._OllamaMessage(text, None))


def ask_once(registry, caller: str, target: str) -> dict:
    prompts: list[str] = []
    client = _Scripted(target)
    messages = [
        {"role": "system", "content": dp.system_message(registry)},
        {"role": "user", "content": "probe"},
    ]
    started = time.perf_counter()
    report = dp.run_dispatch_messages(
        messages, registry, client=client, forced_agent=caller, max_turns=4,
        confirmation_gate=lambda p: prompts.append(p) or True,
    )
    results = [e["text"] for e in report["transcript"] if e["type"] == "tool_result" and e["name"] == "ask_agent"]
    text = results[0] if results else ""
    return {
        "caller": caller, "target": target, "asked_owner": len(prompts), "result_head": text[:60],
        "answered": text.startswith(f"ANSWER FROM {target}") and f"pong from {target}" in text,
        "refused": text.startswith("REFUSED"), "model_calls": client.calls,
        "ms": round((time.perf_counter() - started) * 1000, 1),
    }


def cross_talk(registry) -> dict:
    names = sorted(registry.subagent_names)
    rows: list[dict] = []
    for caller in names:
        for target in names:
            rows.append(ask_once(registry, caller, target))
    wrong: list[dict] = []
    for r in rows:
        caller, target = r["caller"], r["target"]
        if caller == target:
            if not r["refused"] or r["model_calls"] != 2:
                wrong.append({**r, "why": "asking yourself must be refused without spawning a run"})
            continue
        should_ask = caller in dp.UNTRUSTED_CONTENT_AGENTS and target not in dp.PEER_ASK_OPEN_TARGETS
        if not r["answered"]:
            wrong.append({**r, "why": "no answer came back"})
        elif r["asked_owner"] != (1 if should_ask else 0):
            wrong.append({**r, "why": f"owner asked {r['asked_owner']} times, expected {1 if should_ask else 0}"})
    pairs = [r for r in rows if r["caller"] != r["target"]]
    return {
        "agents": len(names), "pairs": len(pairs), "answered": sum(1 for r in pairs if r["answered"]),
        "asked_owner_first": sum(1 for r in pairs if r["asked_owner"]),
        "answered_free": sum(1 for r in pairs if r["answered"] and not r["asked_owner"]),
        "self_asks_refused": sum(1 for r in rows if r["caller"] == r["target"] and r["refused"]),
        "wrong": wrong, "slowest_ms": max(r["ms"] for r in rows),
        "per_caller": {c: sum(1 for r in pairs if r["caller"] == c and r["answered"]) for c in names},
        "per_target": {t: sum(1 for r in pairs if r["target"] == t and r["answered"]) for t in names},
    }


# ------------------------------------------------------------------ phase C


def smoke(registry) -> list[dict]:
    chosen: list[tuple[str, object]] = []
    seen: set[str] = set()
    for sub in registry.all_subagents():
        for tool in sub.tools:
            if tool.name in seen:
                continue
            seen.add(tool.name)
            required = (tool.parameters or {}).get("required") or []
            if (tool.permission is Permission.REGULAR and not required
                    and _SAFE_NAME.search(tool.name) and not _UNSAFE_NAME.search(tool.name)):
                chosen.append((sub.name, tool))
    rows: list[dict] = []
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="probe")
    for agent, tool in chosen:
        started = time.perf_counter()
        future = pool.submit(tool.handler, {})
        try:
            text = str(future.result(timeout=_SMOKE_TIMEOUT_S))
            outcome = "honest" if text.lstrip().startswith(_HONEST) else "ok"
            detail = f"{len(text)} characters"
        except concurrent.futures.TimeoutError:
            outcome, detail = "timeout", f"over {_SMOKE_TIMEOUT_S:.0f}s"
        except Exception as exc:  # noqa: BLE001 - a raising handler is the finding
            outcome, detail = "raised", f"{type(exc).__name__}: {str(exc)[:120]}"
        rows.append({
            "agent": agent, "tool": tool.name, "outcome": outcome, "detail": detail,
            "ms": round((time.perf_counter() - started) * 1000, 1),
        })
    pool.shutdown(wait=False, cancel_futures=True)
    return rows


# ------------------------------------------------------------------ report


def markdown(agents, problems, talk, smoke_rows, elapsed) -> str:
    today = time.strftime("%Y-%m-%d")
    lines = [
        f"# Agent abilities and cross-talk report ({today})",
        "",
        "Produced by `scripts/agent_abilities_probe.py` in a throwaway workspace. The model is a scripted stand-in, "
        "so this proves the plumbing, the gates and the rules for every agent. It does not prove a real model "
        "chooses well; the live sweep (`scripts/e2e_peer_sweep.py`) covers that.",
        "",
        "## Result",
        "",
        f"- Agents: {talk['agents']}. Tools: {len({t for a in agents for t in a['tools']})} (unique).",
        f"- Cross-talk: {talk['answered']} of {talk['pairs']} ordered pairs answered "
        f"({talk['answered_free']} straight through, {talk['asked_owner_first']} after asking the owner first).",
        f"- Asking yourself refused: {talk['self_asks_refused']} of {talk['agents']}.",
        f"- Pairs that behaved wrongly: {len(talk['wrong'])}.",
        f"- Tool audit problems: {len(problems)}.",
        f"- Safe tool smoke: {dict(Counter(r['outcome'] for r in smoke_rows)) or 'skipped'}.",
        f"- Took {elapsed:.0f} s.",
        "",
        "## Every agent",
        "",
        "| Agent | Domain | Tools | Ask the owner first | Reads others' text | Asked freely by page readers | Reached by | Can reach |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for a in agents:
        lines.append(
            f"| {a['agent']} | {a['domain']} | {len(a['tools'])} | {len(a['gated'])} | "
            f"{'yes' if a['reads_other_peoples_text'] else 'no'} | {'yes' if a['open_to_untrusted_askers'] else 'no'} | "
            f"{talk['per_target'][a['agent']]} of {talk['agents'] - 1} | {talk['per_caller'][a['agent']]} of {talk['agents'] - 1} |"
        )
    lines += ["", "## What each agent can do", ""]
    for a in agents:
        gated = set(a["gated"])
        names = ", ".join(f"{t}*" if t in gated else t for t in a["tools"])
        lines += [f"**{a['agent']}** ({a['domain']}): {a['description'][:220]}", f"Tools ({len(a['tools'])}): {names}", ""]
    lines.append("`*` means the tool asks the owner first.")
    lines += ["", "## Tool audit problems", ""]
    lines += [f"- {p['agent']}.{p['tool']}: {p['problem']}" for p in problems] or ["None."]
    lines += ["", "## Pairs that behaved wrongly", ""]
    lines += [f"- {w['caller']} -> {w['target']}: {w['why']}" for w in talk["wrong"][:40]] or ["None."]
    lines += ["", "## Safe tool smoke (read-only tools that need no arguments, called once each)", ""]
    if smoke_rows:
        lines += ["| Agent | Tool | Outcome | Detail |", "|---|---|---|---|"]
        lines += [f"| {r['agent']} | {r['tool']} | {r['outcome']} | {r['detail']} |" for r in smoke_rows]
        lines += ["", "`honest` means the tool answered with an error, refusal or not-configured message, which is a "
                  "correct answer in a throwaway workspace with no accounts. `raised` or `timeout` is a bug to look at."]
    else:
        lines.append("Skipped.")
    lines += [
        "", "## Not exercised, and why", "",
        "- Tools that send, write, delete, click, type, play, order, schedule or run commands: they change things, "
        "and each has its own tests and its own owner prompt. They were listed above, not called.",
        "- Tools with required arguments: the probe does not invent arguments for them.",
        "- Runs on the Claude CLI backend: tools there execute inside the MCP bridge subprocess, where ask_agent is "
        "deliberately not offered (it needs this process's nested dispatch). list_agents is.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="write the raw results as JSON")
    ap.add_argument("--report", help="write the markdown report here")
    ap.add_argument("--skip-smoke", action="store_true")
    args = ap.parse_args()
    started = time.time()
    registry = build_general_registry()
    agents, problems = audit_tools(registry)
    print(f"A. {len(agents)} agents, {len({t for a in agents for t in a['tools']})} tools, {len(problems)} audit problems")
    talk = cross_talk(registry)
    print(f"B. {talk['answered']}/{talk['pairs']} pairs answered "
          f"({talk['answered_free']} free, {talk['asked_owner_first']} after asking the owner), "
          f"{talk['self_asks_refused']}/{talk['agents']} self-asks refused, {len(talk['wrong'])} wrong")
    rows = [] if args.skip_smoke else smoke(registry)
    if rows:
        print(f"C. {len(rows)} safe tools called: {dict(Counter(r['outcome'] for r in rows))}")
    elapsed = time.time() - started
    if args.out:
        Path(args.out).write_text(json.dumps({"agents": agents, "problems": problems, "talk": talk, "smoke": rows}, indent=1))
    if args.report:
        Path(args.report).write_text(markdown(agents, problems, talk, rows, elapsed), encoding="utf-8")
    bad = talk["wrong"] or [r for r in rows if r["outcome"] in ("raised", "timeout")]
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
