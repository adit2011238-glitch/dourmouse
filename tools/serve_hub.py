#!/usr/bin/env python3
"""serve_hub.py — serve the dourmouse UI shell (hub.html + existing pages).

dourmouse is the shell; ATLAS and the TAILSCALE FEED live inside it as
separate large-scale UIs. This serves the ui/ folder (hub.html, index.html,
agent.html, map.html, login.html) on one port so the whole shell is one URL.

The hub page itself talks directly to:
    ATLAS  -> http://127.0.0.1:8790  (engine_api.py — backtest service)
    FEED   -> http://127.0.0.1:8788  (chat_feed.py — live relay chat)

Run:  python tools/serve_hub.py [--port 8791] [--bind 127.0.0.1]
"""
from __future__ import annotations

import argparse
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

UI = Path(__file__).resolve().parent.parent / "ui"
TOKEN = os.environ.get("HUB_ENGINE_TOKEN", "")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(UI), **kw)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def log_message(self, *a):  # keep the console quiet
        pass

    def do_GET(self):  # inject the engine token into the hub page only
        if self.path in ("/hub.html", "/"):
            try:
                raw = (UI / "hub.html").read_bytes()
                body = raw.replace(b"__ENGINE_TOKEN__", TOKEN.encode())
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            except OSError:
                pass
        super().do_GET()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("HUB_PORT", 8791)))
    ap.add_argument("--bind", default=os.environ.get("HUB_BIND", "127.0.0.1"))
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"DOURMOUSE shell on http://{args.bind}:{args.port}  (hub: /hub.html)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("stopped")


if __name__ == "__main__":
    main()
