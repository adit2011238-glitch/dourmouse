"""tv_webhook_server.py — standalone, tunnel-facing TradingView webhook.

WHY a separate process: the HUD webui (port 8765) is a full agent console
(chat, file tools, uploads). Pointing a public tunnel at it would expose
ALL of that to the internet. This listener serves ONLY the TradingView
webhook on its own port, so a cloudflared/ngrok tunnel exposes exactly one
endpoint and nothing else. The secret gate (TV_WEBHOOK_SECRET) is enforced
here just like the /api/tv-webhook route on the webui.

  python -m dourmouse.tv_webhook_server [--port 8766]

Endpoints:
  POST /  -> handle_tv_webhook (validate -> signals log -> paper log -> bus)
  GET  /health -> {"ok": true}

Signals land in workspace/tv_signals.jsonl and are applied to the seasonal
paper log (FOREX_DATA_PATH/reports/paper_log.csv) via route_to_paper.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from dourmouse.message_bus import get_message_bus
from dourmouse.tradingview_ops import handle_tv_webhook

_DEFAULT_PORT = 8766
_MAX_BODY = 256 * 1024


class _Handler(BaseHTTPRequestHandler):
    server_version = "DourmouseTvWebhook/1.0"

    def log_message(self, fmt, *args):  # quieter
        pass

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        raw_len = self.headers.get("Content-Length", "0") or "0"
        try:
            length = int(raw_len)
        except (TypeError, ValueError):
            self._send_json({"ok": False, "error": "invalid Content-Length"}, 400)
            return
        if length < 0 or length > _MAX_BODY:
            self._send_json({"ok": False, "error": "webhook body too large"}, 400)
            return
        body = self.rfile.read(length) if length else b""
        ctype = self.headers.get("Content-Type", "") or ""
        result = handle_tv_webhook(body, ctype, bus=get_message_bus())
        self._send_json(result, status=200 if result.get("ok") else 400)

    def do_GET(self):  # noqa: N802
        if self.path in ("/", "/health"):
            self._send_json({"ok": True, "service": "tv-webhook"})
        else:
            self._send_json({"ok": False, "error": "not found"}, 404)


def _load_env() -> None:
    """Load .env into os.environ (setdefault — real env wins) so the
    standalone listener sees TV_WEBHOOK_SECRET + FOREX_DATA_PATH exactly
    like the webui does at startup."""
    import os
    from pathlib import Path

    env_path = Path(__file__).resolve().parent.parent / ".env"
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    _load_env()
    args = list(argv if argv is not None else __import__("sys").argv[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT)
    parsed, _ = parser.parse_known_args(args)
    server = ThreadingHTTPServer(("127.0.0.1", parsed.port), _Handler)
    print(f"tv-webhook listening on 127.0.0.1:{parsed.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
