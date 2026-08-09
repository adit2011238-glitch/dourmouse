#!/bin/bash
# =============================================================================
#  DOURMOUSE // CENTRAL AGENT DISPATCH  —  launcher (macOS, DESKTOP APP)
#
#  Double-click this file in Finder (or run: ./start.command) to boot the
#  dispatch dashboard in its own NATIVE macOS window (WebKit) — no browser
#  tab. If the native-window dependency (pywebview) is unavailable, it falls
#  back to your default browser automatically and says so.
#
#  What it does, in order:
#    1.  Finds a usable Python 3.10+ (Homebrew/system/anywhere on PATH).
#    2.  Creates a local virtualenv (.venv) and installs dependencies
#        (including the desktop extra, requirements-desktop.txt).
#    3.  On FIRST run: asks for your NVIDIA API key and writes it into .env
#        (file mode 600 — never hardcoded, never printed back, never shipped).
#    4.  Starts the app (dourmouse.desktop) in the background — it opens
#        the native DOURMOUSE window (Agent Map gets its own second window).
#
#  Stop it later by double-clicking stop.command (or killing .dourmouse-ui.pid).
# =============================================================================
set -uo pipefail
cd "$(dirname "$0")" || exit 1

PORT="${DOURMOUSE_UI_PORT:-8765}"
URL="http://127.0.0.1:${PORT}"

echo ""
echo "◈ DOURMOUSE // CENTRAL AGENT DISPATCH"
echo "  boot directory : $(pwd)"
echo ""

# --- 1. find a usable Python (>= 3.10) -------------------------------------
PY=""
for cand in \
  python3.13 python3.12 python3.11 python3.10 \
  /opt/homebrew/bin/python3 /usr/local/bin/python3 \
  /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
  python3
do
  if command -v "$cand" >/dev/null 2>&1 || [ -x "$cand" ]; then
    v="$("$cand" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
    if [ -n "$v" ]; then
      maj="${v%%.*}"; min="${v#*.}"; min="${min%%.*}"
      if [ "$maj" -ge 3 ] && [ "$min" -ge 10 ]; then PY="$cand"; break; fi
    fi
  fi
done
if [ -z "$PY" ]; then
  echo "✕ No Python 3.10+ found."
  echo "  Install Python 3.12 (https://python.org or \`brew install python@3.12\`)"
  echo "  then double-click start.command again."
  echo ""
  read -r -n1 -s -p "Press any key to close..." ; echo
  exit 1
fi
echo "  python         : $PY ($("$PY" --version 2>&1))"

# --- 2. virtualenv + dependencies ------------------------------------------
if [ ! -x ".venv/bin/python" ]; then
  echo "  creating .venv ..."
  "$PY" -m venv .venv || { echo "✕ venv creation failed."; read -r -n1 -s -p "Press any key to close..."; echo; exit 1; }
fi
echo "  installing dependencies ..."

".venv/bin/python" -m pip install --quiet --upgrade pip
".venv/bin/python" -m pip install --quiet -r requirements.txt || {
  echo "✕ dependency install failed — check your network connection."
  read -r -n1 -s -p "Press any key to close..."; echo; exit 1;
}
# Desktop extra (pywebview = native window). Non-fatal: the app falls back to
# the browser if this fails.
if [ -f requirements-desktop.txt ]; then
  ".venv/bin/python" -m pip install --quiet -r requirements-desktop.txt || \
    echo "  ⚠ desktop extra install failed — will fall back to the browser."
fi

# --- 3. first-run NVIDIA API key onboarding (Rule 2.6: never hardcode) ------
# The pasted key is validated LIVE (a real 1-token NVIDIA call via
# dourmouse.key_check) BEFORE anything is written to .env — an invalid,
# revoked, or inference-restricted key is rejected at the prompt with a
# clear reason instead of failing later with a 401/403 mid-chat.
if ! grep -qE '^NVIDIA_API_KEY=.+' .env 2>/dev/null; then
  echo ""
  echo "  ── FIRST RUN: NVIDIA API KEY REQUIRED ──────────────────────"
  echo "  Get a key at https://build.nvidia.com (free tier available)."
  echo "  It is stored ONLY in .env (mode 600) on this machine and never"
  echo "  transmitted anywhere except NVIDIA's API."
  ATTEMPT=0
  KEY_VALIDATED=""
  while [ -z "$KEY_VALIDATED" ]; do
    ATTEMPT=$((ATTEMPT+1))
    if [ "$ATTEMPT" -gt 3 ]; then
      echo "  ✕ No valid key after 3 attempts. Get one at https://build.nvidia.com and re-run."
      read -r -n1 -s -p "Press any key to close..."; echo
      exit 1
    fi
    printf '  Paste your NVIDIA_API_KEY and press Enter: '
    read -r KEY
    echo ""
    KEY="$(printf '%s' "${KEY:-}" | tr -d '[:space:]')"   # strip stray whitespace/CR
    if [ -z "$KEY" ]; then
      echo "  No key provided — aborting. Paste it into .env later and relaunch."
      read -r -n1 -s -p "Press any key to close..."; echo
      exit 1
    fi
    if [ "${#KEY}" -lt 16 ]; then
      echo "  ✕ That doesn't look like an NVIDIA key (expected 'nvapi-…'). Try again (attempt $ATTEMPT/3)."
      continue
    fi
    case "$KEY" in
      nvapi-[A-Za-z0-9._-]*) : ;;  # NVIDIA key format; chars kept sed-safe
      *) echo "  ✕ That doesn't look like an NVIDIA key (expected 'nvapi-…'). Try again (attempt $ATTEMPT/3)."
         continue ;;
    esac
    echo "  ✓ format OK — validating live against NVIDIA (1-token call)..."
    if printf '%s\n' "$KEY" | ".venv/bin/python" -m dourmouse.key_check; then
      KEY_VALIDATED=1
    else
      echo "  ✕ Live validation FAILED — key NOT saved. (attempt $ATTEMPT/3)"
    fi
  done
  OLD_UMASK="$(umask)"
  umask 177   # never world-readable, even for the instant before chmod
  if grep -qE '^NVIDIA_API_KEY=' .env 2>/dev/null; then
    sed -i.bak "s|^NVIDIA_API_KEY=.*|NVIDIA_API_KEY=$KEY|" .env && rm -f .env.bak
  else
    printf 'NVIDIA_API_KEY=%s\n' "$KEY" >> .env
  fi
  chmod 600 .env
  umask "$OLD_UMASK"  # restore BEFORE any mkdir/redirect below (Rule: no 600 dirs)
  echo "  ✓ key stored in .env (chmod 600)."
  unset KEY
fi

# --- 4. start the desktop app -----------------------------------------------
# v5.20: a dourmouse:// deep link may arrive with the launch (the applet's
# `on open location` sets DOURMOUSE_DEEP_LINK). The raw URL is ONLY ever
# forwarded — the allow-list parser in dourmouse/deeplink.py decides what
# it means. First gate here: only dourmouse:// ever reaches the server.
if [ -n "${DOURMOUSE_DEEP_LINK:-}" ]; then
  case "$DOURMOUSE_DEEP_LINK" in
    dourmouse://*) : ;;
    *) echo "  ⚠ ignoring non-dourmouse:// deep link"; DOURMOUSE_DEEP_LINK="" ;;
  esac
fi
if curl -s -o /dev/null --max-time 2 "$URL/api/roster" 2>/dev/null; then
  echo ""
  if [ -n "${DOURMOUSE_DEEP_LINK:-}" ]; then
    echo "  → forwarding deep link to the running app: $DOURMOUSE_DEEP_LINK"
    # The running window's SSE hub gets a validated `navigate` event and
    # routes itself — no browser involved, nothing executed. The body is
    # JSON-encoded by python (exact even if the URL contained quotes or
    # backslashes — never hand-spliced into the request).
    printf '%s' "$DOURMOUSE_DEEP_LINK" | ".venv/bin/python" -c '
import json, sys, urllib.request
url = sys.argv[1]
payload = json.dumps({"to": sys.stdin.read().strip()}).encode()
try:
    req = urllib.request.Request(
        url + "/api/deeplink", data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=3)
except Exception:
    pass
' "$URL" >/dev/null 2>&1 || true
  fi
  echo "  ✓ DOURMOUSE is ALREADY running at $URL — check its window."
  exit 0
fi

mkdir -p workspace
echo ""
echo "  booting dispatch core ... (log → .dourmouse-ui.log)"
DEEP_LINK_ARGS=()
if [ -n "${DOURMOUSE_DEEP_LINK:-}" ]; then
  DEEP_LINK_ARGS=(-- "$DOURMOUSE_DEEP_LINK")
  echo "  cold start with deep link: $DOURMOUSE_DEEP_LINK"
fi
nohup ".venv/bin/python" -m dourmouse.desktop "${DEEP_LINK_ARGS[@]}" > .dourmouse-ui.log 2>&1 &
echo $! > .dourmouse-ui.pid

UP=0
until curl -s -o /dev/null --max-time 1 "$URL/api/roster" 2>/dev/null; do
  UP=$((UP+1))
  if [ "$UP" -ge 40 ]; then
    echo "✕ Server did not come up in time. See .dourmouse-ui.log for details."
    tail -20 .dourmouse-ui.log
    read -r -n1 -s -p "Press any key to close..."; echo
    exit 1
  fi
  sleep 0.5
done
echo "  ✓ dispatch core online — $URL"
echo "  ✓ DOURMOUSE is opening in its desktop window."
echo "    (If the native window isn't available it falls back to the browser.)"
echo ""
echo "  Stop it any time with stop.command (or: kill \$(cat .dourmouse-ui.pid))"
echo ""
read -r -n1 -s -p "Press any key to close this window..." ; echo
exit 0
