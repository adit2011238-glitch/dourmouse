"""Proactive surfacing (Vision stage 7): raise a small, dismissible window
UNPROMPTED when something the system already knows about is worth
mentioning — a world-monitor alert, an ATLAS leaderboard change, a system
notice — instead of waiting for the user to go look.

Real data source (not invented for this stage): GET /api/state on the
already-running DourMouse server, the SAME endpoint
``dourmouse.desktop.DesktopNotifier`` already polls for macOS native
notifications, and the SAME ``alerts`` array whose schema
(``dourmouse/state_store.py``) already validates a fixed ``kind`` on every
row: ``ALERT_KINDS = {"atlas", "world", "market", "system"}``. This module
does not add a new data source; it is a second consumer of that existing
feed, filtered down to a much smaller allowlist and rendered as a real popup
window instead of (or alongside) a system notification.

Restraint IS the design problem here, exactly as the task brief says — not
the mechanism. Two independent restraints are enforced, deliberately, not
just one:

1. ``ALLOWED_ALERT_KINDS`` below is a short, explicit, HARDCODED tuple —
   not "every kind the schema happens to allow". It is state_store's own
   validated ``ALERT_KINDS`` MINUS ``"market"``: routine price-tick alerts
   are the single noisiest, least interrupt-worthy category this app could
   ever produce, and including them by default would turn this into exactly
   the broad heuristic the brief warns against. Widening this tuple is a
   one-line, deliberate decision for whoever later decides "market" alerts
   ARE worth an interrupt — never the default.
2. Every alert id is only ever surfaced ONCE (seen-set dedupe, primed on
   the first refresh so launching the app never replays a backlog of
   pre-existing alerts) — the exact same dedupe shape
   ``dourmouse.desktop.DesktopNotifier`` already uses, applied here to
   window-popping instead of notifications.

Honesty about what is actually wired today (read this before assuming more
than what's here): grepping this entire repo for real ``add_alert(...)``
call sites finds exactly ONE — ``dourmouse/webui.py``'s ATLAS-run-started
handler, which always uses ``kind="system"``. Nothing in this codebase
today ever calls ``add_alert(kind="world", ...)`` or
``add_alert(kind="atlas", ...)`` — so in practice, until a real world-monitor
or ATLAS-leaderboard alert producer exists somewhere else in this app, this
mechanism will only ever fire on real ATLAS-run-started system alerts. The
filtering/dedupe/popup machinery below is 100% real and tested; "world" and
"atlas" are wired and ready the moment such a producer starts calling
``add_alert`` with those kinds — no changes needed here for that day to
work. The task's third named example, "mail sitting unanswered", has NO
matching entry in ``state_store.ALERT_KINDS`` at all today — the schema
would reject ``add_alert(kind="mail", ...)`` outright — and
``dourmouse/state_store.py`` is out of this task's file scope to edit, so
this module does not invent a "mail" kind that nothing could ever actually
emit. A real full version would need a mail-polling module (reading
dourmouse/google_services.py or similar) AND a new validated alert kind
before that example could genuinely fire.

Honesty about the popup window itself (Rule 2.1/2.2): like
dourmouse/overlay.py, this needs a real pywebview backend to show anything
— ``default_popup_factory`` builds a real small frameless always-on-top
window with exactly one control (Dismiss), the same "one control, no menu
to grow" restraint tray.py's kill switch already applies. This is exercised
headlessly here only up to window CONSTRUCTION (the same seam shape as
dourmouse.overlay's ``webview_loader``); actually seeing it appear
unprompted on screen needs a live desktop session to confirm.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from typing import Any, Callable

#: state_store.ALERT_KINDS is {"atlas", "world", "market", "system"} — this
#: is that vocabulary MINUS "market", chosen deliberately (see module
#: docstring). Not derived programmatically from state_store.ALERT_KINDS on
#: purpose: a restraint list must not silently widen itself just because the
#: schema grows a new kind later.
ALLOWED_ALERT_KINDS: tuple[str, ...] = ("system", "world", "atlas")

_POPUP_WIDTH = 320
_POPUP_HEIGHT = 128
_POPUP_Y_BASE = 128  # below dourmouse.overlay's status card (which sits at y=16, ~96 tall)
_POPUP_Y_STEP = 96


class _PopupBridge:
    """window.pywebview.api for one popup. ONE control on purpose (dismiss)
    — the same restraint tray.py's kill switch applies: a proactive popup is
    a look-and-decide surface, not a place to grow a menu of actions."""

    def __init__(self) -> None:
        self._window: Any = None

    def attach(self, window: Any) -> None:
        self._window = window

    def dismiss(self) -> bool:
        if self._window is None:
            return False
        try:
            self._window.destroy()
            return True
        except Exception:  # noqa: BLE001 -- dismiss must never crash the shell
            return False


_POPUP_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>DourMouse</title>
<style>
  html, body {{ margin:0; padding:0; background:transparent; overflow:hidden; }}
  #card {{
    font-family: -apple-system, "SF Mono", Menlo, monospace;
    background: rgba(18,18,22,0.92);
    color: #e6e6ea;
    border-radius: 10px;
    padding: 12px 14px;
    box-sizing: border-box;
    -webkit-user-select: none;
    user-select: none;
    height: 100%;
    display: flex;
    flex-direction: column;
  }}
  #kind {{ font-size: 10px; letter-spacing: .08em; opacity: .6; text-transform: uppercase; }}
  #title {{ font-size: 13px; font-weight: 600; margin: 4px 0 3px; }}
  #detail {{ font-size: 11px; opacity: .75; flex: 1; overflow: hidden; }}
  #dismiss {{
    align-self: flex-end;
    background: rgba(255,255,255,0.08);
    color: #e6e6ea;
    border: none;
    border-radius: 6px;
    padding: 4px 10px;
    font-size: 11px;
    cursor: pointer;
  }}
  #dismiss:hover {{ background: rgba(255,255,255,0.16); }}
</style></head>
<body>
<div id="card">
  <div id="kind">{kind}</div>
  <div id="title">{title}</div>
  <div id="detail">{detail}</div>
  <button id="dismiss">Dismiss</button>
</div>
<script>
document.getElementById('dismiss').addEventListener('click', function () {{
  try {{
    if (window.pywebview && window.pywebview.api && window.pywebview.api.dismiss) {{
      window.pywebview.api.dismiss();
    }}
  }} catch (e) {{}}
}});
</script>
</body></html>"""


def _esc(text: str) -> str:
    """Minimal HTML-escape for interpolation into the popup template — this
    content is alert title/detail text from the app's OWN state store, not
    untrusted external input, but escaping costs nothing and closes off any
    future producer accidentally breaking the template."""
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _corner_x(width: int) -> int | None:
    """Best-effort top-right placement (macOS, via AppKit — same pattern as
    dourmouse.overlay._corner_x). Honest None (let the OS place it) when the
    display can't be queried."""
    try:
        from AppKit import NSScreen  # pyobjc ships with the desktop build

        screen_w = int(NSScreen.mainScreen().frame().size.width)
        return max(0, screen_w - width - 16)
    except Exception:  # noqa: BLE001 -- placement is best-effort
        return None


def default_popup_factory(webview_module: Any) -> Callable[[dict[str, Any]], Any]:
    """Build a real popup-factory function bound to a live pywebview
    module. Each call to the returned function opens one more popup one
    slot further down the screen than the last (an internal counter closed
    over here), so several alerts surfaced in one refresh cycle stagger
    instead of stacking exactly on top of each other."""
    _next_slot = [0]

    def _create(item: dict[str, Any]) -> Any:
        slot = _next_slot[0]
        _next_slot[0] += 1
        bridge = _PopupBridge()
        html = _POPUP_HTML.format(
            kind=_esc(str(item.get("kind") or "").upper()),
            title=_esc(str(item.get("title") or "DourMouse")[:80]),
            detail=_esc(str(item.get("detail") or "")[:160]),
        )
        window = webview_module.create_window(
            "DOURMOUSE",
            html=html,
            width=_POPUP_WIDTH,
            height=_POPUP_HEIGHT,
            x=_corner_x(_POPUP_WIDTH),
            y=_POPUP_Y_BASE + slot * _POPUP_Y_STEP,
            on_top=True,
            frameless=True,
            transparent=True,
            resizable=False,
            easy_drag=True,
            shadow=False,
            focus=False,
            js_api=bridge,
        )
        bridge.attach(window)
        return window

    return _create


class ProactiveSurfacer:
    """Background: /api/state alerts -> ALLOWED_ALERT_KINDS filter -> deduped
    by id -> one real popup per genuinely NEW allowed item.

    Shaped like ``dourmouse.desktop.DesktopNotifier`` on purpose (same SSE
    hub sink shape via ``on_event``, same /api/state polling, same
    primed-on-first-refresh dedupe) — this is a SECOND consumer of the exact
    same alert feed, not a competing source of truth. When both are
    registered, an allowed alert genuinely produces both a native
    notification (DesktopNotifier) AND a popup window (this class) — that
    overlap is intentional; restraint here comes from the allowlist, not
    from suppressing the existing notification path.
    """

    _MAX_SEEN = 200

    def __init__(
        self,
        base_url: str,
        popup_factory: Callable[[dict[str, Any]], Any] | None,
        *,
        fetch: Callable[[str], dict[str, Any] | None] | None = None,
        allowed_kinds: tuple[str, ...] = ALLOWED_ALERT_KINDS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._popup_factory = popup_factory
        self._fetch = fetch or self._default_fetch
        self._allowed_kinds = frozenset(allowed_kinds)
        self._seen: set[int] = set()
        self._primed = False
        self._refreshing = False
        self._lock = threading.Lock()

    @staticmethod
    def _default_fetch(url: str) -> dict[str, Any] | None:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 -- loopback only
                return json.loads(resp.read().decode("utf-8"))
        except (OSError, ValueError, urllib.error.URLError):
            return None

    def on_event(self, payload: dict[str, Any] | None) -> None:
        """Hub sink: react to alerts state_change broadcasts only — same
        shape and same off-the-broadcast-thread guard as
        DesktopNotifier.on_event (a synchronous /api/state fetch inside the
        hub's own broadcast thread would stall every other connected
        client)."""
        payload = payload or {}
        if payload.get("type") != "state_change":
            return
        if payload.get("section") != "alerts":
            return
        with self._lock:
            if self._refreshing:
                return
            self._refreshing = True
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self) -> None:
        try:
            self.refresh()
        finally:
            with self._lock:
                self._refreshing = False

    def refresh(self) -> list[dict[str, Any]]:
        """Fetch /api/state and pop a window for every genuinely new,
        allowed-kind alert. Returns the list actually surfaced (empty list,
        never None, when the server is unreachable or nothing qualified) —
        useful for tests and for a caller that wants to know what fired."""
        state = self._fetch(f"{self._base_url}/api/state")
        if state is None:
            return []
        surfaced: list[dict[str, Any]] = []
        with self._lock:
            for alert in state.get("alerts") or []:
                try:
                    aid = int(alert.get("id"))
                except (TypeError, ValueError):
                    continue
                if not self._primed:
                    self._seen.add(aid)
                    continue
                if aid in self._seen:
                    continue
                self._seen.add(aid)
                kind = str(alert.get("kind") or "")
                if kind not in self._allowed_kinds:
                    continue  # the allowlist gate -- everything else is silently skipped
                surfaced.append(alert)
            self._primed = True
            if len(self._seen) > self._MAX_SEEN:
                self._seen = set(sorted(self._seen)[-self._MAX_SEEN:])
        for i, alert in enumerate(surfaced):
            if self._popup_factory is None:
                continue
            try:
                self._popup_factory(alert)
            except Exception:  # noqa: BLE001 -- a bad popup must never crash the poller
                pass
        return surfaced
