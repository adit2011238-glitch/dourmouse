"""Backend for the OS shell's ATLAS screen, the WORLD MONITOR (finding #146).

Not the quant lab: that is /api/atlas*. This one route only re-polls the
public feeds the world monitor already reads.

* ``POST /api/os/world/refresh``: ``world_pulse_snapshot(force=True)``, the
  one call no existing route exposed. It fans out to every registered source
  (up to about 10 seconds for a slow one), so it is throttled: a second call
  inside ``MIN_GAP`` seconds is refused, and so is a call while another
  refresh is running. A refusal is HTTP 200 with ``ok: false`` and a plain
  reason (the shell client turns that into an error carrying the reason), not
  a 4xx, because a browser logs every 4xx as a console error and a throttle
  is an ordinary answer. The answer has
  the same shape as ``GET /api/world/pulse``. Read-only toward the machine:
  it only makes outbound requests to the public feeds.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from . import Request, route

MIN_GAP = 15.0
_lock = threading.Lock()
_last = {"at": 0.0}


@route("POST", "/api/os/world/refresh")
def refresh(req: Request) -> tuple[int, dict[str, Any]]:
    if not _lock.acquire(blocking=False):
        return 200, {"ok": False, "busy": True, "error": "A refresh is already running. Wait for it to finish."}
    try:
        wait = MIN_GAP - (time.monotonic() - _last["at"])
        if _last["at"] and wait > 0:
            return 200, {"ok": False, "throttled": True, "error": f"The feeds were re-polled {int(MIN_GAP - wait)} seconds ago. Try again in {int(wait) + 1} seconds."}
        from dourmouse.world_pulse import world_pulse_snapshot

        snap = world_pulse_snapshot(force=True)
        _last["at"] = time.monotonic()
        return 200, {"ok": True, **snap}
    finally:
        _lock.release()
