"""Real, live control of God's Eye View's 3D globe from a Dourmouse
directive (v13).

God's Eye View (gods-eye-view/, github.com/bilawalsidhu/gods-eye-view) is
a real, separate Node/Vite/Cesium application already embedded read-only
in the console's EYE screen (see ui/console.html's paintGlobe()). It
ships its own real action interface — createGevActionRunner() in
gods-eye-view/src/voice/gevActions.js — a live, in-page JS function bound
to the actual Cesium viewer that every voice-control verb (zoom, track an
aircraft, toggle a data layer, change visual style, ...) already goes
through. That function lives inside a browser tab; nothing outside the
browser (this Python process included) can call it directly.

The bridge: gods-eye-view/vite.config.js's dourmouseActionBridgeProxy()
adds a small in-memory action queue to that app's own dev server
(POST /api/dourmouse/action), and gods-eye-view/src/dourmouseBridge.js
(started from main.js once the globe finishes loading) long-polls for
queued actions and runs them through the SAME real runner voice control
uses, posting the real result back. This module is the THIRD side: the
real HTTP client Dourmouse's own globe_control tool (general_roster.py)
uses to reach that queue.

Honest failure modes (Rule 2.1/2.2), all real, none fabricated:
- God's Eye View's dev server isn't running at all -> connection refused,
  reported as NOT CONFIGURED with the exact command to start it.
- The server is up but no browser tab has the globe open (or it's still
  loading) -> the bridge's own server-side wait times out at 15s and
  responds honestly; run_globe_action reports THAT exact message, not a
  fabricated "done".
- The action itself fails inside the browser (unknown layer id, no
  tracked entity, etc.) -> gevActions.js's own real {ok: false, error}
  shape is returned verbatim; this module never reinterprets or
  swallows it.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

_DEFAULT_URL = "http://localhost:4173"
_URL_ENV = "GODS_EYE_URL"
# Longer than the bridge's own 15s internal wait (dourmouseActionBridgeProxy
# in vite.config.js) so THIS timeout is never the one that fires first —
# a caller should always see the bridge's own honest "no tab connected"
# message rather than a generic urllib timeout with no explanation.
_HTTP_TIMEOUT = 20.0


def gods_eye_url() -> str:
    raw = os.environ.get(_URL_ENV, "").strip()
    return (raw or _DEFAULT_URL).rstrip("/")


def run_globe_action(name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one real gevActions.js action against the live globe.

    Returns the REAL result dict from the browser (whatever shape that
    particular action returns — every gevActions.js handler already
    reports {ok, ...} on success and {ok: false, error: ...} on a real
    in-app failure) or raises RuntimeError with an honest, actionable
    message when the bridge itself couldn't be reached at all.
    """
    name = (name or "").strip()
    if not name:
        raise RuntimeError("run_globe_action requires a non-empty action 'name'.")
    payload = json.dumps({"name": name, "args": args or {}}).encode("utf-8")
    url = f"{gods_eye_url()}/api/dourmouse/action"
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:  # noqa: S310 - localhost dev server
            raw = resp.read()
    except urllib.error.URLError as exc:
        raise RuntimeError(
            "NOT CONFIGURED: God's Eye View's dev server is not reachable at "
            f"{gods_eye_url()} ({exc.reason}). Start it: cd gods-eye-view && "
            "npm run dev -- --host localhost --port 4173"
        ) from exc
    except TimeoutError as exc:
        raise RuntimeError(
            f"the request to God's Eye View timed out after {_HTTP_TIMEOUT}s "
            "(the dev server itself is up, but did not answer in time)."
        ) from exc
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"God's Eye View returned a non-JSON response: {exc}") from exc
