#!/usr/bin/env bash
# Launch the ATLAS Terminal (streamlit) with the dourmouse venv.
# Usage: ./start_atlas_ui.sh            (default port 8501)
#        PORT=8510 ./start_atlas_ui.sh  (custom port)
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "creating .venv + installing deps (first run)..."
  python -m venv .venv
  ./.venv/Scripts/python -m pip install -q -r requirements.txt -r requirements-atlas-ui.txt
fi

PORT="${PORT:-8501}"
echo "ATLAS TERMINAL -> http://127.0.0.1:${PORT}  (FOREX_DATA_PATH=${FOREX_DATA_PATH:-unset})"
exec ./.venv/Scripts/python -m streamlit run atlas_terminal/atlas_terminal.py \
  --server.port "${PORT}" --server.headless true --server.address 127.0.0.1
