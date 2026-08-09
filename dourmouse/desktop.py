"""Native desktop launcher for Dourmouse (v2.5).

Runs the SAME stdlib web UI server (dourmouse.webui) in-process on a
background thread and opens it in a REAL native macOS window via PyWebView
(WebKit/WKWebView) — no browser chrome, no tabs, no Node, no build step.
The dashboard and the Agent Map each get their own native window.

Why this shape (documented decision):
- The UI already speaks SSE + fetch to a plain http://127.0.0.1:PORT server,
  and WKWebView supports both reliably against a real local HTTP server
  (custom-scheme loading is the fragile path — we deliberately avoid it).
- The Agent Map window is created up-front with hidden=True and revealed by
  the js_api bridge (window.pywebview.api.open_map()). Creating webview
  windows dynamically from a background/JS thread is backend-dependent and
  unsafe; creating both windows before webview.start() is the supported,
  thread-safe pattern.

Honesty rules (Rule 2.1/2.2) apply here exactly like everywhere else:
- If PyWebView is not installed, we say so plainly and fall back to opening
  the DEFAULT BROWSER — a visible, documented degradation, never a silent
  stub. `launch(..., prefer_browser=True)` forces the browser path.
- The server binds 127.0.0.1 only; secrets stay in .env (Rule 2.6).

Run it:
    python -m dourmouse.desktop
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from typing import Any, Callable

_PORT_ENV = "DOURMOUSE_UI_PORT"
_DEFAULT_PORT = 8765


def _pick_port() -> int:
    raw = os.environ.get(_PORT_ENV, "")
    try:
        return int(raw) if raw.strip() else _DEFAULT_PORT
    except ValueError:
        return _DEFAULT_PORT


def _import_webview() -> Any:
    """Lazy import so the module works (and tests run) without pywebview."""
    try:
        import webview  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "NOT CONFIGURED: the native window needs pywebview — run "
            "`.venv/bin/python -m pip install -r requirements-desktop.txt`."
        ) from exc
    return webview


class DesktopBridge:
    """Exposed to the dashboard page as window.pywebview.api.

    Lets the page open native windows from inside the app:
    - ``open_map()``: reveal the (pre-created, hidden) Agent Map window.
    - ``open_agent(name)``: open a dedicated LIVE window for one subagent at
      /agent/<name> — the "each agent gets its own DOURMOUSE window" feature
      (v2.7). PyWebView supports creating windows at runtime from the
      JS-bridge thread (verified against pywebview docs; it queues UI ops on
      the GUI thread), so this is safe on the macOS/WKWebView backend.
      Windows are tracked by agent name and REUSED (brought to front) rather
      than duplicated; a closed window is recreated.

    v5.19 (desktop conversion) — the typed IPC surface, kept tiny and typed
    (window-state / deep-link only; NO shell, no paths, no exec):
    - ``window_state()`` / ``set_window_state()``: window geometry through
      the existing per-user prefs API (machine scope, owner '*'), so the app
      reopens where you left it.
    - ``navigate(href)``: point the main window at a validated SPA hash
      route. The href must come from ``deeplink.parse_deeplink`` (the
      allow-list gate) — only ``[A-Za-z0-9_/-#]`` ever reaches
      ``location.hash``; nothing else is ever executed.
    """

    #: Persisted-geometry sanity clamp (width/height never smaller).
    _MIN_DIMENSION = 200
    _WINDOW_PREFS_KEY = "desktop.window"

    def __init__(self, map_window: Any, webview: Any, base_url: str,
                 state: Any | None = None) -> None:
        self._map_window = map_window
        self._webview = webview
        self._base_url = base_url
        self._state = state  # the server's StateStore (in-process)
        self._main_window: Any | None = None
        self._agent_windows: dict[str, Any] = {}

    def attach_main_window(self, window: Any) -> None:
        """The shell's main window — created after the bridge, so attached
        once available (navigate() and close-persistence need it)."""
        self._main_window = window

    def open_map(self) -> None:
        self._map_window.show()

    def open_agent(self, name: str) -> None:
        name = (name or "").strip()
        if not name:
            return
        win = self._agent_windows.get(name)
        if win is not None and not getattr(win, "closed", False):
            win.show()  # already open -> bring to front, don't duplicate
            return
        win = self._webview.create_window(
            f"AGENT // {name.upper()}",
            f"{self._base_url}/agent/{name}",
            width=980,
            height=760,
            min_size=(720, 540),
        )
        self._agent_windows[name] = win

    def open_all_agents(self, names) -> None:
        """v2.8: open EVERY registered agent's own live window at startup.

        Called from launch() before webview.start() so the whole roster is
        visible and working from the first frame (all agents live and
        immediately working). Each window is created once and reused — the
        same dedupe registry as open_agent().
        """
        for name in sorted(names or []):
            self.open_agent(name)

    # -- v5.19: typed window-state IPC (Phase 1 native shell) ------------- #

    def window_state(self) -> dict[str, Any]:
        """The persisted window geometry (via the existing prefs store), or
        {} honestly when nothing is saved or the store is absent. Only
        validated ints (width/height >= _MIN_DIMENSION, x/y ints) and a
        bool maximized flag are returned — garbage prefs are never replayed."""
        if self._state is None:
            return {}
        raw = self._state.get_pref(self._WINDOW_PREFS_KEY, {}, owner="*")
        if not isinstance(raw, dict):
            return {}
        out: dict[str, Any] = {}
        for key in ("width", "height", "x", "y"):
            value = raw.get(key)
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
            if key in ("width", "height") and value < self._MIN_DIMENSION:
                continue
            out[key] = value
        if isinstance(raw.get("maximized"), bool):
            out["maximized"] = raw["maximized"]
        return out

    def set_window_state(self, *, width=None, height=None, x=None, y=None,
                         maximized=None) -> bool:
        """Persist window geometry through the existing per-user prefs API
        (machine scope — the window belongs to this machine). Returns False
        honestly when nothing valid was provided or the store is absent."""
        if self._state is None:
            return False
        state: dict[str, Any] = {}
        for key, value in (("width", width), ("height", height),
                           ("x", x), ("y", y)):
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
            if key in ("width", "height") and value < self._MIN_DIMENSION:
                continue
            state[key] = value
        if isinstance(maximized, bool):
            state["maximized"] = maximized
        if not state:
            return False
        self._state.set_pref(self._WINDOW_PREFS_KEY, state, owner="*")
        return True

    # -- v5.19: validated navigation (deep links) ------------------------- #

    def navigate(self, href: str | None) -> bool:
        """Navigate the main window to a validated SPA hash route.

        Only ``[A-Za-z0-9_/-#]`` characters are ever accepted (the deep-link
        allow-list output), assigned to ``location.hash`` — never executed.
        Returns False honestly when the href is invalid or no window is
        attached (e.g. browser-fallback mode)."""
        href = (href or "").strip()
        if not re.fullmatch(r"^#[A-Za-z0-9_/-]{1,200}$", href):
            return False
        win = self._main_window
        if win is None or not hasattr(win, "evaluate_js"):
            return False
        try:
            win.evaluate_js(f"location.hash = {json.dumps(href)}")
            return True
        except Exception:  # noqa: BLE001 -- a failing navigate never crashes the shell
            return False


class DesktopNotifier:
    """Maps DOURMOUSE alert broadcasts to native notifications (v5.19).

    The desktop process subscribes to the server's OWN SSE fan-out hub (the
    same one the HUD uses — no extra endpoint) and, on a ``state_change``
    for the alerts section, diffs the alert inbox against the last seen ids
    and notifies on NEW alerts. The first refresh only seeds the seen set
    (late-subscriber honesty, like the SSE watcher's status replay) so a
    launch never spams every pre-existing alert.

    Honest (Rule 2.2): the default notifier uses macOS ``osascript``
    ``display notification``; when that fails or is unavailable (headless,
    non-macOS, missing binary) the alert is printed to the app console —
    never silently dropped, never faked. ``notifier`` is the test seam.
    """

    _MAX_SEEN = 200

    def __init__(self, base_url: str,
                 notifier: Callable[[str, str], None] | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._notifier = notifier or self._default_notify
        self._seen: set[int] = set()
        self._primed = False
        self._refreshing = False
        self._lock = threading.Lock()

    @staticmethod
    def _default_notify(title: str, body: str) -> None:
        """macOS native notification via osascript; honest stderr fallback."""
        if shutil.which("osascript"):
            script = "display notification {} with title {}".format(
                json.dumps((body or "")[:120]),
                json.dumps((title or "DOURMOUSE alert")[:80]),
            )
            try:
                subprocess.run(["osascript", "-e", script], check=False,
                               capture_output=True, timeout=10)
                return
            except Exception:  # noqa: BLE001 -- fall through to the console
                pass
        print(f"[NOTIFY] {title}: {body}")

    def on_event(self, payload: dict[str, Any] | None) -> None:
        """Hub sink: react to alerts state_change broadcasts only.

        The refresh runs on a DAEMON THREAD — the SSE hub calls each sink's
        emit() sequentially on its own broadcast thread, and a synchronous
        /api/state fetch inside it (up to the 5s timeout) would stall the
        live fan-out to every connected HUD client (reviewer-caught). The
        notifier must never be the slowest client in the hub.
        """
        payload = payload or {}
        if payload.get("type") != "state_change":
            return
        if payload.get("section") != "alerts":
            return
        with self._lock:
            if self._refreshing:
                return  # a refresh is already in flight; it will see the new alert
            self._refreshing = True
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self) -> None:
        try:
            self.refresh()
        finally:
            with self._lock:
                self._refreshing = False

    def refresh(self) -> None:
        """Fetch /api/state (the desktop shell is signed out, so this reads
        the shared bucket + system alerts — the owner's machine scope) and
        notify on alerts we have not seen before."""
        try:
            with urllib.request.urlopen(  # noqa: S310 -- the loopback server
                f"{self._base_url}/api/state", timeout=5
            ) as resp:
                state = json.loads(resp.read().decode("utf-8"))
        except (OSError, ValueError, urllib.error.URLError):
            return
        new: list[dict[str, Any]] = []
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
                new.append(alert)
            self._primed = True
            # Bound the seen set so a long-lived shell never leaks memory.
            if len(self._seen) > self._MAX_SEEN:
                self._seen = set(sorted(self._seen)[-self._MAX_SEEN:])
        for alert in new:
            self._notifier(
                (alert.get("title") or "DOURMOUSE alert").strip(),
                (alert.get("detail") or "").strip(),
            )


def _wait_forever() -> None:
    """Keep the server alive in browser-fallback mode until Ctrl+C."""
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def _fallback_to_browser(url: str, reason: str) -> int:
    """Honest degradation: explain WHY, open the browser, keep serving."""
    print(f"[DESKTOP] {reason}")
    print(f"[DESKTOP] Falling back to your default browser: {url}")
    webbrowser.open(url)
    _wait_forever()
    return 1


def launch(
    registry: Any | None = None,
    *,
    host: str = "127.0.0.1",
    port: int | None = None,
    width: int = 1440,
    height: int = 900,
    webview_loader: Callable[[], Any] | None = None,
    live_polling: bool = True,
    open_all_windows: bool = True,
    deep_link: str | None = None,
) -> int:
    """Launch the DOURMOUSE desktop app. Returns a process exit code.

    Starts the web UI server on a background thread, then opens a native
    PyWebView window pointed at it. Falls back to the default browser with an
    honest message when pywebview is unavailable.

    ``webview_loader`` is the test seam for ``_import_webview``.
    ``live_polling`` (v2.8): start the always-on live agent loops (env
    DOURMOUSE_LIVE=0 still disables them, see live_runtime.live_enabled).
    ``open_all_windows`` (v2.8): open every agent's own native window at
    startup so the whole roster is live and immediately working.
    ``memory`` (v2.9): long-term store for the Store & Learn loop. None
    (default) opens the default store — the real app learns from every
    completed session (DOURMOUSE_LEARN=0 honestly disables it, see
    learn.open_default_store). Hermetic tests pass an explicit store or
    set DOURMOUSE_LEARN=0.
    """
    from dourmouse import learn, webui
    from dourmouse.general_roster import build_general_registry

    registry = registry if registry is not None else build_general_registry()
    port = _pick_port() if port is None else port

    memory = learn.open_default_store()
    # v3.1: resolve the NVIDIA config so server.config is set and per-agent
    # models (DOURMOUSE_MODEL_<AGENT>) work in the real app — same fix as
    # serve_forever (reviewer-caught: None left the feature inert).
    config = webui._resolve_server_config(None)
    server = webui.run_server(
        registry,
        host=host,
        port=port,
        config=config,
        live_polling=live_polling,
        memory=memory,
    )
    actual_port = server.server_address[1]
    url = f"http://{host}:{actual_port}"
    # v5.19: an OS deep link at launch (dourmouse://...) loads the main
    # window at its validated SPA route — the allow-list parser decides,
    # never raw argv.
    initial_href = ""
    if deep_link:
        from dourmouse.deeplink import parse_deeplink

        parsed = parse_deeplink(deep_link)
        if parsed["ok"]:
            initial_href = parsed["href"]
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="dourmouse-webui")
    thread.start()
    print(f"[DESKTOP] Dourmouse core online at {url}")
    if server.live_runtime is not None:
        print(
            f"[DESKTOP] {server.live_runtime.poll_count} always-on live agent poll loop(s) running"
        )

    # The notification sink and its hub are initialized OUTSIDE the try so
    # the finally block can always reference them — a loader failure that
    # returns via the browser-fallback must not trip a NameError on cleanup
    # (reviewer-guard: the pre-existing fallback test catches this).
    notifier_sink = None
    events_hub = getattr(server, "events_broadcast", None)
    try:
        loader = webview_loader or _import_webview
        try:
            webview = loader()
        except RuntimeError as exc:
            return _fallback_to_browser(url, str(exc))

        map_window = webview.create_window(
            "AGENT ORCHESTRATION MAP",
            f"{url}/map",
            width=1280,
            height=860,
            hidden=True,
        )
        bridge = DesktopBridge(map_window, webview, url, state=server.state)
        # v2.8: every agent gets its own live window at startup — created
        # BEFORE webview.start() (the documented thread-safe pattern), so
        # the whole roster is working and visible from the first frame.
        if open_all_windows:
            bridge.open_all_agents(registry.subagent_names)
        # v5.19: window-geometry memory (Phase 1) — restore the last size/
        # position from the prefs store before the window is made.
        geometry = bridge.window_state()
        main_window = webview.create_window(
            "DOURMOUSE // CENTRAL AGENT DISPATCH",
            f"{url}{initial_href}",
            width=int(geometry.get("width", width)),
            height=int(geometry.get("height", height)),
            min_size=(1024, 680),
            js_api=bridge,
        )
        bridge.attach_main_window(main_window)
        # Persist geometry on close so the next launch reopens where the
        # user left it. Guarded: a webview without window.events (old
        # backend / test fake) must never break the shell.
        try:

            def _persist_geometry() -> None:
                bridge.set_window_state(
                    width=getattr(main_window, "width", None),
                    height=getattr(main_window, "height", None),
                    x=getattr(main_window, "x", None),
                    y=getattr(main_window, "y", None),
                    maximized=getattr(main_window, "maximized", None),
                )

            main_window.events.closed += _persist_geometry
        except Exception:  # noqa: BLE001 -- window-state memory is best-effort
            pass
        # v5.19: native alert notifications — the desktop process subscribes
        # to the server's OWN SSE fan-out (same hub the HUD uses, no extra
        # endpoint) and maps new alerts to native notifications. Env-gated:
        # DOURMOUSE_DESKTOP_NOTIFICATIONS=0 disables.
        if os.environ.get("DOURMOUSE_DESKTOP_NOTIFICATIONS", "1") != "0" \
                and events_hub is not None:
            notifier = DesktopNotifier(url)

            class _HubSink:
                """Minimal SSE-stream-shaped sink for the broadcast hub."""

                def emit(self, payload: dict[str, Any]) -> None:  # noqa: N802 -- SSE API
                    notifier.on_event(payload)

            notifier_sink = _HubSink()
            events_hub.register(notifier_sink)
        # Blocks until every window is closed; Ctrl+C also returns. If the
        # GUI backend fails at start() (headless/SSH session, missing pyobjc),
        # degrade to the browser with a clear message — the server stays up.
        try:
            webview.start()
        except KeyboardInterrupt:
            pass
        except Exception as exc:
            return _fallback_to_browser(url, f"Native window could not start: {exc}")
        return 0
    finally:
        if notifier_sink is not None and events_hub is not None:
            try:
                events_hub.unregister(notifier_sink)
            except Exception:  # noqa: BLE001 -- cleanup is best-effort
                pass
        if server.live_runtime is not None:
            server.live_runtime.stop()
        if memory is not None:
            memory.close()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    import sys

    from dourmouse.deeplink import deep_link_from_argv

    pid_file = ".dourmouse-ui.pid"
    try:
        with open(pid_file, "w") as fh:
            fh.write(str(os.getpid()))
    except OSError:
        pass
    try:
        # v5.19: macOS/Windows re-launch the app with a dourmouse:// URL in
        # argv when the scheme is registered — only the validated parse of
        # it is ever used.
        sys.exit(launch(deep_link=deep_link_from_argv(sys.argv)))
    finally:
        try:
            os.remove(pid_file)
        except OSError:
            pass
