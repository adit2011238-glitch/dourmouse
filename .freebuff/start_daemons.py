#!/usr/bin/env python3
"""Start the laptop relay daemons fully detached (new sessions, own logs).

Why not launchd: macOS System Policy (sandbox) denies launchd-spawned
processes file access to /Volumes/ATLAS ... (kernel denials on
relay_config.txt / bridge.log). Plain nohup & disown gets reaped by the
Freebuff runner (process group kill). start_new_session=True detaches into
a fresh session, which survives both.

Re-run this after every Freebuff restart. Logs: .freebuff/bridge.log,
.freebuff/chat_feed.log.
"""
import os
import subprocess
import sys

PROJ = "/Volumes/ATLAS /Atlas/dourmouse-4.0.0"
REPO = os.path.join(PROJ, "atlas-strategy-lab")
LOGD = os.path.join(PROJ, ".freebuff")


def _cfg(key: str) -> str:
    cfg = os.path.join(REPO, "relay", "relay_config.txt")
    for line in open(cfg, encoding="utf-8"):
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {cfg}")


TOKEN = _cfg("TOKEN")
RELAY = _cfg("RELAY_URL")

JOBS = [
    (["relay/agent_bridge.py", "--relay", RELAY, "--token", TOKEN,
      "--me", "laptop-dourmouse"],
     os.path.join(LOGD, "bridge.log")),
    (["relay/chat_feed.py", "--relay", RELAY, "--token", TOKEN,
      "--me", "laptop-dourmouse", "--port", "8789",
      "--send-token", "laptop-dash-2026"],
     os.path.join(LOGD, "chat_feed.log")),
]

for args, log in JOBS:
    logf = open(log, "a")
    p = subprocess.Popen([sys.executable] + args, cwd=REPO,
                         stdin=subprocess.DEVNULL, stdout=logf,
                         stderr=subprocess.STDOUT, start_new_session=True)
    print(f"started {os.path.basename(args[0])} pid={p.pid} log={log}")
