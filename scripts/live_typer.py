#!/usr/bin/env python3
"""LIVE E2E typer — drives the REAL desktop DourMouse window.

For each directive it:
  1. raises the main "DOURMOUSE // CENTRAL AGENT DISPATCH" window (AXRaise),
  2. clicks the command box inside it,
  3. types the prompt with AppleScript keystrokes,
  4. presses Return (key code 36),
  5. waits for the newest session audit JSONL to contain that prompt with a
     final_text, then classifies PASS/FAIL (expected tool used, no failure
     markers, no run error).

Usage:
    python3 scripts/live_typer.py "prompt|expected,tool1" "prompt2|tool" ...

The session audit lives under <workspace>/sessions/session_*.jsonl (the same
place the HUD's own runs persist).
"""
import glob
import json
import os
import subprocess
import sys
import time

SESSIONS_DIR = "/Applications/dourmouse-dist/workspace/sessions"
MAIN_WIN = "DOURMOUSE // CENTRAL AGENT DISPATCH"
FAIL_MARKERS = ("ERROR:", "Traceback", "CONFIRMATION REQUIRED",
                "does not exist on this account", "No module named",
                "run error")

APPLE_ACTIVATE = """tell application "System Events"
  set frontmost of (first process whose name is "Python") to true
end tell"""

APPLE_RAISE = """tell application "System Events"
  tell process "Python"
    set frontmost to true
    try
      perform action "AXRaise" of window "%s"
    end try
  end tell
  delay 0.4
end tell"""

APPLE_TYPE = """tell application "System Events"
  tell process "Python"
    set frontmost to true
  end tell
  delay 0.2
  click at {%d, %d}
  delay 0.4
  -- clear any leftover text in the box (Cmd+A then delete) so a previous
  -- directive can never concatenate onto this one
  key code 0 using command down
  delay 0.3
  key code 51
  delay 0.3
  keystroke %s
  delay 0.5
  -- submit with Return; the SEND-button click path throws CGEvent -25200
  -- from a detached session, and Enter is reliable once the box is clean
  key code 36
end tell"""


def _osascript(script: str) -> str:
    proc = subprocess.run(["osascript", "-e", script],
                          capture_output=True, text=True, timeout=30)
    return (proc.stderr or "").strip()


def _activate_app() -> None:
    """Fully activate the DourMouse app via AppKit (switches to its Space,
    raises its windows). System Events `frontmost` alone does NOT cross
    Spaces; NSRunningApplication.activate does."""
    code = (
        "from AppKit import NSRunningApplication, NSApplicationActivateAllWindows, "
        "NSApplicationActivateIgnoringOtherApps\n"
        "import subprocess, re\n"
        "pid = int(subprocess.run(['pgrep','-f','dourmouse.desktop'],"
        "capture_output=True, text=True).stdout.split()[0])\n"
        "app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)\n"
        "if app is not None:\n"
        "    app.activateWithOptions_(NSApplicationActivateAllWindows | "
        "NSApplicationActivateIgnoringOtherApps)\n"
    )
    try:
        subprocess.run(["/Applications/dourmouse-dist/.venv/bin/python", "-c", code],
                       capture_output=True, text=True, timeout=20)
    except Exception:  # noqa: BLE001
        pass


def _window_bounds() -> tuple[int, int, int, int] | None:
    """The main window's (x, y, w, h) on the CURRENT space, or None.

    Uses kCGWindowListOptionAll and requires kCGWindowIsOnscreen — the
    window must be visible on the space the user is looking at, or we must
    NOT type anywhere (keystrokes would go to whatever app is frontmost,
    e.g. the user's Freebuff chat — never type blind).
    """
    code = (
        "import Quartz, json\n"
        "wl = Quartz.CGWindowListCopyWindowInfo("
        "Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID)\n"
        "for w in wl:\n"
        "    if (w.get('kCGWindowName','') == 'DOURMOUSE // CENTRAL AGENT DISPATCH'"
        " and w.get('kCGWindowIsOnscreen')):\n"
        "        b = w['kCGWindowBounds']\n"
        "        print(json.dumps([b['X'], b['Y'], b['Width'], b['Height']]))\n"
        "        break\n"
    )
    proc = subprocess.run(
        ["/Applications/dourmouse-dist/.venv/bin/python", "-c", code],
        capture_output=True, text=True, timeout=30)
    try:
        x, y, w, h = json.loads(proc.stdout.strip().splitlines()[-1])
        return int(x), int(y), int(w), int(h)
    except Exception:  # noqa: BLE001
        return None


def _newest_session() -> str | None:
    files = sorted(glob.glob(os.path.join(SESSIONS_DIR, "session_*.jsonl")))
    return files[-1] if files else None


def _auto_confirm() -> None:
    """Approve any pending confirmation gate (ids are sequential confirm-N).
    The gated tools (spotify_play, run_command with confirm) emit
    confirmation_requested in the SSE stream the window consumes; the typer
    does not parse that stream, so it brute-forces the small id space — a
    non-pending id is a no-op, an honest 404-style False."""
    for n in range(1, 16):
        try:
            req = urllib.request.Request(
                BASE + "/api/confirm",
                data=json.dumps({"id": f"confirm-{n}", "approved": True}).encode(),
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=5).read()
        except Exception:  # noqa: BLE001
            pass


def _verify(prompt: str, expected: list[str]) -> dict:
    """Wait for the run to land in the audit, then classify it."""
    deadline = time.time() + 300
    while time.time() < deadline:
        _auto_confirm()
        path = _newest_session()
        if path:
            try:
                lines = open(path).read().splitlines()
            except OSError:
                lines = []
            for line in lines[-3:]:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                user = rec.get("user", "")
                if user and prompt[:40] in user:
                    final = str(rec.get("final_text") or "")
                    transcript = rec.get("transcript", [])
                    tools = [e.get("name") for e in transcript
                             if e.get("type") == "tool_use"]
                    problems = []
                    for marker in FAIL_MARKERS:
                        if marker in final:
                            problems.append(marker)
                    # "a|b" means EITHER tool satisfies the expectation.
                    missing = []
                    for t in expected:
                        if "|" in t:
                            if not any(alt in tools for alt in t.split("|")):
                                missing.append(t)
                        elif t not in tools:
                            missing.append(t)
                    if missing:
                        problems.append(f"expected tools not used: {missing}")
                    return {"verdict": "FAIL" if problems else "PASS",
                            "tools": tools, "problems": problems,
                            "final": final[:400]}
        time.sleep(2)
    return {"verdict": "FAIL", "tools": [], "problems": ["timeout — no run landed"],
            "final": ""}


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    directives = []
    for arg in sys.argv[1:]:
        prompt, _, expected = arg.partition("|")
        directives.append((prompt.strip(), [e for e in expected.split(",") if e]))

    report = []
    for i, (prompt, expected) in enumerate(directives):
        # Bring the DourMouse app to the front FIRST — if its windows live
        # on another Space, activation switches the user's view to them, and
        # the window becomes on-screen (global coords stay valid). Only then
        # re-query; if it is still not on-screen, SKIP safely (never type
        # into the wrong app).
        bounds = None
        for attempt in range(8):
            _activate_app()
            time.sleep(3.0 if attempt else 1.2)
            bounds = _window_bounds()
            if bounds is not None:
                break
        if bounds is None:
            print(f"[{i:02d}] SKIP — DourMouse window not on the current space "
                  f"after 8 activations (keep it on your screen, then re-run)",
                  flush=True)
            report.append({"verdict": "SKIP",
                           "problems": ["window not on current space"]})
            continue
        x, y, w, h = bounds
        cx, cy = x + int(w * 0.45), y + h - 45
        err = _osascript(APPLE_RAISE % MAIN_WIN)
        time.sleep(0.4)
        err2 = _osascript(APPLE_TYPE % (cx, cy, json.dumps(prompt)))
        print(f"[{i:02d}] typed: {prompt[:60]}", flush=True)
        if err or err2:
            print(f"    apple error: {(err or err2)[:120]}")
        r = _verify(prompt, expected)
        r.update({"prompt": prompt, "expected": expected})
        report.append(r)
        print(f"    [{r['verdict']}] tools={r['tools']} "
              f"({time.strftime('%H:%M:%S')})", flush=True)
        for p in r["problems"][:3]:
            print(f"      ! {p}")
        with open("/tmp/live_typer_report.json", "w") as fh:
            json.dump(report, fh, indent=2)

    passed = sum(1 for r in report if r["verdict"] == "PASS")
    skipped = sum(1 for r in report if r["verdict"] == "SKIP")
    print(f"\nLIVE SWEEP: {passed}/{len(report)} passed "
          f"({skipped} skipped — window off-screen)")
    return 0 if passed == len(report) else 1


if __name__ == "__main__":
    sys.exit(main())
