"""Always-on-top ambient status overlay (Vision stage 2).

A small, semi-transparent, always-on-top window — not something you open,
something that is simply there — showing, at a glance:
  - MIC / CAMERA armed-or-killed (two dots, same privacy state
    dourmouse/tray.py's kill switch owns — see ``_read_privacy_state``),
  - one line of real status: what DourMouse is currently doing.

The status line is NOT decorative. It is a real poll of GET /api/activity
on the already-running DourMouse web UI server — the SAME ActivityTracker
(dourmouse/webui.py) that already drives the Agent Map window, so this
overlay never invents a second, competing notion of "what's happening".
``summarize_activity()`` is the pure function that turns that snapshot into
one headline + detail line; it is the thing to unit test, independent of
any GUI.

Honesty (Rule 2.1/2.2): unlike dourmouse/desktop.py, there is deliberately
NO browser-tab fallback when pywebview is unavailable. An always-on-top
overlay's entire reason to exist is living outside a browser tab; falling
back to a browser tab would silently defeat the feature while looking like
it worked. When pywebview is missing this prints exactly why and exits 1 —
never a degraded stand-in pretending to be the real thing.

Run it standalone (needs an already-running DourMouse server — launch
dourmouse.desktop or dourmouse.webui first):
    .venv/bin/python -m dourmouse.overlay
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from typing import Any, Callable

_HOST = "127.0.0.1"
_POLL_INTERVAL_SECONDS = 2.0
_OVERLAY_WIDTH = 260
_OVERLAY_HEIGHT = 96


def _base_url() -> str:
    """The already-running DourMouse server's URL — reuses desktop.py's own
    port-selection helper (DOURMOUSE_UI_PORT env, default 8765) rather than
    inventing a second convention for the same setting."""
    from dourmouse.desktop import _pick_port

    return f"http://{_HOST}:{_pick_port()}"


def summarize_activity(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """Turn a GET /api/activity payload (dourmouse.webui.ActivityTracker
    .snapshot(): ``{"agents": {name: {"status", "last", "feed"}}}``) into a
    one-line ambient summary. Pure function, no I/O — webui.py stays the
    single source of truth for status; this only renders it.

    Priority when several agents are in different states at once: a
    confirmation waiting on the user outranks background work, which
    outranks an always-on live monitor, which outranks true idle — because
    that is the order of things the user most needs to notice.
    """
    agents = (snapshot or {}).get("agents") or {}
    computing: list[tuple[str, dict[str, Any]]] = []
    auth: list[str] = []
    live: list[str] = []
    for name, info in agents.items():
        info = info or {}
        status = info.get("status")
        if status == "computing":
            computing.append((name, info.get("last") or {}))
        elif status == "auth":
            auth.append(name)
        elif status == "live":
            live.append(name)

    if auth:
        return {
            "busy": True,
            "headline": "WAITING ON YOU",
            "detail": f"{auth[0]} needs confirmation",
        }
    if computing:
        name, last = computing[0]
        tool = last.get("tool") or "working"
        extra = f" (+{len(computing) - 1} more)" if len(computing) > 1 else ""
        return {
            "busy": True,
            "headline": "WORKING",
            "detail": f"{name} · {tool}{extra}",
        }
    if live:
        return {
            "busy": False,
            "headline": "IDLE",
            "detail": f"{len(live)} live monitor(s) running",
        }
    return {"busy": False, "headline": "IDLE", "detail": "nothing in progress"}


def _read_privacy_state() -> dict[str, bool]:
    """Mic/camera armed-vs-killed, read from the SAME persisted flag
    dourmouse/tray.py's kill switch writes — the overlay never keeps its
    own copy of this truth. Honest defaults (both armed) when tray.py has
    never run or can't be read, matching tray.load_state()'s own defaults."""
    try:
        from dourmouse.tray import load_state

        state = load_state()
        return {"mic_enabled": state.mic_enabled, "camera_enabled": state.camera_enabled}
    except Exception:  # noqa: BLE001 -- the overlay must never crash on this
        return {"mic_enabled": True, "camera_enabled": True}


class OverlayStatusPoller:
    """Background poller: activity snapshot + privacy state -> one summary
    dict, pushed to ``on_update`` on an interval.

    Shaped like dourmouse.desktop.DesktopNotifier on purpose (urllib
    against the loopback server, injectable seams, a daemon thread) rather
    than inventing a new polling pattern for the same server.
    """

    def __init__(
        self,
        base_url: str,
        on_update: Callable[[dict[str, Any]], None],
        *,
        interval: float = _POLL_INTERVAL_SECONDS,
        fetch: Callable[[str], dict[str, Any] | None] | None = None,
        privacy_reader: Callable[[], dict[str, bool]] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._on_update = on_update
        self._interval = interval
        self._fetch = fetch or self._default_fetch
        self._privacy_reader = privacy_reader or _read_privacy_state
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def _default_fetch(url: str) -> dict[str, Any] | None:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:  # noqa: S310 -- loopback only
                return json.loads(resp.read().decode("utf-8"))
        except (OSError, ValueError, urllib.error.URLError):
            return None

    def poll_once(self) -> dict[str, Any]:
        snapshot = self._fetch(f"{self._base_url}/api/activity")
        if snapshot is None:
            summary = {
                "busy": False,
                "headline": "OFFLINE",
                "detail": "no dourmouse server reachable",
            }
        else:
            summary = summarize_activity(snapshot)
        summary["online"] = snapshot is not None
        summary.update(self._privacy_reader())
        return summary

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="dourmouse-overlay-poll"
        )
        self._thread.start()

    def stop(self, join: bool = False) -> None:
        self._stop.set()
        if join and self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._on_update(self.poll_once())
            except Exception:  # noqa: BLE001 -- the poll loop must never die
                pass
            self._stop.wait(self._interval)


_OVERLAY_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>DourMouse Status</title>
<style>
  html, body { margin:0; padding:0; background:transparent; overflow:hidden; }
  #card {
    font-family: -apple-system, "SF Mono", Menlo, monospace;
    background: rgba(18,18,22,0.82);
    color: #e6e6ea;
    border-radius: 10px;
    padding: 10px 12px;
    box-sizing: border-box;
    -webkit-user-select: none;
    user-select: none;
  }
  .row { display:flex; align-items:center; gap:6px; font-size:11px; letter-spacing:.04em; opacity:.85; }
  .dot { width:8px; height:8px; border-radius:50%; background:#555; flex:0 0 auto; }
  .dot.on { background:#3adc64; }
  .dot.off { background:#e0413a; }
  .dot.offline { background:#555; }
  #headline { font-size:13px; font-weight:600; margin:6px 0 2px; }
  #detail { font-size:11px; opacity:.7; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
</style></head>
<body>
<div id="card">
  <div class="row">
    <span class="dot" id="micDot"></span><span>MIC</span>
    <span class="dot" id="camDot" style="margin-left:10px"></span><span>CAMERA</span>
  </div>
  <div id="headline">STARTING…</div>
  <div id="detail">connecting to dourmouse…</div>
</div>
<script>
window.__dourmouseOverlayUpdate = function(s) {
  try {
    document.getElementById('headline').textContent = s.headline || '';
    document.getElementById('detail').textContent = s.detail || '';
    var offline = s.online === false;
    var micDot = document.getElementById('micDot');
    var camDot = document.getElementById('camDot');
    micDot.className = 'dot ' + (offline ? 'offline' : (s.mic_enabled ? 'on' : 'off'));
    camDot.className = 'dot ' + (offline ? 'offline' : (s.camera_enabled ? 'on' : 'off'));
  } catch (e) {}
};
</script>
</body></html>"""


def _push(window: Any, summary: dict[str, Any]) -> None:
    """Push a status update into the already-open window via evaluate_js —
    the exact mechanism dourmouse.desktop.DesktopBridge.navigate() already
    uses to talk to a live pywebview window, applied here to a small JS
    function instead of location.hash."""
    if not hasattr(window, "evaluate_js"):
        return
    try:
        window.evaluate_js(
            f"window.__dourmouseOverlayUpdate({json.dumps(summary)})"
        )
    except Exception:  # noqa: BLE001 -- a failed push must never crash the shell
        pass


def _corner_x(width: int) -> int | None:
    """Best-effort top-right placement (macOS, via AppKit — already a
    pywebview dependency, same as dourmouse.desktop's split-screen code).
    Honest None (let the OS place the window) when the display can't be
    queried — same fallback shape as DesktopBridge.screen_size()."""
    try:
        from AppKit import NSScreen  # pyobjc ships with the desktop build

        screen_w = int(NSScreen.mainScreen().frame().size.width)
        return max(0, screen_w - width - 16)
    except Exception:  # noqa: BLE001 -- placement is best-effort
        return None


def launch(
    *,
    base_url: str | None = None,
    webview_loader: Callable[[], Any] | None = None,
    width: int = _OVERLAY_WIDTH,
    height: int = _OVERLAY_HEIGHT,
) -> int:
    """Start the standalone always-on-top status overlay. Returns a process
    exit code (0 success, 1 pywebview unavailable).

    ``webview_loader`` is the test seam (mirrors dourmouse.desktop.launch).
    """
    from dourmouse.desktop import _import_webview

    loader = webview_loader or _import_webview
    try:
        webview = loader()
    except RuntimeError as exc:
        print(f"[OVERLAY] {exc}")
        return 1

    window = webview.create_window(
        "DOURMOUSE STATUS",
        html=_OVERLAY_HTML,
        width=width,
        height=height,
        x=_corner_x(width),
        y=16,
        on_top=True,
        frameless=True,
        transparent=True,
        resizable=False,
        easy_drag=True,
        shadow=False,
        focus=False,
    )

    poller = OverlayStatusPoller(
        base_url or _base_url(), on_update=lambda summary: _push(window, summary)
    )
    poller.start()

    try:
        window.events.closed += poller.stop
    except Exception:  # noqa: BLE001 -- cleanup wiring is best-effort
        pass

    try:
        webview.start()
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # noqa: BLE001 -- report, never crash silently
        print(f"[OVERLAY] native window could not start: {exc}")
        return 1
    finally:
        poller.stop()
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(launch())
