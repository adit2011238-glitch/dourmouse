#!/usr/bin/env python3
"""Live check that agents can talk to each other (finding #189).

Drives the REAL running app the way the command box does: POST /api/chat with a pinned
focus_agent (what a chat screen sends) and the real model, reading the SSE stream and
auto-approving any owner prompt through /api/confirm. Point it at a preview server
(scripts/dev_preview_webui.sh, port 18765), not at the owner's own app.

Each case pins one agent and tells it, in plain words, to hand a job to another agent.
It passes when the stream shows the peer call, the peer's answer coming back, and a final
answer; and, for a conversation that reads text written by other people asking an agent
that can act, when the owner prompt appeared first.

  .venv/bin/python scripts/e2e_peer_sweep.py [--base http://localhost:18765] [--only 1,3]

The offline counterpart, which covers every agent pair with a scripted model, is
scripts/agent_abilities_probe.py.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

# (label, pinned agent, prompt, peer, expect_owner_prompt, tool that proves the call)
CASES = [
    ("markets asks news (open peer, no prompt)", "markets",
     "Use ask_agent to ask the news agent for the single top headline right now, then tell me it in one sentence.",
     "news", False, "ask_agent"),
    ("reviewer asks research_info (open peer, no prompt)", "reviewer",
     "Use ask_agent to ask the research_info agent what the capital of Australia is, and give me its answer.",
     "research_info", False, "ask_agent"),
    ("docs asks system (page reader to an agent that acts: owner is asked)", "docs",
     "Use ask_agent to ask the system agent for this Mac's system info (CPU and memory), then summarise it.",
     "system", True, "ask_agent"),
    ("browser asks news (pinned screen, the app's own loop)", "browser",
     "Ask the news agent for the single top headline right now. Hand that one job to it, then tell me the headline.",
     "news", False, "ask_agent"),
]


def post_chat(base: str, prompt: str, agent: str, timeout: float) -> tuple[list[dict], list[str]]:
    payload = {"prompt": prompt, "screen": "HOME"}
    if agent:
        payload["focus_agent"] = agent  # empty means an unpinned HOME turn
    body = json.dumps(payload).encode()
    req = urllib.request.Request(base + "/api/chat", data=body, headers={"Content-Type": "application/json"})
    events: list[dict] = []
    prompts: list[str] = []
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        buf = b""
        while True:
            byte = resp.read(1)  # byte at a time: a gate event must be seen the instant it arrives
            if not byte:
                break
            buf += byte
            if not buf.endswith(b"\n\n"):
                continue
            block, buf = buf[:-2], b""
            for line in block.split(b"\n"):
                if not line.startswith(b"data: "):
                    continue
                try:
                    evt = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                events.append(evt)
                if evt.get("type") == "confirmation_requested":
                    prompts.append(str(evt.get("prompt") or evt.get("text") or evt)[:300])
                    confirm(base, evt.get("id"))
                if evt.get("type") in ("done", "error"):
                    return events, prompts
    return events, prompts


def confirm(base: str, confirm_id) -> None:
    req = urllib.request.Request(
        base + "/api/confirm", data=json.dumps({"id": confirm_id, "approved": True}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as exc:  # noqa: BLE001
        print(f"    [confirm {confirm_id} failed: {exc}]", file=sys.stderr)


def judge(events: list[dict], prompts: list[str], peer: str, expect_prompt: bool, tool) -> dict:
    wanted = (tool,) if isinstance(tool, str) else tuple(tool)
    uses = [e for e in events if e.get("type") == "tool_use"]
    names = [e.get("tool") or e.get("name") for e in uses]
    results = [e for e in events if e.get("type") == "tool_result" and (e.get("name") or e.get("tool")) in wanted]
    done = [e for e in events if e.get("type") == "done"]
    final = (done[-1].get("final_text") or done[-1].get("text") or "") if done else ""
    errors = [e for e in events if e.get("type") == "error"]
    called = any(t in names for t in wanted)

    def good(result: dict) -> bool:
        text = str(result.get("text") or "")
        if (result.get("name") or result.get("tool")) == "ask_agent":
            return text.startswith(f"ANSWER FROM {peer}")
        return bool(text.strip()) and not text.startswith(("ERROR", "REFUSED"))

    checks = {
        "called the peer tool": called,
        "peer answered": any(good(r) for r in results),
        "owner asked exactly when expected": bool(prompts) == expect_prompt,
        "final answer given": bool(final.strip()),
        "no error event": not errors,
    }
    return {"ok": all(checks.values()), "checks": checks, "tools": names, "prompts": prompts, "final": final[:300],
            "error": (errors[0].get("message") if errors else "")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:18765")
    ap.add_argument("--only", default="", help="comma separated case numbers, 1 based")
    ap.add_argument("--timeout", type=float, default=300.0)
    args = ap.parse_args()
    only = {int(x) for x in args.only.split(",") if x.strip()}
    failed = 0
    for n, (label, agent, prompt, peer, expect_prompt, tool) in enumerate(CASES, start=1):
        if only and n not in only:
            continue
        print(f"[{n}] {label}")
        started = time.time()
        try:
            events, prompts = post_chat(args.base, prompt, agent, args.timeout)
        except Exception as exc:  # noqa: BLE001
            print(f"    FAIL: could not run: {exc}")
            failed += 1
            continue
        r = judge(events, prompts, peer, expect_prompt, tool)
        print(f"    {'PASS' if r['ok'] else 'FAIL'} in {time.time() - started:.0f}s; tools: {r['tools']}")
        for name, ok in r["checks"].items():
            print(f"      {'ok ' if ok else 'BAD'} {name}")
        if r["prompts"]:
            print(f"      owner prompt: {r['prompts'][0][:200]}")
        print(f"      final: {r['final']!r}")
        if r["error"]:
            print(f"      error: {r['error']}")
        failed += 0 if r["ok"] else 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
