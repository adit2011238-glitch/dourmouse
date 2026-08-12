#!/bin/bash
# =============================================================================
#  DOURMOUSE // build a self-contained distribution folder (any device)
#
#  Usage:
#    ./build_dist.sh [output_dir] [--with-voice]
#
#  Output:  <output_dir>/dourmouse-dist/  — a folder you can zip, copy to any
#  Mac/Linux box, and run. It contains:
#    - dourmouse/         the Python package
#    - ui/                the HUD / map / agent windows
#    - .venv/             a full Python virtualenv with all deps
#    - .env.example       template (copy to .env, fill in keys)
#    - start.command      macOS launcher (double-click)
#    - start.sh           Linux launcher
#    - dourmouse.app      macOS double-clickable app (when on macOS)
#    - INSTALL.md         first-run instructions
#
#  The first run on the target machine still needs Ollama running with the
#  model pulled (see INSTALL.md) — the app is portable, the model is not
#  shipped inside it (multi-GB).
#
#  --with-voice  also installs faster-whisper + piper (adds ~1 GB to .venv).
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$ROOT/dist}"
WITH_VOICE="${2:-}"
STAGE="$OUT/dourmouse-dist"
VOICE_EXTRA=""

echo "==> staging into $STAGE"
rm -rf "$STAGE"
mkdir -p "$STAGE"

echo "==> copying source (EXPLICIT include-list — an include-list cannot"
echo "    leak .env/.venv/workspace/.git by accident; a wholesale cp can)"
cp -R "$ROOT/dourmouse/dourmouse" "$STAGE/dourmouse"
cp -R "$ROOT/ui" "$STAGE/ui"
# launchers + docs + requirements (explicit)
cp "$ROOT/start.command" "$STAGE/start.command" 2>/dev/null || true
cp "$ROOT/start.sh" "$STAGE/start.sh" 2>/dev/null || true
cp "$ROOT/.env.example" "$STAGE/.env.example" 2>/dev/null || true
cp "$ROOT/UPGRADE_PLAN_02.md" "$STAGE/UPGRADE_PLAN_02.md" 2>/dev/null || true
cp "$ROOT/requirements.txt" "$STAGE/requirements.txt" 2>/dev/null || true
cp "$ROOT/requirements-extract.txt" "$STAGE/requirements-extract.txt" 2>/dev/null || true
# strip anything that could still sneak in (tests, pycache, local secrets)
rm -rf "$STAGE/dourmouse/tests" "$STAGE/dourmouse/__pycache__" "$STAGE/ui"/*.orig 2>/dev/null || true
rm -f "$STAGE/dourmouse/local_secrets.py" 2>/dev/null || true
find "$STAGE" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true

echo "==> LEAK CHECK (release-blocker gate: dist must never ship user data)"
LEAKS="$(find "$STAGE" \( -name 'local_secrets.py' -o -name '.env' \
  -o -name '*.db' -o -name 'schedules.jsonl' -o -name 'relay_config.txt' \
  -o -path '*/.git/*' -o -path '*/workspace/*' -o -name '.venv' \) 2>/dev/null | head -20)"
if [ -n "$LEAKS" ]; then
  echo "RELEASE BLOCKED — user data or secrets found inside the dist:"
  echo "$LEAKS"
  exit 1
fi
echo "    leak check: CLEAN (no .env / workspace / .venv / .git / *.db shipped)"

if [ "$(uname -s)" = "Darwin" ] && [ -d "$ROOT/dourmouse.app" ]; then
  echo "==> bundling dourmouse.app (macOS)"
  cp -R "$ROOT/dourmouse.app" "$STAGE/dourmouse.app"
  rm -rf "$STAGE/dourmouse.app/Contents/_CodeSignature" 2>/dev/null || true
fi

echo "==> creating fresh virtualenv"
python3 -m venv "$STAGE/.venv"

echo "==> installing dependencies (this downloads the wheels)"
if [ -n "${WITH_VOICE}" ]; then
  VOICE_EXTRA=" -r $ROOT/requirements-voice.txt"
fi
"$STAGE/.venv/bin/pip" install --quiet --upgrade pip
"$STAGE/.venv/bin/pip" install --quiet -r "$ROOT/requirements.txt" $VOICE_EXTRA

cat > "$STAGE/INSTALL.md" <<'DOC'
# DOURMOUSE — install on a new device (5 minutes)

## 1. Prerequisite: Ollama (free, local LLM)
- Install from https://ollama.com (or `curl -fsSL https://ollama.com/install.sh | sh` on Linux)
- Pull the models (one-time, a few GB):
  - `ollama pull qwen3:8b`   (main brain)
  - `ollama pull qwen3:4b`   (fast dispatch; skip to save space — see .env)
- Keep Ollama running (the app talks to http://127.0.0.1:11434).

## 2. Configure
- `cp .env.example .env` then edit:
  - `DOURMOUSE_LLM_BACKEND=ollama` (default, free) — or set NVIDIA/DEEPSEEK/CODEX keys
  - Optional: `DOURMOUSE_VOICE=1` + whisper/piper settings for local voice
  - Optional: `GOOGLE_GMAIL_USER` + `GOOGLE_GMAIL_APP_PASSWORD` for Gmail tools

## 3. Run
- **macOS:** double-click `start.command` (or `dourmouse.app`), or `./start.sh`
- **Linux:** `./start.sh` (binds 127.0.0.1:8765)
- Open http://127.0.0.1:8765 — done.

## 4. Any device?
- The folder is self-contained: zip it (`zip -r dourmouse-dist.zip dourmouse-dist`),
  copy to the target, repeat steps 1-3 there. The UI is a web page, so you can
  also run the server on one machine and open it from a phone/tablet on the
  same network (set `DOURMOUSE_HOST=0.0.0.0` + `DOURMOUSE_ACCESS_TOKEN`).

## What's NOT shipped (on purpose)
- Ollama models (multi-GB) — pulled on first run.
- Secrets — only `.env.example` ships; `.env` is yours.
DOC

echo ""
echo "==> DONE. Dist folder: $STAGE"
echo "    zip -r ${STAGE}.zip $STAGE"
du -sh "$STAGE" 2>/dev/null || true
