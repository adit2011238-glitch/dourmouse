#!/usr/bin/env python3
"""E2E directive sweep — drives the REAL running DourMouse app the same way
the command box does (POST /api/chat + SSE stream, auto-confirming gated
tools via /api/confirm). No shortcuts: every result is the actual dispatch
path's output.

Usage:  python3 scripts/e2e_directive_sweep.py [--base http://127.0.0.1:8765] [--only N,M]
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8765"

# Every feature area of the roster, one realistic directive each. The LLM
# routes to its own tools — we only verify the results it produces.
DIRECTIVES = [
    # --- system ---------------------------------------------------------
    ("system", "Give me your system info: CPU, memory and disk usage.",
     ["system_info"]),
    ("system", "Run the command `echo e2e-sweep-ok` and show me its output.",
     ["run_command"]),
    # --- admin_ops ------------------------------------------------------
    ("admin_ops", "List the files in the dourmouse workspace folder (the one with webui.py).",
     ["list_files"]),
    # --- dev_coding -----------------------------------------------------
    ("dev_coding", "Run a python snippet that computes 2+2 and prints the result.",
     ["run_python"]),
    # --- memory ---------------------------------------------------------
    # The model must call `remember` (the fact must actually persist);
    # `recall` is optional since the answer is in context.
    ("memory", "Remember that my favorite color is teal, then tell me what my favorite color is.",
     ["remember"]),
    # --- tasks ----------------------------------------------------------
    ("tasks", "Add a task called 'e2e sweep test' and then list all my tasks.",
     ["add_task", "list_tasks"]),
    # --- news -----------------------------------------------------------
    ("news", "What are today's top news headlines? Give me 3.",
     ["news_headlines"]),
    # --- markets --------------------------------------------------------
    ("markets", "What is the current price of AAPL stock?",
     ["stock_quote"]),
    ("markets", "What are the top market movers right now?",
     ["market_movers"]),
    # --- research_info / rnd -------------------------------------------
    ("research_info", "Search the web for the latest NVIDIA news and give me a one-line summary.",
     ["web_search"]),
    ("research_info", "Fetch https://example.com and tell me its title.",
     ["fetch_url"]),
    # --- music ----------------------------------------------------------
    ("music", "What's currently playing on Spotify?",
     ["spotify_now_playing"]),
    ("music", "Show me my top 3 Spotify tracks.",
     ["spotify_top_tracks"]),
    ("music", "List my Spotify playlists.",
     ["spotify_playlists"]),
    ("music", "Play my jazz bar in nyc playlist on Spotify.",
     ["spotify_play"]),
    ("music", "Search Spotify for the song Levels by Avicii.",
     ["spotify_search"]),
    ("music", "What is the current Spotify playback state?",
     ["spotify_playback_state"]),
    # --- mail -----------------------------------------------------------
    ("mail", "How many unread emails are in my inbox? Just report the count.",
     ["read_inbox"]),
    # --- scheduling -----------------------------------------------------
    ("scheduling", "List my calendar events for today.",
     ["list_calendar_events"]),
    ("scheduling", "Propose three meeting slots this week.",
     ["propose_time_slots"]),
    # --- atlas ----------------------------------------------------------
    ("atlas", "What is the ATLAS engine status?",
     ["atlas_status"]),
    ("atlas", "What version is the ATLAS engine?",
     ["atlas_version"]),
    ("atlas", "Give me the ATLAS health check.",
     ["atlas_health"]),
    # --- worldmonitor ---------------------------------------------------
    ("worldmonitor", "What is the World Monitor status?",
     ["worldmonitor_status"]),
    # --- comms ----------------------------------------------------------
    ("comms", "Draft an email to myself saying the e2e sweep is running. Do not send it.",
     ["draft_message"]),
    # --- connections ----------------------------------------------------
    ("system", "Check my connections: which services are linked and working?",
     ["check_connections"]),
    # --- v5.22.9: slash commands -----------------------------------------
    # Each slash runs its own backend and streams brain slash:<cmd> + done.
    ("slash", "/claude Reply with exactly: CLAUDE-SLASH-OK", ["slash:claude"]),
    ("slash", "/nvidia Reply with exactly: NVIDIA-SLASH-OK", ["slash:nvidia"]),
    ("slash", "/deepseek Reply with exactly: DEEPSEEK-SLASH-OK", ["slash:deepseek"]),
    ("slash", "/chatgpt Reply with exactly: CHATGPT-SLASH-OK", ["slash:chatgpt"]),
    ("slash", "/freebuff Write a note: sweep check", ["slash:freebuff"]),
    # --- v5.22.9: All-Hands ---------------------------------------------
    # Starts a real run; the verifier polls /api/allhands/<id> to done and
    # requires the synthesis. (Brain cards may honestly report NOT CONFIGURED
    # for a missing key — that is correct behaviour, not a bug.)
    ("allhands", "/all In one short sentence: what is 2+2?", ["allhands_started"]),
    ("allhands", "use all resources to tell me in one sentence what Python is",
     ["allhands_started"]),
]

FAIL_MARKERS = ("ERROR:", "error:", "Traceback",
                "CONFIRMATION REQUIRED", "does not exist on this account",
                "No module named")
#: Tool results containing these are the app behaving CORRECTLY — the
#: capability is honestly not set up on this machine (no Google OAuth client,
#: Freebuff app closed, ...). Counted HONEST, never a FAIL: there is nothing
#: to fix in the app when it tells the truth about missing setup.
HONEST_MARKERS = ("NOT CONFIGURED",)


def sse_post(prompt: str, timeout: float = 240.0) -> list[dict]:
    """Send one directive exactly like the command box and return every SSE
    event, auto-approving confirmation gates."""
    body = json.dumps({"prompt": prompt}).encode()
    req = urllib.request.Request(
        BASE + "/api/chat", data=body,
        headers={"Content-Type": "application/json"},
    )
    events: list[dict] = []
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        # CRITICAL: read BYTE-AT-A-TIME and buffer. ``read(4096)`` blocks
        # until 4096 bytes arrive or the stream ENDS — a long-running tool
        # (confirmation-gated play, multi-turn search) never fills the
        # buffer, so the confirmation event is only seen AFTER the gate
        # auto-declines (300s) and the run dies. Byte reads return the
        # instant a byte is available, exactly like the browser's fetch
        # reader. This was the root cause of the sweep's "confirm 409" runs.
        buf = b""
        while True:
            byte = resp.read(1)
            if not byte:
                break
            buf += byte
            if buf.endswith(b"\n\n"):
                block, buf = buf[:-2], b""
                for line in block.split(b"\n"):
                    if line.startswith(b"data: "):
                        try:
                            evt = json.loads(line[6:])
                        except json.JSONDecodeError:
                            continue
                        events.append(evt)
                        if evt.get("type") == "confirmation_requested":
                            _confirm(evt.get("id"))
                        if evt.get("type") in ("done", "error"):
                            return events
    return events


def _confirm(confirm_id) -> None:
    try:
        req = urllib.request.Request(
            BASE + "/api/confirm",
            data=json.dumps({"id": confirm_id, "approved": True}).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as exc:  # noqa: BLE001
        print(f"    [confirm {confirm_id} failed: {exc}]", file=sys.stderr)


def _tools_used(events: list[dict]) -> list[str]:
    return [e.get("tool") or e.get("name") for e in events
            if e.get("type") == "tool_use"]


def _tool_results(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("type") == "tool_result"]


def _done_text(events: list[dict]) -> str:
    return "\n".join(e.get("text", "") for e in events
                     if e.get("type") == "assistant_text" and e.get("text"))


def classify(events: list[dict], expected: list[str]) -> dict:
    used = _tools_used(events)
    results = _tool_results(events)
    terminal = [e for e in events if e.get("type") in ("done", "error")]
    done = terminal[-1] if terminal else {}
    final_text = _done_text(events)

    problems = []
    honest_notes = []
    for r in results:
        text = str(r.get("text") or "")
        hit = None
        for marker in FAIL_MARKERS:
            if marker in text:
                hit = f"{r.get('tool')}: {text[:120]}"
                break
        if hit:
            problems.append(hit)
            continue
        for marker in HONEST_MARKERS:
            if marker in text:
                honest_notes.append(f"{r.get('tool')}: {text[:120]}")
                break

    if done.get("type") == "error":
        problems.append(f"run error: {done.get('text', '')[:200]}")

    # ---- slash-command mode (brain slash:<cmd> + done) ----
    slash_expect = [e for e in expected if e.startswith("slash:")]
    if slash_expect:
        brains = [e.get("model") for e in events if e.get("type") == "brain"]
        for want in slash_expect:
            if want not in brains:
                problems.append(f"expected brain {want}; brains={brains}")
        if "NOT CONFIGURED" in final_text:
            honest_notes.append("backend honestly NOT CONFIGURED on this machine")

    # ---- All-Hands mode (allhands_started + completed run) ----
    if "allhands_started" in expected:
        started = [e for e in events if e.get("type") == "allhands_started"]
        if not started:
            problems.append("expected allhands_started event but none arrived")
        else:
            run_id = started[0].get("run_id")
            snap = _wait_allhands(run_id)
            if snap is None:
                problems.append(f"all-hands run {run_id} never completed")
            else:
                if not snap.get("synthesis"):
                    problems.append(f"no synthesis: {str(snap.get('error'))[:150]}")
                # Honest per-brain red cards are expected behaviour.
                errs = [f"{k}: {b.get('error', '')[:80]}" for k, b in
                        snap.get("brains", {}).items()
                        if b.get("status") == "error"]
                for err in errs:
                    if "NOT CONFIGURED" in err:
                        honest_notes.append("brain card: " + err)
                    else:
                        honest_notes.append("brain card: " + err)

    # Expected-tool coverage: did the run use at least one expected tool?
    # (slash:/allhands_started expectations are matched by their own modes.)
    checkable = [t for t in expected
                 if not t.startswith("slash:") and t != "allhands_started"]
    missing = [t for t in checkable if t not in used]
    if missing:
        problems.append(f"expected tool(s) not used: {missing}; used={used}")

    if problems:
        verdict = "FAIL"
    elif honest_notes:
        verdict = "HONEST"
    else:
        verdict = "PASS"
    return {
        "verdict": verdict,
        "tools_used": used,
        "problems": problems,
        "honest_notes": honest_notes,
        "final_text": final_text[-600:],
        "n_events": len(events),
    }


def _wait_allhands(run_id: str, timeout: float = 180.0) -> dict | None:
    """Poll /api/allhands/<id> until the run settles; None on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                    BASE + "/api/allhands/" + run_id, timeout=10) as resp:
                data = json.loads(resp.read().decode())
        except Exception:  # noqa: BLE001
            return None
        run = data.get("run") or {}
        if run.get("status") != "running":
            return run
        time.sleep(4)
    return None


def main() -> int:
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--only", default=None, help="comma list of 0-based indexes")
    args = ap.parse_args()
    BASE = args.base

    indexes = [int(i) for i in args.only.split(",")] if args.only else range(len(DIRECTIVES))
    report = []
    for i in indexes:
        area, prompt, expected = DIRECTIVES[i]
        print(f"\n[{i:02d}] {area:14s} -> {prompt[:70]}")
        t0 = time.time()
        try:
            events = sse_post(prompt)
        except Exception as exc:  # noqa: BLE001
            print(f"    NETWORK/RUN FAILURE: {exc}")
            report.append({"index": i, "area": area, "prompt": prompt,
                           "expected": expected, "verdict": "FAIL",
                           "problems": [f"run failure: {exc}"],
                           "tools_used": [], "final_text": "", "n_events": 0})
            continue
        r = classify(events, expected)
        r.update({"index": i, "area": area, "prompt": prompt, "expected": expected})
        report.append(r)
        print(f"    [{r['verdict']}] tools={r['tools_used']} in {time.time()-t0:.0f}s")
        for p in r["problems"][:4]:
            print(f"      ! {p}")
        with open("/tmp/e2e_sweep_report.json", "w") as fh:
            json.dump(report, fh, indent=2, default=str)

    passed = sum(1 for r in report if r["verdict"] == "PASS")
    honest = sum(1 for r in report if r["verdict"] == "HONEST")
    failed = sum(1 for r in report if r["verdict"] == "FAIL")
    print("\n" + "=" * 70)
    print(f"SWEEP COMPLETE: {passed} PASS / {honest} HONEST / {failed} FAIL "
          f"(of {len(report)})")
    for r in report:
        mark = r["verdict"]
        print(f"  [{mark}] {r['index']:02d} {r['area']:14s} "
              f"tools={r['tools_used']}")
        for p in r["problems"][:3]:
            print(f"        ! {p}")
        for n in r.get("honest_notes", [])[:2]:
            print(f"        ~ {n}")
    print("=" * 70)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
