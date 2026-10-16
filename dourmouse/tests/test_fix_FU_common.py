"""Shared support for the FU UI tests (no tests of its own).

Starts an isolated demo server (own port, temp workspace and config dir,
background loops off), and gives Playwright pages a request router so a test can
mock exactly the API calls it cares about. Everything else reaches the real
demo server. Real headless Chrome, never the owner's profile or app.

FU_OLD_UI=<dir containing the pre-fix ui/ tree> makes static files come from
that tree instead (used to watch a test fail on the old code):
    git archive HEAD ui | tar -x -C /tmp/old && FU_OLD_UI=/tmp/old/ui pytest ...
"""

from __future__ import annotations

import json
import mimetypes
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OLD_UI = os.environ.get("FU_OLD_UI", "").strip()

QUIET_ENV = {
    "DOURMOUSE_LLM_BACKEND": "ollama",
    "DOURMOUSE_LIBRARIAN": "0",
    "DOURMOUSE_DOWNLOADS_WATCH": "0",
    "DOURMOUSE_STANDING_AGENTS": "0",
    "DOURMOUSE_SECURITY_SENTRY_LOOP": "0",
    "DOURMOUSE_NETWATCH": "0",
    "DOURMOUSE_LOCKDOWN_ENFORCER": "0",
    "DOURMOUSE_SECURITY_ANALYST": "0",
    "DOURMOUSE_WORLD_PULSE_WARMER": "0",
    "DOURMOUSE_GMAIL_WARMER": "0",
    "DOURMOUSE_PROACTIVE_SURFACE": "0",
}


def chrome_usable() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            pw.chromium.launch(channel="chrome", headless=True).close()
        return True
    except Exception:
        return False


def pick_demo_port() -> int:
    for port in range(18871, 18890):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("no free 188xx port")


class DemoServer:
    def __init__(self) -> None:
        self.port = pick_demo_port()
        self.tmp = tempfile.mkdtemp(prefix="dm_fu_")
        env = dict(os.environ)
        env.update(QUIET_ENV)
        env.update(
            DOURMOUSE_UI_PORT=str(self.port),
            DOURMOUSE_WORKSPACE=str(Path(self.tmp) / "ws"),
            DOURMOUSE_CONFIG_DIR=str(Path(self.tmp) / "cfg"),
        )
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "dourmouse.webui"], cwd=ROOT, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(120):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/os/ping", timeout=1):
                    return
            except OSError:
                if self.proc.poll() is not None:
                    raise RuntimeError("demo server exited early")
                time.sleep(0.25)
        self.stop()
        raise RuntimeError("demo server did not answer")

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def json_body(data, status: int = 200) -> dict:
    return {"status": status, "content_type": "application/json", "body": json.dumps(data)}


STATIC_MAP = {
    "/shell": "shell.html", "/": "shell.html", "/console": "console.html", "/login": "login.html",
    "/setup": "setup.html", "/hud": "hud.html", "/all-hands": "all_hands.html", "/atlas-lab": "atlas_lab.html",
}


def install_router(page, base: str, mocks: list | None = None):
    """Route table for a page: mocks is a list of (substring-or-predicate, handler(route, request)).

    A handler fulfils, or calls route.fallback(). Anything unmatched goes to the real demo server,
    except static files when FU_OLD_UI is set, which come from the old ui tree."""
    mocks = mocks if mocks is not None else []

    def old_static(url: str):
        if not OLD_UI:
            return None
        path = url.split(base, 1)[-1].split("?", 1)[0].split("#", 1)[0]
        name = STATIC_MAP.get(path)
        rel = name if name else (path[1:] if path.startswith("/assets/") else None)
        if not rel:
            return None
        f = Path(OLD_UI) / rel
        return f if f.is_file() else None

    def handler(route, request):
        if request.method == "GET" and (f := old_static(request.url)):
            ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
            if f.suffix == ".js":
                ctype = "text/javascript"
            route.fulfill(status=200, body=f.read_bytes(), headers={"content-type": ctype, "cache-control": "no-store"})
            return
        for match, fn in mocks:
            hit = match(request) if callable(match) else (match in request.url)
            if hit:
                fn(route, request)
                return
        route.continue_()

    page.route("**/*", handler)
    return mocks
