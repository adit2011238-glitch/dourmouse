# DOURMOUSE headless server image.
#
# Packages dourmouse/webui.py's entry point — a stdlib-only
# ThreadingHTTPServer (no Flask/Django, see the module docstring) that serves
# ui/index.html and the /api/* dashboard routes. This is the SAME server
# start.sh already runs on Linux today (`exec ./.venv/bin/python -m
# dourmouse.webui`); this image just containerizes that path instead of
# needing a checked-out repo + venv on the host.
#
# Deliberately NOT installed here, and why that's safe (not a corner cut):
#   - requirements-desktop.txt (pywebview, the native macOS window shell) —
#     webui.py never imports dourmouse/desktop.py at module level, so it's
#     dead weight in a container with no display. requirements.txt happens
#     to already list pywebview too (v6.0, overlapping requirements-desktop's
#     >=5.0 pin) — installing it here is harmless, just unused.
#   - requirements-voice.txt (faster-whisper, piper-tts) — the voice module's
#     own comment says the endpoints "report NOT CONFIGURED honestly" without
#     these wheels. Omitting them here doesn't change that behavior, it just
#     doesn't build it into the image.
#   - MetaTrader5, atlas_terminal, pypdf — all try/except-import-guarded at
#     the call site (dourmouse/mt5_ops.py, dourmouse/atlas_ui_ops.py,
#     dourmouse/extract.py). Those features degrade to an honest "unavailable"
#     message exactly as they do on any machine that never installed them.
#
# NOT build-tested in the environment this Dockerfile was written in (no
# Docker daemon available there). Run `docker build -t dourmouse .` and
# `docker compose up` yourself and read the logs before trusting this.

FROM python:3.12-slim

# libgomp1: numpy/pyarrow wheels on slim Debian sometimes want it at import
# time even though pip itself needs no compiler for these (manylinux wheels).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Layer-cache the dependency install separately from the source copy below.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Only what dourmouse.webui's import chain actually needs at runtime:
# the package itself and the sibling ui/ directory it serves
# (dourmouse/webui.py resolves _UI_DIR as Path(__file__).parent.parent/"ui",
# i.e. it must sit next to dourmouse/, not inside it).
COPY dourmouse/ ./dourmouse/
COPY ui/ ./ui/

# Runtime state (session transcripts, the long-term memory SQLite/FTS5 db,
# logs) belongs on a volume, not baked into the image — see DOURMOUSE_WORKSPACE
# / DOURMOUSE_MEMORY_DB below and docker-compose.yml's volume mounts.
RUN mkdir -p /data/workspace /data/logs \
    && useradd --create-home --uid 10001 dourmouse \
    && chown -R dourmouse:dourmouse /app /data

USER dourmouse

ENV PYTHONUNBUFFERED=1 \
    DOURMOUSE_WORKSPACE=/data/workspace \
    DOURMOUSE_MEMORY_DB=/data/workspace/memory.sqlite3 \
    DOURMOUSE_UI_PORT=8765 \
    DOURMOUSE_HOST=0.0.0.0

# Binding 0.0.0.0 inside the container is fine by itself — the container's
# own port isn't reachable from outside until something (docker-compose's
# port mapping, or the host) publishes it. But per docs/tailscale.md's own
# rule, DOURMOUSE_ACCESS_TOKEN is REQUIRED wherever this actually gets
# published beyond localhost: the app prints a loud warning and serves
# anyway if you skip it (backward-compatible, not a container-specific
# check) — pass it via --env-file / -e, never bake it into this image.

EXPOSE 8765

# / is served token-free only for loopback clients (see dourmouse/config.py
# require_token()'s docstring); a healthcheck run from inside the container
# is loopback, so this works with or without DOURMOUSE_ACCESS_TOKEN set.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"DOURMOUSE_UI_PORT\",\"8765\")}/', timeout=4)" || exit 1

ENTRYPOINT ["python", "-m", "dourmouse.webui"]
