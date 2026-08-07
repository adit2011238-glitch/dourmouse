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

import os
import threading
import time
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
    """

    def __init__(self, map_window: Any, webview: Any, base_url: str) -> None:
        self._map_window = map_window
        self._webview = webview
        self._base_url = base_url
        self._agent_windows: dict[str, Any] = {}

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
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="dourmouse-webui")
    thread.start()
    print(f"[DESKTOP] Dourmouse core online at {url}")
    if server.live_runtime is not None:
        print(
            f"[DESKTOP] {server.live_runtime.poll_count} always-on live agent poll loop(s) running"
        )

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
        bridge = DesktopBridge(map_window, webview, url)
        # v2.8: every agent gets its own live window at startup — created
        # BEFORE webview.start() (the documented thread-safe pattern), so
        # the whole roster is working and visible from the first frame.
        if open_all_windows:
            bridge.open_all_agents(registry.subagent_names)
        webview.create_window(
            "DOURMOUSE // CENTRAL AGENT DISPATCH",
            url,
            width=width,
            height=height,
            min_size=(1024, 680),
            js_api=bridge,
        )
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
        if server.live_runtime is not None:
            server.live_runtime.stop()
        if memory is not None:
            memory.close()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    import sys

    pid_file = ".dourmouse-ui.pid"
    try:
        with open(pid_file, "w") as fh:
            fh.write(str(os.getpid()))
    except OSError:
        pass
    try:
        sys.exit(launch())
    finally:
        try:
            os.remove(pid_file)
        except OSError:
            pass
