#!/bin/bash
# =============================================================================
#  DOURMOUSE // CENTRAL AGENT DISPATCH  —  stop script (macOS)
#
#  Double-click to stop the running dispatch dashboard. Safe to run even if
#  the server is not currently running.
# =============================================================================
cd "$(dirname "$0")" || exit 1

echo "◈ DOURMOUSE // stopping dispatch core ..."

if [ -f .dourmouse-ui.pid ]; then
  PID="$(cat .dourmouse-ui.pid)"
  if kill -0 "$PID" 2>/dev/null; then
    kill "$PID" 2>/dev/null
    sleep 1
    if kill -0 "$PID" 2>/dev/null; then
      echo "  force-stopping pid $PID ..."
      kill -9 "$PID" 2>/dev/null
    fi
    echo "  ✓ dispatch core stopped (pid $PID)."
  else
    echo "  stale pid file (process $PID not running) — cleaning up."
  fi
  rm -f .dourmouse-ui.pid
else
  echo "  no pid file found — nothing to stop."
fi

PORT="${DOURMOUSE_UI_PORT:-8765}"
if curl -s -o /dev/null --max-time 2 "http://127.0.0.1:${PORT}/api/roster" 2>/dev/null; then
  echo "  ⚠ still responding on port ${PORT}; stopping whatever is listening ..."
  lsof -ti tcp:${PORT} 2>/dev/null | xargs kill -9 2>/dev/null || true
  sleep 1
fi
echo ""
read -r -n1 -s -p "Press any key to close..." ; echo
exit 0
