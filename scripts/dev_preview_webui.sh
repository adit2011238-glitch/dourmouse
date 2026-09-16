#!/usr/bin/env bash
# Dev preview of the webui frontend, isolated from the real, live daily-use
# app (which normally holds port 8765 and reads/writes the real workspace/
# directory). Runs on a different port against a throwaway workspace so
# UI/frontend changes can be tested in a real browser without touching real
# user data or colliding with an already-running instance.
set -euo pipefail
cd "$(dirname "$0")/.."

export DOURMOUSE_UI_PORT="${DOURMOUSE_UI_PORT:-18765}"
export DOURMOUSE_WORKSPACE="${DOURMOUSE_WORKSPACE:-$(pwd)/.dev-preview-workspace}"
mkdir -p "$DOURMOUSE_WORKSPACE"

exec .venv/bin/python -m dourmouse.webui
