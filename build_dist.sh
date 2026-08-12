#!/bin/bash
# =============================================================================
#  DOURMOUSE // build a self-contained distribution folder (any device)
#
#  Usage:
#    ./build_dist.sh [output_dir] [--with-voice] [--personal]
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
#
#  --personal    SINGLE-DOWNLOAD build for ONE user's own laptop (v5.22).
#                Unlike the portable build, this embeds EVERYTHING so the
#                result needs zero setup on that machine:
#                  - the full ATLAS quant engine (code + data + deliverables)
#                    at atlas/, with its requirements installed into the SAME
#                    .venv (shared interpreter — the app runs atlas directly,
#                    no second venv to build or spawn)
#                  - the real .env with all credentials (chmod 600), with
#                    ATLAS_REPO_PATH rewritten to the bundled engine and
#                    ATLAS_VENV_PATH removed (the shared venv is the runtime)
#                  - the app's workspace (sessions, memory, linked-account
#                    tokens) and dourmouse/local_secrets.py
#                Atlas source comes from $ATLAS_SRC, else ATLAS_REPO_PATH in
#                .env, else a sibling ../atlas directory.
#                WARNING: the personal build is a secrets carrier by design.
#                Treat the folder like your keys: never share or upload it.
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
OUT="$ROOT/dist"
WITH_VOICE=""
PERSONAL=""
VOICE_EXTRA=""

for arg in "$@"; do
  case "$arg" in
    --with-voice) WITH_VOICE=1 ;;
    --personal) PERSONAL=1 ;;
    *) OUT="$arg" ;;
  esac
done

STAGE="$OUT/dourmouse-dist"

echo "==> staging into $STAGE"
rm -rf "$STAGE"
mkdir -p "$STAGE"
# Absolute STAGE: the personal .env must point at the bundled atlas with a
# path that works no matter how the build was invoked.
STAGE="$(cd "$STAGE" && pwd)"

echo "==> copying source (EXPLICIT include-list — an include-list cannot"
echo "    leak .env/.venv/workspace/.git by accident; a wholesale cp can)"
# The package may live flat at <root>/dourmouse (this repo) or nested at
# <root>/dourmouse/dourmouse (older layouts). Pick whichever exists.
if [ -d "$ROOT/dourmouse/dourmouse" ]; then
  cp -R "$ROOT/dourmouse/dourmouse" "$STAGE/dourmouse"
elif [ -d "$ROOT/dourmouse" ]; then
  cp -R "$ROOT/dourmouse" "$STAGE/dourmouse"
else
  echo "error: no dourmouse package found under $ROOT" >&2
  exit 1
fi
cp -R "$ROOT/ui" "$STAGE/ui"
# tests/docs are NOT shipped — keep the dist clean; the package is self-contained.
rm -rf "$STAGE/dourmouse/tests" "$STAGE/dourmouse/__pycache__" "$STAGE/ui"/*.orig 2>/dev/null || true
# single-user source secrets NEVER ship in the PORTABLE build (the dist is
# copyable/shared; the credential stays with the builder). The PERSONAL build
# is the exception: it is explicitly one user's own laptop, so local_secrets
# (Google app passwords etc.) ride along — that is the point of --personal.
if [ -z "$PERSONAL" ]; then
  rm -f "$STAGE/dourmouse/local_secrets.py" 2>/dev/null || true
fi
# keep ui only with its assets
find "$STAGE" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true

# --- atlas engine source resolution (personal build) -----------------------
ATLAS_SRC="${ATLAS_SRC:-}"
if [ -n "$PERSONAL" ]; then
  if [ -z "$ATLAS_SRC" ] && [ -f "$ROOT/.env" ]; then
    ATLAS_SRC="$(grep -E '^ATLAS_REPO_PATH=' "$ROOT/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' || true)"
  fi
  if [ -z "$ATLAS_SRC" ] && [ -d "$ROOT/../atlas" ]; then
    ATLAS_SRC="$ROOT/../atlas"
  fi
  if [ -z "$ATLAS_SRC" ] && [ -d "$ROOT/atlas" ]; then
    ATLAS_SRC="$ROOT/atlas"
  fi
  ATLAS_SRC="${ATLAS_SRC/#\~/$HOME}"
  if [ -z "$ATLAS_SRC" ] || [ ! -d "$ATLAS_SRC" ]; then
    echo "error: --personal needs the ATLAS engine source." >&2
    echo "  set ATLAS_SRC=/path/to/atlas, or ATLAS_REPO_PATH=... in .env," >&2
    echo "  or place the repo at ../atlas next to this project." >&2
    exit 1
  fi
  echo "==> bundling ATLAS engine from $ATLAS_SRC (single self-contained download)"
  mkdir -p "$STAGE/atlas"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a \
      --exclude '.git/' --exclude '.venv*/' --exclude '__pycache__/' \
      --exclude '.pytest_cache/' --exclude '.mypy_cache/' --exclude '.ruff_cache/' \
      --exclude '*.pyc' --exclude 'tests/' \
      "$ATLAS_SRC/" "$STAGE/atlas/"
  else
    cp -R "$ATLAS_SRC"/. "$STAGE/atlas/"
    rm -rf "$STAGE/atlas/.git" "$STAGE/atlas/.venv" "$STAGE/atlas/.venv-atlas" \
           "$STAGE/atlas/tests" 2>/dev/null || true
    find "$STAGE/atlas" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
  fi
  # ship the app's own workspace (sessions, memory, linked-account tokens)
  # — the point of --personal: one user's full state on one laptop.
  if [ -d "$ROOT/workspace" ]; then
    echo "==> shipping workspace (sessions, memory, linked-account tokens)"
    cp -R "$ROOT/workspace" "$STAGE/workspace"
  fi
fi

echo "==> copying launchers + docs + requirements (explicit)"
cp "$ROOT/start.command" "$STAGE/start.command" 2>/dev/null || true
cp "$ROOT/start.sh" "$STAGE/start.sh" 2>/dev/null || true
cp "$ROOT/.env.example" "$STAGE/.env.example" 2>/dev/null || true
cp "$ROOT/UPGRADE_PLAN_02.md" "$STAGE/UPGRADE_PLAN_02.md" 2>/dev/null || true
# start.command runs `pip install -r requirements.txt` on first run in the
# portable build; the dist must ship the requirement files for that to work
# (a latent bug fixed in v5.22.1 — the stage venv was always prebuilt, but a
# missing requirements.txt made start.command fail before ever reaching it).
cp "$ROOT/requirements.txt" "$STAGE/requirements.txt" 2>/dev/null || true
cp "$ROOT/requirements-desktop.txt" "$STAGE/requirements-desktop.txt" 2>/dev/null || true
cp "$ROOT/requirements-voice.txt" "$STAGE/requirements-voice.txt" 2>/dev/null || true
cp "$ROOT/requirements-extract.txt" "$STAGE/requirements-extract.txt" 2>/dev/null || true
# strip anything that could still sneak in (tests, pycache, local secrets)
rm -rf "$STAGE/dourmouse/tests" "$STAGE/dourmouse/__pycache__" "$STAGE/ui"/*.orig 2>/dev/null || true
rm -f "$STAGE/dourmouse/local_secrets.py" 2>/dev/null || true
find "$STAGE" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true

# --- LEAK CHECK (release-blocker gate: dist must never ship user data) -----
# Portable builds run the FULL gate — workspace/.env/venv/.git/*.db are hard
# blockers. The --personal build is a documented single-user secrets carrier
# (its whole point is embedding .env + workspace), so it gates on the
# ACCIDENTAL leaks only: source secrets, session DBs, and relay configs.
LEAK_PATTERNS=(\( -name '*.db' -o -name 'schedules.jsonl' \
  -o -name 'relay_config.txt' -o -path '*/.git/*' \))
if [ -z "$PERSONAL" ]; then
  LEAK_PATTERNS=(\( -name 'local_secrets.py' -o -name '.env' \
    -o -name '*.db' -o -name 'schedules.jsonl' -o -name 'relay_config.txt' \
    -o -path '*/.git/*' -o -path '*/workspace/*' -o -name '.venv' \))
fi
LEAKS="$(find "$STAGE" "${LEAK_PATTERNS[@]}" 2>/dev/null | head -20)"
if [ -n "$LEAKS" ]; then
  echo "RELEASE BLOCKED — user data or secrets found inside the dist:"
  echo "$LEAKS"
  exit 1
fi
echo "    leak check: CLEAN (no user data / secrets shipped)"

if [ "$(uname -s)" = "Darwin" ] && [ -d "$ROOT/dourmouse.app" ]; then
  echo "==> bundling dourmouse.app (macOS)"
  cp -R "$ROOT/dourmouse.app" "$STAGE/dourmouse.app"
  rm -rf "$STAGE/dourmouse.app/Contents/_CodeSignature" 2>/dev/null || true
  # Re-sign ad-hoc (the copy's signature is stale after cp; an ad-hoc
  # signature keeps the bundle consistent). Best-effort: an unsigned bundle
  # also launches fine locally.
  xattr -cr "$STAGE/dourmouse.app" 2>/dev/null || true
  codesign --force -s - "$STAGE/dourmouse.app" 2>/dev/null || true
fi

# Pick a Python >= 3.10: requirements.txt pins openai>=2.52.0 which needs it,
# and macOS ships /usr/bin/python3 as 3.9. Prefer an interpreter that also
# satisfies the project's own venv, so the dist venv is consistent.
PYTHON_BIN=""
for candidate in "${PYTHON:-}" python3.13 python3.12 python3.11 python3.10 /opt/homebrew/bin/python3 python3; do
  [ -n "$candidate" ] || continue
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
    PYTHON_BIN="$candidate"
    break
  fi
done
if [ -z "$PYTHON_BIN" ]; then
  echo "error: need Python >= 3.10 for the dist venv (openai>=2.52.0); found $(python3 --version 2>/dev/null || echo 'none')" >&2
  exit 1
fi
echo "==> creating fresh virtualenv with $PYTHON_BIN ($("$PYTHON_BIN" --version))"
"$PYTHON_BIN" -m venv "$STAGE/.venv"

echo "==> installing dependencies (this downloads the wheels)"
# Array (not a string): requirement paths may contain spaces (e.g.
# /Volumes/ATLAS /Atlas/...), and unquoted expansion would split them.
EXTRA_REQS=()
if [ -n "${WITH_VOICE}" ] && [ -f "$ROOT/requirements-voice.txt" ]; then
  EXTRA_REQS+=(-r "$ROOT/requirements-voice.txt")
fi
# The personal build is offline-first: pywebview (the NATIVE app window) must
# ship in the venv, not be installed on first run. The portable build leaves
# it out — its start.command installs requirements-desktop.txt at first run.
if [ -n "$PERSONAL" ] && [ -f "$ROOT/requirements-desktop.txt" ]; then
  EXTRA_REQS+=(-r "$ROOT/requirements-desktop.txt")
fi
"$STAGE/.venv/bin/pip" install --quiet --upgrade pip
"$STAGE/.venv/bin/pip" install --quiet -r "$ROOT/requirements.txt" "${EXTRA_REQS[@]}"

if [ -n "$PERSONAL" ]; then
  echo "==> installing ATLAS engine deps into the SAME venv (shared interpreter — fastest atlas)"
  ATLAS_LOCK="$STAGE/atlas/requirements.lock"
  ATLAS_REQ="$STAGE/atlas/requirements.txt"
  if [ -f "$ATLAS_LOCK" ]; then
    if ! "$STAGE/.venv/bin/pip" install --quiet -r "$ATLAS_LOCK"; then
      echo "  ⚠ atlas requirements.lock install failed — falling back to requirements.txt"
      "$STAGE/.venv/bin/pip" install --quiet -r "$ATLAS_REQ"
    fi
  elif [ -f "$ATLAS_REQ" ]; then
    "$STAGE/.venv/bin/pip" install --quiet -r "$ATLAS_REQ"
  else
    echo "  ⚠ no requirements.txt under $STAGE/atlas — atlas will report NOT CONFIGURED"
  fi
  # The venv is fully provisioned: start.command skips pip when this marker
  # exists, so the personal app boots fast and offline.
  touch "$STAGE/.deps-installed"
fi

echo "==> precompiling bytecode (faster first run)"
"$STAGE/.venv/bin/python" -m compileall -q "$STAGE/dourmouse" "$STAGE/atlas" 2>/dev/null || true

# --- credentials + INSTALL.md (personal vs portable) -----------------------
if [ -n "$PERSONAL" ]; then
  if [ -f "$ROOT/.env" ]; then
    echo "==> shipping personal .env (credentials preloaded — PERSONAL build)"
    cp "$ROOT/.env" "$STAGE/.env"
    chmod 600 "$STAGE/.env"
    # Point ATLAS_REPO_PATH at the BUNDLED engine (absolute), drop the
    # external ATLAS_VENV_PATH: the shared dist venv runs atlas itself.
    if grep -q '^ATLAS_REPO_PATH=' "$STAGE/.env"; then
      sed -i.bak "s|^ATLAS_REPO_PATH=.*|ATLAS_REPO_PATH=$STAGE/atlas|" "$STAGE/.env" && rm -f "$STAGE/.env.bak"
    else
      printf 'ATLAS_REPO_PATH=%s\n' "$STAGE/atlas" >> "$STAGE/.env"
    fi
    sed -i.bak '/^ATLAS_VENV_PATH=/d' "$STAGE/.env" && rm -f "$STAGE/.env.bak" || true
  else
    echo "  ⚠ --personal but no .env found — shipping .env.example only"
  fi
  cat > "$STAGE/INSTALL.md" <<'DOC'
# DOURMOUSE — PERSONAL build (preloaded for this laptop)

Everything is already inside this folder — there is nothing to configure:

- the app (dourmouse package + ui + bundled virtualenv)
- your credentials (.env, chmod 600)
- your workspace (sessions, memory, linked-account tokens)
- the ATLAS quant engine, embedded at atlas/ with its FX data archive and
  written research — the HUD's ATLAS panel is live immediately, offline

Run it:
- double-click dourmouse.app — it boots the core silently and opens DOURMOUSE
  in its own NATIVE macOS window (no Terminal, no browser tab). The app
  appears in the Dock with its own icon, exactly like Chrome or Spotify.
- or run start.command from a terminal.

Make it feel fully installed (one-time, keeps the app together with its
engine folder — the .app launches the folder's venv/ATLAS engine):
  cp -R "$(pwd)" /Applications/dourmouse-dist
  open /Applications/dourmouse-dist/dourmouse.app
DourMouse then shows in Launchpad and Spotlight like any other app (it is a
real macOS .app bundle with its own icon). Drag dourmouse.app to the Dock
to pin it there.

The ATLAS engine runs on the app's own interpreter (its requirements were
installed into the same .venv at build time) — no second venv, no setup.
The native window needs pywebview, which is preinstalled in this build.

SECURITY: this folder contains live credentials BY DESIGN (single-user).
Never zip/upload/share it. If you move it, the whole folder must move as one.
DOC
else
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
  - **Google sign-in (optional):** `GOOGLE_CLIENT_ID` + `GOOGLE_CLIENT_SECRET` from a
    Google Cloud Console OAuth *Web client* — the login page then shows
    SIGN IN WITH GOOGLE and each account gets its own watchlist/alerts/prefs.
    Register the callback URL `http://127.0.0.1:8765/api/auth/google/callback`.
  - **ATLAS (optional):** `ATLAS_REPO_PATH` + `ATLAS_VENV_PATH` to the real
    ATLAS engine — or build a `--personal` dist, which embeds it.

## 3. Run
- **macOS:** double-click `start.command` (or `dourmouse.app`), or `./start.sh`
- **Linux:** `./start.sh` (binds 127.0.0.1:8765)
- Open http://127.0.0.1:8765 — done.
- **Deep links:** the app registers the `dourmouse://` scheme, so links like
  `dourmouse://world` or `dourmouse://atlas/research` open the right screen
  (or start the app on a cold launch).

## 4. Any device?
- The folder is self-contained: zip it (`zip -r dourmouse-dist.zip dourmouse-dist`),
  copy to the target, repeat steps 1-3 there. The UI is a web page, so you can
  also run the server on one machine and open it from a phone/tablet on the
  same network (set `DOURMOUSE_HOST=0.0.0.0` + `DOURMOUSE_ACCESS_TOKEN`).

## What's NOT shipped (on purpose)
- Ollama models (multi-GB) — pulled on first run.
- Secrets — only `.env.example` ships; `.env` is yours.
- The ATLAS engine — point ATLAS_REPO_PATH at your own copy, or build --personal.
DOC
fi

echo ""
echo "==> DONE. Dist folder: $STAGE"
echo "    zip -r ${STAGE}.zip $STAGE"
du -sh "$STAGE" 2>/dev/null || true
