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

# Load .env if present (DOURMOUSE_* and provider keys).
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

if [ ! -d .venv ]; then
  echo "No .venv found — creating one (first run)..."
  python3 -m venv .venv
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install --quiet -r requirements.txt
fi

exec ./.venv/bin/python -m dourmouse.webui
