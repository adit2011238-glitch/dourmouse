#!/usr/bin/env bash
# setup.sh — one-command onboarding for the dourmouse commercial stack (macOS/Linux).
# Windows counterpart: atlas-strategy-lab/setup.bat
#
# 1. checks Python (>=3.10), 2. creates .venv, 3. installs the exact CI
# dependency set, 4. creates config from .env.example, 5. runs the test suite
# as the done-gate. Non-destructive: existing .venv / .env are left alone.
set -euo pipefail

cd "$(dirname "$0")"

echo "=== [1/5] python ==="
PY=""
if command -v python3.12 >/dev/null 2>&1; then PY=python3.12
elif command -v python3.11 >/dev/null 2>&1; then PY=python3.11
elif command -v python3 >/dev/null 2>&1; then PY=python3
else echo "FAILED: no python3 on PATH"; exit 1; fi
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' \
  || { echo "FAILED: python >= 3.10 required (have $("$PY" --version))"; exit 1; }
echo "using $("$PY" --version) ($PY)"

echo "=== [2/5] virtualenv ==="
if [ ! -x .venv/bin/python ]; then
  "$PY" -m venv .venv
  echo "created .venv"
else
  echo ".venv already present (left alone)"
fi
.venv/bin/python -m pip install --upgrade pip --quiet

echo "=== [3/5] dependencies (CI set) ==="
.venv/bin/python -m pip install -q \
  -r requirements.txt \
  -r requirements-dev.txt \
  -r requirements-extract.txt
.venv/bin/python -m pip install -q pywebview 2>/dev/null || true
echo "dependencies installed"

echo "=== [4/5] config ==="
if [ ! -f .env ]; then
  cp .env.example .env
  echo "created .env from .env.example — EDIT IT with your tokens before going live"
else
  echo ".env already present (left alone)"
fi

echo "=== [5/5] done-gate: test suite ==="
.venv/bin/python -m pytest dourmouse/tests -q --tb=short
echo
echo "SETUP COMPLETE. Next:"
echo "  .venv/bin/python dourmouse/webui.py        # run the app"
echo "  atlas-strategy-lab/scripts/health_check.py # stack smoke test"
