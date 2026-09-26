"""The shell's own handshake: the one route every OS shell page can call."""

from __future__ import annotations

from typing import Any

from . import Request, route


@route("GET", "/api/os/ping")
def ping(req: Request) -> tuple[int, dict[str, Any]]:
    """Proves the plug-in router is wired, lists what it serves, and names any
    backend module that failed to import (with the reason)."""
    from . import failed, routes

    return 200, {"ok": True, "routes": [f"{m} {p}" for m, p in routes()], "failed_modules": failed()}
