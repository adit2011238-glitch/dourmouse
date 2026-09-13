#!/bin/bash
# Stage E of the desktop-shell migration (see
# ~/.claude/plans/sorted-wiggling-pearl.md). Builds the FRESH, real Python
# virtualenv electron-builder's `extraResources` config bundles into the
# packaged .app as Contents/Resources/.venv — the same real, from-scratch
# venv-plus-pip-install approach build_dist.sh's own staging already uses,
# just producing its output where electron-builder expects to find it
# instead of a dourmouse-dist/ folder.
#
# Deliberately reuses base + desktop requirements as-is (not a pruned
# "electron-only" requirements file): pywebview/pystray ride along unused
# under this shell (Electron replaces both), a real but small size
# tradeoff accepted in favor of one requirements set to maintain instead
# of three. pyobjc-framework-ApplicationServices (Phase 2's real AX app
# control, dourmouse/app_control_ax.py) is genuinely needed regardless of
# shell and ships either way.
#
# Usage: electron/scripts/prepare-venv.sh
# Output: electron/build/venv-stage/.venv (a real, complete, pip-installed venv)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
STAGE="$ROOT/electron/build/venv-stage"

echo "==> staging venv build at $STAGE"
rm -rf "$STAGE"
mkdir -p "$STAGE"

# Same Python-version search build_dist.sh already uses: >=3.10 (this
# project's requirements.txt pins openai>=2.52.0, which needs it; macOS
# ships /usr/bin/python3 as 3.9).
PYTHON_BIN=""
for candidate in "${PYTHON:-}" python3.13 python3.12 python3.11 python3.10 /opt/homebrew/bin/python3 python3; do
  [ -n "$candidate" ] || continue
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
    PYTHON_BIN="$candidate"
    break
  fi
done
if [ -z "$PYTHON_BIN" ]; then
  echo "error: need Python >= 3.10 for the bundled venv; found $(python3 --version 2>/dev/null || echo 'none')" >&2
  exit 1
fi

echo "==> creating fresh virtualenv with $PYTHON_BIN ($("$PYTHON_BIN" --version))"
"$PYTHON_BIN" -m venv "$STAGE/.venv"

echo "==> installing dependencies (this downloads the wheels)"
"$STAGE/.venv/bin/pip" install --quiet --upgrade pip
"$STAGE/.venv/bin/pip" install --quiet \
  -r "$ROOT/requirements.txt" \
  -r "$ROOT/requirements-desktop.txt"

echo "==> precompiling bytecode (faster first run)"
"$STAGE/.venv/bin/python" -m compileall -q "$ROOT/dourmouse" 2>/dev/null || true

# Same release-blocker leak-check discipline build_dist.sh's own gate
# enforces — a bundled venv (site-packages, pip caches) has no business
# containing any of this, but real user data has leaked into unexpected
# places before in this project's history; check, don't assume.
LEAKS="$(find "$STAGE" \( -name '.env' -o -name 'local_secrets.py' -o -path '*/.git/*' \) 2>/dev/null | head -20)"
if [ -n "$LEAKS" ]; then
  echo "BUILD BLOCKED — user data or secrets found inside the venv stage:" >&2
  echo "$LEAKS" >&2
  exit 1
fi
echo "    leak check: CLEAN"

echo "==> venv ready: $STAGE/.venv"
du -sh "$STAGE/.venv" 2>/dev/null || true
