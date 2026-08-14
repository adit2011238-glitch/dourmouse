#!/usr/bin/env python3
"""Continuous upstream-push watcher for the local dourmouse checkout.

Polls the real remote (github.com/adit2011238-glitch/dourmouse) HEAD every
``--interval`` seconds (default 10) via ``git ls-remote`` — a lightweight,
rate-limit-free network call that never touches the working tree. The
instant the upstream HEAD differs from the last-known value it:

1. triggers the merge-safe sync (tools/sync_dourmouse.py) — fetch + merge
   + reinstall changed requirements + run the integration tests,
2. notifies the RUNNING dourmouse webui (POST /api/push-notify, loopback)
   so the push appears in the AGENT COMMS panel immediately, and
3. appends every detection to ``workspace/push_events.log``.

The last-known upstream head is persisted to
``workspace/upstream_head.txt`` so a restart never re-triggers on the
same commit. Transient network failures back off (30s) and are logged,
never fatal — the loop keeps running (Rule 2.1: honest, observable).

Run:  python tools/watch_dourmouse.py [--interval 10] [--webui-port 8765]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"  # Windows
if not VENV_PY.is_file():
    VENV_PY = ROOT / ".venv" / "bin" / "python"      # POSIX
WORKSPACE = ROOT / "workspace"
STATE_FILE = WORKSPACE / "upstream_head.txt"
EVENTS_LOG = WORKSPACE / "push_events.log"
SYNC_TOOL = ROOT / "tools" / "sync_dourmouse.py"
PID_FILE = WORKSPACE / "watch_dourmouse.pid"


def _log_event(line: str) -> None:
    try:
        WORKSPACE.mkdir(parents=True, exist_ok=True)
        with EVENTS_LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().isoformat(timespec='seconds')}  {line}\n")
    except OSError as exc:
        print(f"(push_events.log not writable: {exc})")
    print(f"[watch] {line}", flush=True)


def upstream_head() -> str | None:
    """Real remote HEAD sha, or None on transient failure."""
    try:
        out = subprocess.run(
            ["git", "ls-remote", "origin", "HEAD"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=20,
            check=False,
        )
        if out.returncode != 0:
            return None
        return out.stdout.strip().split()[0] if out.stdout.strip() else None
    except (subprocess.TimeoutExpired, OSError):
        return None


def last_known() -> str:
    try:
        return STATE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def save_known(sha: str) -> None:
    try:
        WORKSPACE.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(sha + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"(state file not writable: {exc})")


def notify_webui(old: str, new: str, webui_port: int) -> str:
    """POST to the running webui so the push shows in AGENT COMMS."""
    body = {
        "from": "watchdog",
        "subject": "UPSTREAM PUSH DETECTED",
        "body": f"dourmouse upstream pushed: {old[:8]} -> {new[:8]}. "
                f"Merge-safe sync triggered; check sync_log.txt for the result.",
    }
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{webui_port}/api/push-notify",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return f"notified webui (HTTP {resp.status})"
    except Exception as exc:  # noqa: BLE001 -- notify must never kill the loop
        return f"webui notify failed (reported honestly): {exc}"


def run_sync() -> str:
    """Trigger the merge-safe sync; returns a short honest summary."""
    try:
        out = subprocess.run(
            [str(VENV_PY), str(SYNC_TOOL)],
            cwd=str(ROOT), capture_output=True, text=True, timeout=1200,
            check=False,
        )
        tail = (out.stdout or out.stderr or "").strip().splitlines()
        summary = " · ".join(tail[-2:]) if tail else f"exit {out.returncode}"
        return f"sync exit={out.returncode} | {summary}"
    except Exception as exc:  # noqa: BLE001 -- honest failure surface
        return f"sync failed (reported honestly): {exc}"


def _already_running() -> bool:
    """True when another watcher process is alive (single-instance guard).

    Makes the scheduled task self-healing: if a watcher is already up, a
    redundant launch exits 0 immediately; if it died, the next task tick
    starts a fresh one. Stale pid files never block (checked against a
    live process, and rewritten on every successful start).
    """
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return False
    if pid <= 0:
        return False
    # Windows has no signal-0 probe; ask the OS directly whether the pid
    # is a live process (PROCESS_QUERY_LIMITED_INFORMATION, no side effects).
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--interval", type=int, default=10,
                    help="seconds between remote HEAD polls (default 10)")
    ap.add_argument("--webui-port", type=int, default=8765,
                    help="dourmouse webui port to notify (default 8765)")
    ap.add_argument("--single-instance", action="store_true",
                    help="exit 0 immediately if another watcher is already running")
    args = ap.parse_args()

    if args.single_instance and _already_running():
        print("[watch] another watcher is already running — exiting (self-healing tick)")
        return 0

    interval = max(2, args.interval)
    try:
        WORKSPACE.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(__import__("os").getpid()) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"(pid file not writable: {exc})")
    known = last_known()
    _log_event(f"watcher started (interval={interval}s, last-known={known[:8] or 'none'})")

    while True:
        head = upstream_head()
        if head is None:
            _log_event("upstream poll failed (transient) — backing off 30s")
            time.sleep(30)
            continue
        if head != known:
            if known:
                _log_event(f"UPSTREAM PUSH {known[:8]} -> {head[:8]} — triggering sync")
                sync_summary = run_sync()
                _log_event(f"  {sync_summary}")
                notify = notify_webui(known, head, args.webui_port)
                _log_event(f"  {notify}")
            else:
                # First run: just record the current head, do not sync.
                _log_event(f"initial upstream head recorded: {head[:8]}")
            known = head
            save_known(known)
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
