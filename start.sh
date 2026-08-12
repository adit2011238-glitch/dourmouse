#!/bin/bash
# DOURMOUSE // Linux / generic launcher
# Usage: ./start.sh   (binds http://127.0.0.1:8765 by default)
set -euo pipefail
cd "$(dirname "$0")"

# Auto-sync upstream changes (merge-safe; never clobbers local work).
# Failures are logged to sync_log.txt and never block startup.
if [ -f tools/sync_dourmouse.py ]; then
  echo "checking upstream for updates..."
  (./.venv/bin/python tools/sync_dourmouse.py || \
   python3 tools/sync_dourmouse.py || true) 2>/dev/null || true
fi

# v8.2: continuous upstream-push watcher (single-instance, self-healing).
# If one is already running the tick exits 0 immediately; if it died this
# starts a fresh one. Logs to workspace/watch_dourmouse.log + push_events.log.
if [ -f tools/watch_dourmouse.py ]; then
  (./.venv/bin/python tools/watch_dourmouse.py --interval 10 --single-instance \
   >> workspace/watch_dourmouse.log 2>&1 || \
   python3 tools/watch_dourmouse.py --interval 10 --single-instance \
   >> workspace/watch_dourmouse.log 2>&1 || true) & disown
fi

# Load .env if present (DOURMOUSE_* and provider keys).
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

# v8.4: standalone TradingView webhook listener (port 8766) + cloudflared
# tunnel. The listener serves ONLY the webhook — never expose the full HUD.
# Cloudflared quick tunnels need no account; the URL changes each start, so
# update TV_PUBLIC_URL in .env after starting.
if [ -x "$DOURMOUSE_CLOUDFLARED" ] && [ -n "${TV_WEBHOOK_SECRET:-}" ]; then
  (./.venv/bin/python -m dourmouse.tv_webhook_server --port 8766 \
   >> workspace/tv_webhook.log 2>&1 || true) & disown
  ("$DOURMOUSE_CLOUDFLARED" tunnel --url http://127.0.0.1:8766 --no-autoupdate \
   >> workspace/cloudflared.log 2>&1 || true) & disown
fi

if [ ! -d .venv ]; then
  echo "No .venv found — creating one (first run)..."
  python3 -m venv .venv
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install --quiet -r requirements.txt
fi

exec ./.venv/bin/python -m dourmouse.webui
