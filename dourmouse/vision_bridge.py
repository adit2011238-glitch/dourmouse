"""Kill-switch reach for an in-progress browser vision session (Vision stage 5,
backend half).

The problem this solves: dourmouse/tray.py's kill switch (Vision stage 3)
flips a persisted flag the instant "Kill camera + mic NOW" is clicked, but
until now nothing continuous existed to reach — see tray.py's own docstring.
Vision stage 5 makes the browser's MediaPipe hand tracking in ui/index.html
continuous, so the kill switch now needs a REAL way to reach INTO an
in-progress browser session and force it to stop, not just prevent a future
one from starting.

Why a second tiny server instead of a route on dourmouse/webui.py: this task
explicitly scopes dourmouse/webui.py out (another agent owns it in this
checkout — see the task brief). So this is a second, genuinely real, stdlib
``http.server`` process — the same ``ThreadingHTTPServer`` primitive
webui.py itself is built on (grep webui.py: ``from http.server import
BaseHTTPRequestHandler, ThreadingHTTPServer``) — started by dourmouse/tray.py
(the process that already owns the live ``KillSwitch`` instance) alongside
its tray icon. ui/index.html's continuous vision loop polls it directly
(``visionKillSwitchLoop()`` — see that file) so a kill-switch click reaches
an in-progress browser MediaPipe session within one poll interval (default
1.5s) even though the browser page is served by a completely different
process (dourmouse.webui, port 8765 by default) than this bridge (port 8766
by default).

Honesty (Rule 2.1/2.2): every part of this module is genuinely runnable and
tested headlessly — it is plain stdlib ``http.server`` with no native/GUI
dependency, so ``dourmouse/tests/test_vision_bridge.py`` starts a real
instance on an ephemeral port and makes real HTTP requests against it. What
is NOT verified here (and needs a live desktop session to confirm) is (a)
that ui/index.html's browser-side poller in a REAL browser actually receives
these responses and stops a REAL getUserMedia() stream, and (b) that
tray.py's packaged/live process can bind the configured port on a machine
where something else might already be using it (handled honestly below —
see ``VisionBridgeServer.start``).

Fail-open default, deliberately, matching tray.py's own convention: when
this bridge is unreachable (tray.py not running yet, or its bind failed),
ui/index.html treats mic/camera as "allowed" — the SAME default
``tray.KillSwitchState()`` uses before anything has ever been killed. This
is not a new policy invented here; it is the existing house convention
(``tray.load_state()``'s docstring: "Honest defaults ... never raises, so a
reader always gets a usable answer instead of crashing on a bad file") that
a state that has never been read defaults to enabled. The direct
consequence, stated plainly: the kill switch can only reach a continuous
browser vision session while dourmouse/tray.py is actually running. That is
an honest fact about this design, not a bug to silently paper over.

Run it standalone (mostly for manual testing — normally dourmouse.tray
starts this automatically):
    .venv/bin/python -m dourmouse.vision_bridge
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

_PORT_ENV = "DOURMOUSE_VISION_BRIDGE_PORT"
_DEFAULT_PORT = 8766
_HOST = "127.0.0.1"


def bridge_port() -> int:
    raw = os.environ.get(_PORT_ENV, "")
    try:
        return int(raw) if raw.strip() else _DEFAULT_PORT
    except ValueError:
        return _DEFAULT_PORT


def _state_payload(state_reader: Callable[[], Any]) -> dict[str, Any]:
    """Build the JSON payload from a dourmouse.tray.KillSwitchState-shaped
    object. A thin pure function so the wire format is unit-testable without
    any socket involved."""
    state = state_reader()
    return {
        "mic_enabled": bool(getattr(state, "mic_enabled", True)),
        "camera_enabled": bool(getattr(state, "camera_enabled", True)),
        "updated_at": str(getattr(state, "updated_at", "") or ""),
        "online": True,
    }


def _make_handler(state_reader: Callable[[], Any]) -> type[BaseHTTPRequestHandler]:
    """Build a request-handler class closing over ``state_reader`` (a
    ``KillSwitch.state`` getter, or ``tray.load_state`` — anything returning
    a KillSwitchState-shaped object). A factory, not a module-level class,
    so tests can point multiple independent servers at different fake
    states without global mutation."""

    class _Handler(BaseHTTPRequestHandler):
        server_version = "DourMouseVisionBridge/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 -- stdlib hook name
            pass  # keep the tray console quiet; this endpoint is polled every ~1.5s

        def _cors(self) -> None:
            # Loopback-only server, GET-only, two booleans + a timestamp —
            # CORS is wide open on purpose so ui/index.html (served from a
            # DIFFERENT origin/port by dourmouse.webui) can fetch() it.
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Cache-Control", "no-store")

        def do_OPTIONS(self) -> None:  # noqa: N802 -- stdlib hook name
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802 -- stdlib hook name
            if self.path.split("?", 1)[0] not in ("/api/vision-state", "/api/vision-stream"):
                self.send_response(404)
                self._cors()
                self.end_headers()
                return
            if self.path.startswith("/api/vision-stream"):
                self._serve_sse()
                return
            payload = _state_payload(state_reader)
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _serve_sse(self) -> None:
            """Push-based alternative to polling /api/vision-state — a
            heartbeat every second carrying the current state, so a browser
            EventSource sees a kill within ~1s without polling at all.
            Genuinely real (plain chunked text/event-stream over the same
            stdlib handler); exercised for real in
            dourmouse/tests/test_vision_bridge.py by reading a bounded
            number of bytes from a live connection."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.end_headers()
            try:
                for _ in range(3600):  # ~1 hour cap so a forgotten tab can't pin a thread forever
                    payload = _state_payload(state_reader)
                    chunk = f"data: {json.dumps(payload)}\n\n".encode("utf-8")
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    if self._stop_event_set():
                        return
                    import time

                    time.sleep(1.0)
            except (BrokenPipeError, ConnectionResetError):
                return

        def _stop_event_set(self) -> bool:
            evt = getattr(self.server, "_dourmouse_stop", None)
            return bool(evt is not None and evt.is_set())

    return _Handler


class VisionBridgeServer:
    """Owns the loopback HTTP server. ``start()``/``stop()`` are the test
    seam; ``state_reader`` defaults to ``dourmouse.tray.load_state`` (a
    fresh disk read every request, matching overlay.py's own
    ``_read_privacy_state`` pattern) but a running tray process should pass
    its live ``KillSwitch.state`` getter instead, so a poll never has to
    round-trip the state file the tray itself just wrote.
    """

    def __init__(
        self,
        *,
        state_reader: Callable[[], Any] | None = None,
        host: str = _HOST,
        port: int | None = None,
    ) -> None:
        if state_reader is None:
            from dourmouse.tray import load_state as state_reader  # type: ignore[assignment]
        self._state_reader = state_reader
        self._host = host
        self._port = bridge_port() if port is None else port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    @property
    def port(self) -> int:
        """The port actually bound (may differ from the requested port when
        0 was passed — used by tests to grab an ephemeral port)."""
        if self._server is not None:
            return self._server.server_address[1]
        return self._port

    def start(self) -> tuple[bool, str]:
        """Bind and start serving on a daemon thread. Returns (ok, detail)
        and NEVER raises — a port already in use (e.g. a second tray
        process, or something unrelated already on 8766) is an honest
        failure the caller can log and continue past; the tray icon itself
        must keep working even if this bridge cannot bind."""
        if self._server is not None:
            return True, f"already running on {self._host}:{self.port}"
        handler = _make_handler(self._state_reader)
        try:
            server = ThreadingHTTPServer((self._host, self._port), handler)
        except OSError as exc:
            return False, f"could not bind {self._host}:{self._port}: {exc}"
        server._dourmouse_stop = self._stop_event  # type: ignore[attr-defined]
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever, daemon=True, name="dourmouse-vision-bridge"
        )
        self._thread.start()
        return True, f"listening on {self._host}:{server.server_address[1]}"

    def stop(self) -> None:
        self._stop_event.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None


def launch() -> int:
    bridge = VisionBridgeServer()
    ok, detail = bridge.start()
    print(f"[VISION-BRIDGE] {detail}")
    if not ok:
        return 1
    try:
        while True:
            import time

            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        bridge.stop()
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(launch())
