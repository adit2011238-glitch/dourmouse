"""Day counts of verified completion (plan PL-07, finding 208).

``GET /api/os/verification[?hours=24]``: how many state-changing tool calls were Verified, Not checked or Failed
in the window, per status and per tool. Read only. The Security and Office Activity views show these counts; the
per-step badge is on the chat tool chip.
"""

from __future__ import annotations

import time
from typing import Any

from dourmouse import verify

from . import ApiError, Request, route


@route("GET", "/api/os/verification")
def counts(req: Request) -> tuple[int, dict[str, Any]]:
    raw = req.arg("hours") or "24"
    try:
        hours = float(raw)
    except ValueError:
        raise ApiError(400, "hours must be a number") from None
    if not 0 < hours <= 24 * 30:
        raise ApiError(400, "hours must be between 0 and 720")
    log = getattr(req.server, "office_log", None)
    since = time.time() - hours * 3600
    events = log.events_since_time(since, "action.verified") if log is not None else []
    return 200, {"ok": True, "hours": hours, **verify.day_counts(events, since)}
