#!/usr/bin/env python3
"""Live check for the OS shell (finding #144): one command, real Chrome.

Starts an ISOLATED demo server (temp workspace and config dir, its own port),
opens /shell in the installed Chrome through Playwright, visits screens in
order and reports, per visit: the screen's data-state, console errors, page
errors, failed requests, and the shell's own leak counters
(window.__dmShell.stats()). It kills only the server it started, by its own
process id. It never touches 8765, 9333 or 9334, the owner's live app.

Leak check: when a screen is visited again, its counters must equal those of
its first visit. Growth means a screen left a subscription, timer or listener
behind on unmount.

    .venv/bin/python scripts/os_shell_check.py --screens home,security,news,home
    .venv/bin/python scripts/os_shell_check.py --screens home --stub-chat \
        --send "hello" --approve --shot-dir /tmp/shots

--stub-chat answers POST /api/chat with a scripted stream (a tool call, an
approval request, a reply) and POST /api/confirm with ok, so a thread screen
can be exercised without a model and without spending anything. It proves the
layout and the client, never a model answer.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

STUB_STREAM = "".join(
    "data: " + json.dumps(e) + "\n\n"
    for e in (
        {"type": "brain", "model": "stub/model"},
        {"type": "tool_use", "name": "stub_tool", "raw_arguments": '{"q": "example"}'},
        {"type": "tool_result", "name": "stub_tool", "text": "stub result"},
        {"type": "confirmation_requested", "id": "stub-1", "tool": "stub_tool", "prompt": "STUB: allow the example action?"},
        {"type": "assistant_delta", "text": "STUBBED STREAM, not a model answer. A **bold** word and `code`."},
        {"type": "done", "final_text": "STUBBED STREAM, not a model answer. A **bold** word and `code`."},
    )
)


def start_server(port: int) -> tuple[subprocess.Popen[bytes], str]:
    tmp = tempfile.mkdtemp(prefix="dm_shell_check_")
    env = dict(os.environ)
    env.update(
        DOURMOUSE_UI_PORT=str(port),
        DOURMOUSE_WORKSPACE=str(Path(tmp) / "ws"),
        DOURMOUSE_CONFIG_DIR=str(Path(tmp) / "cfg"),
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "dourmouse.webui"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    for _ in range(80):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/os/ping", timeout=1):
                return proc, tmp
        except OSError:
            if proc.poll() is not None:
                raise SystemExit(f"the demo server exited early (code {proc.returncode}); is port {port} in use?") from None
            time.sleep(0.25)
    proc.terminate()
    raise SystemExit("the demo server did not answer /api/os/ping within 20 s")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=18790)
    ap.add_argument("--screens", default="home", help="comma list of screen slugs, visited in order")
    ap.add_argument("--shot-dir", default="", help="save <slug>-<n>.png per visit here")
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--settle", type=float, default=1.2, help="seconds to wait after each mount")
    ap.add_argument("--stub-chat", action="store_true")
    ap.add_argument("--send", default="", help="type this into the directive box on each visited thread screen")
    ap.add_argument("--approve", action="store_true", help="click APPROVE on a stub approval card")
    ap.add_argument("--eval", action="append", default=[], help="JS expression evaluated after each mount")
    ap.add_argument("--json", action="store_true", help="print only the JSON report")
    args = ap.parse_args()
    if not 1024 <= args.port <= 65535 or args.port in (8765, 9333, 9334):
        raise SystemExit("choose a demo port that is not the owner's (8765, 9333, 9334)")

    from playwright.sync_api import sync_playwright

    proc, tmp = start_server(args.port)
    report: dict = {"port": args.port, "workspace": tmp, "visits": [], "problems": []}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page(viewport={"width": args.width, "height": args.height})
            console_errors: list[str] = []
            page_errors: list[str] = []
            failed: list[str] = []
            page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: page_errors.append(str(e)))
            page.on("requestfailed", lambda r: failed.append(f"{r.method} {r.url} {r.failure}"))
            page.on("response", lambda r: failed.append(f"HTTP {r.status} {r.url}") if r.status >= 400 else None)
            if args.stub_chat:
                page.route("**/api/chat", lambda r: r.fulfill(status=200, content_type="text/event-stream", body=STUB_STREAM))
                page.route("**/api/confirm", lambda r: r.fulfill(status=200, content_type="application/json", body='{"ok": true}'))
            base = f"http://127.0.0.1:{args.port}/shell"
            first: dict[str, dict] = {}
            for n, slug in enumerate(s for s in args.screens.split(",") if s.strip()):
                slug = slug.strip().lower()
                mark = (len(console_errors), len(page_errors), len(failed))
                if n == 0:
                    page.goto(f"{base}#/{slug}")
                else:
                    page.evaluate("(s) => { location.hash = '#/' + s; }", slug)
                page.wait_for_selector(f'#body [data-screen="{slug.upper()}"]', timeout=15000)
                page.wait_for_timeout(int(args.settle * 1000))
                visit: dict = {"screen": slug}
                if args.send and page.query_selector("#composer:not([hidden]) #cin"):
                    page.fill("#cin", args.send)
                    page.press("#cin", "Enter")
                    page.wait_for_timeout(1500)
                    visit["turns"] = page.locator(".turn").count()
                    visit["chips"] = page.locator(".turn .chips .tag").count()
                    visit["approval_cards"] = page.locator(".approve").count()
                    if args.approve and page.locator(".approve:not(.done) button.os-btn--primary").count():
                        page.click(".approve:not(.done) button.os-btn--primary")
                        page.wait_for_timeout(500)
                        visit["approval_title_after_click"] = page.locator(".approve .t").first.inner_text()
                visit["state"] = page.evaluate(f"document.querySelector('#body [data-screen=\"{slug.upper()}\"]')?.dataset.state || ''")
                visit["stats"] = page.evaluate("window.__dmShell && window.__dmShell.stats()")
                visit["evals"] = [page.evaluate(expr) for expr in args.eval]
                visit["console_errors"] = console_errors[mark[0]:]
                visit["page_errors"] = page_errors[mark[1]:]
                visit["failed_requests"] = failed[mark[2]:]
                if args.shot_dir:
                    Path(args.shot_dir).mkdir(parents=True, exist_ok=True)
                    shot = str(Path(args.shot_dir) / f"{slug}-{n + 1}.png")
                    page.screenshot(path=shot)
                    visit["shot"] = shot
                if visit["console_errors"] or visit["page_errors"]:
                    report["problems"].append(f"{slug}: console or page errors")
                if slug in first:
                    a, b = first[slug]["stats"] or {}, visit["stats"] or {}
                    grew = {k: [a.get(k), b.get(k)] for k in b if isinstance(b.get(k), int) and b.get(k) != a.get(k)}
                    visit["leak_vs_first_visit"] = grew
                    if grew:
                        report["problems"].append(f"{slug}: counters differ from its first visit {grew}")
                else:
                    first[slug] = visit
                report["visits"].append(visit)
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    report["ok"] = not report["problems"]
    print(json.dumps(report, indent=None if args.json else 2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
