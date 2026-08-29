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
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Callable

_PORT_ENV = "DOURMOUSE_UI_PORT"
_DEFAULT_PORT = 8765


def _pick_port() -> int:
    raw = os.environ.get(_PORT_ENV, "")
    try:
        return int(raw) if raw.strip() else _DEFAULT_PORT
    except ValueError:
        return _DEFAULT_PORT


# -- Vision helper auto-start (v13) ------------------------------------------
# Real gap found and fixed here: dourmouse/tray.py (system tray + camera/mic
# kill switch + the vision_bridge hand-tracking reaches into), overlay.py
# (the always-on-top ambient status window — its own docstring: "not
# something you open, something that is simply there"), and wakeword.py
# (the local wake-word listener) are all real, tested, shipped modules —
# but NOTHING launched any of them. A user opening the packaged desktop app
# got a VISION screen whose entire dashboard honestly reported "not
# running" for every single row, forever, because nothing ever started
# what it was reporting on. Each is independently runnable
# (`.venv/bin/python -m dourmouse.<module>`) but expecting a non-technical
# user to open three extra terminals to get the feature they see in the UI
# is not a real product. This starts each as its own subprocess (not a
# thread — each owns a blocking native event loop of its own: pystray's
# icon.run(), a second pywebview window, a continuous mic-capture loop —
# and pywebview in particular only tolerates one .start() per process) the
# same way `npm run dev` starts a dev server without the user typing it
# manually. Best-effort per helper: one failing to start (missing
# pystray/pyaudio, no display, whatever) never blocks the other two or the
# main app — this mirrors every other "real result or honest degrade,
# never block the app" contract in this codebase.
_VISION_AUTOSTART_ENV = "DOURMOUSE_VISION_AUTOSTART"
_VISION_HELPER_MODULES = ("dourmouse.tray", "dourmouse.overlay", "dourmouse.wakeword")


def vision_autostart_enabled() -> bool:
    raw = os.environ.get(_VISION_AUTOSTART_ENV, "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _spawn_vision_helper(module: str, port: int) -> subprocess.Popen | None:
    """Launch one helper module as a real background subprocess. Returns
    the Popen handle, or None on any failure to start (never raises —
    Rule 2.1/2.2 concern here is about the MAIN app's own startup, which
    must never fail because an optional ambient helper couldn't). Output
    is captured (DEVNULL) rather than left to inherit this process's
    stdout — three extra chatty subprocesses interleaving with
    "[DESKTOP] ..." lines would make the main app's own console output
    unreadable; each module already degrades honestly to its caller (exit
    code / self-contained window), not to stdout text a user is expected
    to read."""
    env = dict(os.environ)
    env[_PORT_ENV] = str(port)
    try:
        return subprocess.Popen(
            [sys.executable, "-m", module],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
    except OSError as exc:
        print(f"[DESKTOP] vision helper {module!r} could not start: {exc}")
        return None


def _start_vision_helpers(port: int) -> list[subprocess.Popen]:
    """Best-effort launch of every vision helper; see this section's own
    module-level comment for why these are subprocesses, not threads."""
    procs: list[subprocess.Popen] = []
    for module in _VISION_HELPER_MODULES:
        proc = _spawn_vision_helper(module, port)
        if proc is not None:
            procs.append(proc)
    return procs


def _stop_vision_helpers(procs: list[subprocess.Popen]) -> None:
    """Graceful terminate, then a bounded wait, then kill — the same
    escalation every process-supervision code in this codebase uses
    (atlas_command.py's AtlasRunManager included). Best-effort: a helper
    that's already dead or unkillable must never stop the main app's own
    shutdown from completing."""
    for proc in procs:
        try:
            if proc.poll() is not None:
                continue  # already exited on its own
            proc.terminate()
        except Exception:  # noqa: BLE001 - shutdown must never raise
            pass
    for proc in procs:
        try:
            proc.wait(timeout=3)
        except Exception:  # noqa: BLE001 - covers subprocess.TimeoutExpired
            # and any other polling failure; escalate to kill either way
            try:
                proc.kill()
            except Exception:  # noqa: BLE001 - best-effort
                pass


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


_SPLIT_EXCLUDE = {
    "python", "python3", "dourmouse", "finder", "loginwindow", "dock",
    "systemuiserver", "controlcenter", "notificationcenter", "windowserver",
    "touchbaragent", "spotlight", "wallpaper", "wifi", "stocks",
    "shortcuts", "textedit", "preview", "pages", "numbers", "terminal",
    "chronod", "coreautha", "talagentd", "universalaccessauthwarn",
}


def _applescript_escape(value: str) -> str:
    """Escape a string for safe interpolation inside AppleScript double
    quotes. Backslashes and double quotes are the two injection vectors
    (reviewer-caught: app display names can carry invisible Unicode marks
    like ``\u200eWhatsApp`` or quotes, so the native-split path prefers
    bundle ids — this is the fallback for names without a known id)."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _is_split_app(name: str) -> bool:
    """Is this app a sensible split-screen target? Excludes helper agents,
    services and system daemons — the picker should show apps the user
    actually chose to run (Chrome, Spotify, Claude, ...), not 60 helper
    processes."""
    low = name.lower()
    if low in _SPLIT_EXCLUDE:
        return False
    for junk in ("helper", "service", "agent", "web content", "web ui",
                 "graphics and media", "networking", "uielement",
                 "autofill", "open and save panel", "notification",
                 "siriactions", "uikit", "universal control", "wallpaper",
                 "viewbridge", "widget"):
        if junk in low:
            return False
    # Require a real bundle id with a dot (com.spotify.client) — filters out
    # bare daemon ids.
    return True


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
        self._atlas_window: Any | None = None
        self._agent_windows: dict[str, Any] = {}

    def attach_main_window(self, window: Any) -> None:
        """The shell's main window — created after the bridge, so attached
        once available (navigate() and close-persistence need it)."""
        self._main_window = window

    def attach_atlas_window(self, window: Any) -> None:
        """v5.22.7: the ATLAS strategy-lab window (for split-screen tiling)."""
        self._atlas_window = window

    def open_external(self, url: str) -> bool:
        """v5.22.11: open a URL in a REAL browser — Google Chrome first.

        The Google sign-in bridge lives here: Google refuses consent inside
        the embedded WebKit webview, so the login page hands this the consent
        URL. We PREFER Chrome (v5.22.12 — the user asked for Chrome
        specifically; Google's sign-in is never blocked there) via AppKit's
        bundle lookup, and fall back to the default browser via
        ``webbrowser.open`` when Chrome is missing or AppKit is unavailable
        (headless server, tests). Only http(s) URLs are ever accepted (no
        file:, no schemes) — returns False honestly otherwise. Never raises
        in a way that could crash the webview."""
        import webbrowser

        if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
            return False
        if self._open_in_chrome(url):
            return True
        try:
            return bool(webbrowser.open(url))
        except Exception:  # noqa: BLE001 -- the bridge never crashes the webview
            return False

    @staticmethod
    def _open_in_chrome(url: str) -> bool:
        """Launch the URL in Google Chrome via AppKit (bundle lookup — no
        assumptions about where Chrome lives; None when not installed).
        Honest False when Chrome is missing, AppKit is unavailable, or the
        launch fails — the caller falls back to the default browser."""
        try:
            from AppKit import NSWorkspace  # pyobjc ships with the desktop build
            from Foundation import NSURL

            workspace = NSWorkspace.sharedWorkspace()
            chrome = workspace.URLForApplicationWithBundleIdentifier_(
                "com.google.Chrome")
            if chrome is None:
                return False
            ok = workspace.openURLs_withApplicationAtURL_options_configuration_error_(
                [NSURL.URLWithString_(url)], chrome, 0, {}, None)
            return bool(ok)
        except Exception:  # noqa: BLE001 -- fall back to the default browser
            return False

    # -- v5.22.7: split-screen ------------------------------------------- #

    def screen_size(self) -> dict[str, int]:
        """The main display size in points, for split-screen math. Honest
        fallback: (2560, 1440) when the display cannot be queried."""
        try:
            from AppKit import NSScreen  # pyobjc ships with the desktop build
            frame = NSScreen.mainScreen().frame()
            return {"width": int(frame.size.width), "height": int(frame.size.height)}
        except Exception:  # noqa: BLE001 -- split math degrades to a sane default
            return {"width": 2560, "height": 1440}

    def list_running_apps(self) -> list[dict[str, str]]:
        """Running GUI apps (macOS) the user can split-screen ATLAS with.

        Uses ``lsappinfo`` (no permissions needed — it is a read-only
        listing). Returns [{name, path}] sorted by name; [] honestly when
        the query fails or is unavailable."""
        if not sys.platform.startswith("darwin"):
            return []
        try:
            proc = subprocess.run(
                ["lsappinfo", "list"], capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        # lsappinfo list format (macOS 13+): each app is a block like
        #     N) "Spotify" ASN:0x0-0x54054:
        #        bundleID="com.spotify.client"
        #        bundle path="..."
        # The display name is on the header line; bundleID on the next.
        apps: dict[str, str] = {}
        lines = proc.stdout.splitlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue
            header = re.match(r'^\d+\)\s+"([^"]+)"\s+ASN:', stripped)
            if header:
                name = header.group(1)
                bundle = ""
                # bundleID is on the first indented line after the header.
                for nxt in lines[i + 1 : i + 4]:
                    m = re.match(r'^\s+bundleID="([^"]+)"', nxt)
                    if m:
                        bundle = m.group(1)
                        break
                if bundle and _is_split_app(name):
                    apps[name] = bundle
        return [{"name": n, "bundle_id": b} for n, b in sorted(apps.items())]

    def split_with_app(self, app_name: str,
                       bundle_id: str | None = None) -> dict[str, Any]:
        """Tile the ATLAS window on the LEFT half of the screen and the
        named app's frontmost window on the RIGHT half (macOS).

        ``bundle_id`` (from ``list_running_apps``) is the preferred target:
        ``tell application id \"com.spotify.client\"`` is immune to display
        names carrying invisible Unicode marks or quotes. Without it, the
        name is AppleScript-escaped. Uses AppleScript + System Events
        (Accessibility permission required to move OTHER apps' windows;
        ATLAS itself is moved via pywebview, which needs no permission).
        Returns an honest dict: ok, atlas_half (always attempted),
        other_half (None when the other app window could not be moved),
        error (when osascript itself failed).
        """
        if not sys.platform.startswith("darwin"):
            return {"ok": False, "error": "split-screen is macOS-only"}
        size = self.screen_size()
        half_w = size["width"] // 2
        result: dict[str, Any] = {"ok": True, "atlas_half": True, "other_half": None}
        # 1) ATLAS window to the left half — pure pywebview, no permissions.
        try:
            atlas = self._atlas_window
            if atlas is not None and hasattr(atlas, "move") and hasattr(atlas, "resize"):
                atlas.move(0, 0)
                atlas.resize(half_w, size["height"])
                atlas.show()
        except Exception:  # noqa: BLE001 -- report, keep going
            result["atlas_half"] = False
        # 2) Bring the other app forward, then tile its front window right.
        app_name = (app_name or "").strip()
        if not app_name:
            result["ok"] = False
            result["error"] = "no app name given"
            return result
        # Target the app by bundle id when known — bundle ids are
        # [a-z0-9.-], always interpolation-safe. The display name is the
        # escaped fallback.
        if bundle_id:
            app_ref = f'id "{bundle_id}"'
            proc_ref = f'(first process whose bundle identifier is "{bundle_id}")'
        else:
            safe = _applescript_escape(app_name)
            app_ref = f'"{safe}"'
            proc_ref = f'"{safe}"'
        try:
            script = (
                f"tell application {app_ref} to activate\n"
                "delay 0.1\n"
                "tell application \"System Events\"\n"
                f"    tell {proc_ref}\n"
                "        set frontmost to true\n"
                "        if (count of windows) > 0 then\n"
                f"            set position of window 1 to {{{half_w}, 0}}\n"
                f"            set size of window 1 to {{{half_w}, {size['height']}}}\n"
                "        end if\n"
                "    end tell\n"
                "end tell"
            )
            proc = subprocess.run(
                ["osascript", "-e", script], capture_output=True, text=True, timeout=15,
            )
            if proc.returncode == 0:
                result["other_half"] = True
            else:
                err = (proc.stderr or "").strip()
                result["ok"] = False
                result["other_half"] = False
                if "assistive" in err.lower() or "accessibility" in err.lower() \
                        or "not allowed" in err.lower() or "not authorised" in err.lower():
                    result["error"] = (
                        "ATLAS is on the left half. To tile the other app's "
                        "window I need Accessibility permission — System "
                        "Settings → Privacy & Security → Accessibility → "
                        "enable the terminal/app that launched DourMouse."
                    )
                else:
                    result["error"] = f"could not tile '{app_name}': {err[:200]}"
        except (OSError, subprocess.TimeoutExpired) as exc:
            result["ok"] = False
            result["other_half"] = False
            result["error"] = f"osascript unavailable: {exc}"
        return result

    def split_in_window(self, url: str | None = None) -> bool:
        """v5.22.7: navigate the ATLAS window's second pane to an arbitrary
        URL (in-window split). The page handles the split UI; this simply
        returns True honestly when the window is attached (no-op on the
        browser fallback)."""
        return self._atlas_window is not None

    def open_all_hands(self, run_id: str, goal: str = "") -> bool:
        """v5.22.9: open the dedicated ALL HANDS window for one run.

        One window per run id (reused/brought to front, never duplicated —
        the same dedupe pattern as open_agent, now via open_task_window).
        Falls back honestly to False when no run id was given (the page
        then shows recent runs).
        """
        run_id = (run_id or "").strip()
        if not run_id:
            return False
        title = f"ALL HANDS // {(goal or run_id).strip()[:28].upper()}"
        return self.open_task_window(
            f"allhands:{run_id}", f"/all-hands?run={run_id}", title=title
        )

    def open_map(self) -> None:
        self._map_window.show()

    # -- Vision stage 6: generalized multi-window opener ------------------ #
    #
    # open_agent() and open_all_hands() below were the proof this pattern
    # works (each keys its own window into ``self._agent_windows`` by name/
    # run-id, reuses it on repeat calls, recreates it if the user closed it).
    # open_task_window() is that SAME pattern pulled out and generalized so
    # any future "task" — not just a subagent or an ALL HANDS run — gets its
    # own real, independently movable/resizable native window on request,
    # without a bespoke bridge method for every new task kind. Both existing
    # methods are now thin wrappers over it (unchanged signatures/return
    # types — console.html and any other caller of window.pywebview.api
    # keeps working exactly as before).
    #
    # ``path`` is not restricted to the handful of dedicated server routes
    # (``/agent/<name>``, ``/all-hands?run=<id>``, ``/atlas-lab``, ``/map``)
    # — it can equally be one of index.html's own SPA hash routes (e.g.
    # ``/#/world``, ``/#/atlas``, ``/#/markets`` — see VIEW_CYCLE in
    # ui/index.html), since ``{base_url}{path}`` for a hash path simply loads
    # the SAME single-page app and lets its client-side router land on that
    # view. That is what makes this "ANY running task", not "map and ATLAS
    # plus whatever gets a new server route later": every view already
    # reachable in the app can be popped into its own window today.
    def open_task_window(
        self,
        task_id: str,
        path: str,
        *,
        title: str | None = None,
        width: int = 980,
        height: int = 760,
        min_size: tuple[int, int] = (720, 540),
    ) -> bool:
        """Open (or focus) a real native window for one arbitrary task.

        ``task_id`` is any caller-chosen dedupe key (e.g. an agent name, an
        ALL HANDS run id, a SPA route) — one window per id, reused and
        brought to front on repeat calls, transparently recreated if the
        user closed it. Returns False honestly (no window touched) for an
        empty id or a path that isn't a same-origin path/hash (guards
        against a bad caller trying to open an arbitrary external URL
        through this bridge — window creation stays scoped to this app's
        own server, exactly like every other DesktopBridge window today).
        """
        task_id = (task_id or "").strip()
        path = (path or "").strip()
        if not task_id or not path or not path.startswith(("/", "#")):
            return False
        if not path.startswith("/"):
            path = "/" + path  # "#/world" -> "/#/world"
        win = self._agent_windows.get(task_id)
        if win is not None and not getattr(win, "closed", False):
            win.show()  # already open -> bring to front, don't duplicate
            return True
        win = self._webview.create_window(
            (title or task_id.upper())[:80],
            f"{self._base_url}{path}",
            width=width,
            height=height,
            min_size=min_size,
        )
        self._agent_windows[task_id] = win
        return True

    def open_agent(self, name: str) -> None:
        name = (name or "").strip()
        if not name:
            return
        self.open_task_window(name, f"/agent/{name}", title=f"AGENT // {name.upper()}")

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


def _brand_native_app() -> None:
    """Set the native macOS app identity — name in the Dock/menu bar,
    Force Quit window, and the app icon — instead of showing "Python".

    Best-effort (guarded): pyobjc may not be available, and the personal
    build ships pywebview which pulls pyobjc — so this normally works.
    Without it the app still runs, it just shows "Python" in the menu bar.
    """
    try:
        from AppKit import NSApplication, NSImage
        from Foundation import NSBundle

        app = NSApplication.sharedApplication()
        app.setName_("DourMouse")
        # Also set the bundle-level name so Force Quit, Dock tooltip, etc.
        # reflect it.
        bundle = NSBundle.mainBundle()
        info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
        if info:
            info.setObject_forKey_("DourMouse", "CFBundleName")
            info.setObject_forKey_("DourMouse", "CFBundleDisplayName")
        # Dock icon: use the bundled DourMouse.icns if found next to the app.
        icon_path = str(Path(__file__).resolve().parent.parent / "dourmouse.app" / "Contents" / "Resources" / "DourMouse.icns")
        if not Path(icon_path).is_file():
            # Try the installed /Applications path.
            icon_path = "/Applications/dourmouse-dist/dourmouse.app/Contents/Resources/DourMouse.icns"
        if Path(icon_path).is_file():
            img = NSImage.alloc().initWithContentsOfFile_(icon_path)
            if img:
                app.setApplicationIconImage_(img)
    except Exception:
        pass  # Best-effort: the app runs without branding.


def launch(
    registry: Any | None = None,
    *,
    host: str = "127.0.0.1",
    port: int | None = None,
    width: int = 1440,
    height: int = 900,
    webview_loader: Callable[[], Any] | None = None,
    live_polling: bool = True,
    open_all_windows: bool = False,
    deep_link: str | None = None,
    vision_autostart: bool | None = None,
) -> int:
    """Launch the DOURMOUSE desktop app. Returns a process exit code.

    Starts the web UI server on a background thread, opens a SINGLE native
    PyWebView window (no 30 agent windows at startup — they open on demand
    when you interact with an agent in the UI). Falls back to the default
    browser with an honest message when pywebview is unavailable.

    Pass ``open_all_windows=True`` only for testing (and if you really want
    every agent's window at launch — the pre-v5.22.2 behaviour).
    ``webview_loader`` is the test seam for ``_import_webview``.
    ``live_polling`` (v2.8): start the always-on live agent loops (env
    DOURMOUSE_LIVE=0 still disables them, see live_runtime.live_enabled).
    ``memory`` (v2.9): long-term store for the Store & Learn loop. None
    (default) opens the default store — the real app learns from every
    completed session (DOURMOUSE_LEARN=0 honestly disables it, see
    learn.open_default_store). Hermetic tests pass an explicit store or
    set DOURMOUSE_LEARN=0.
    ``vision_autostart`` (v13): spawn tray.py/overlay.py/wakeword.py as
    real background subprocesses so the VISION screen has something real
    to report on. None (default) follows DOURMOUSE_VISION_AUTOSTART (on
    unless explicitly set to 0/false/no/off) — see
    vision_autostart_enabled(). Hermetic tests pass False explicitly.
    """
    # v8.9: in the packaged Windows build every child process would open its
    # own console window, so launching the app flashed up a terminal beside
    # it. Applied here, before anything can shell out. No-op on macOS/Linux
    # and in a source checkout.
    try:
        from dourmouse import winquiet

        winquiet.install()
    except Exception:  # noqa: BLE001 - cosmetic; never block startup
        pass

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

    autostart = vision_autostart_enabled() if vision_autostart is None else vision_autostart
    vision_procs: list[subprocess.Popen] = _start_vision_helpers(actual_port) if autostart else []
    if autostart:
        print(f"[DESKTOP] {len(vision_procs)}/{len(_VISION_HELPER_MODULES)} vision helper(s) started")

    # The notification sink and its hub are initialized OUTSIDE the try so
    # the finally block can always reference them — a loader failure that
    # returns via the browser-fallback must not trip a NameError on cleanup
    # (reviewer-guard: the pre-existing fallback test catches this).
    notifier_sink = None
    proactive_sink = None
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
        # v5.22.6: the ATLAS window — a second DOURMOUSE that is ONLY the
        # strategy lab. A live leaderboard (best→worst) that auto-syncs from
        # the valerygordon200-byte/atlas-strategy-lab GitHub repo, so
        # strategies pushed from the other desktop appear automatically.
        # Opened at launch, sized smaller, positioned offset from the main
        # window; the bridge's open_agent-style reuse keeps it to ONE.
        # v8.9: the strategy lab no longer opens itself at launch. Two
        # windows appearing unbidden reads as a malfunction to anyone who
        # did not build this, and an installed app should open ONE window.
        # It is still created up-front (the thread-safe pattern above) and
        # revealed on demand through the bridge. Set
        # DOURMOUSE_OPEN_ATLAS_LAB=1 to restore the old launch behaviour.
        _atlas_at_launch = os.environ.get("DOURMOUSE_OPEN_ATLAS_LAB", "").strip() == "1"
        atlas_window = webview.create_window(
            "ATLAS // STRATEGY LAB",
            f"{url}/atlas-lab",
            width=1100,
            height=760,
            min_size=(820, 540),
            js_api=bridge,
            hidden=not _atlas_at_launch,
        )
        bridge.attach_atlas_window(atlas_window)
        try:
            # Open on the right of the main window when geometry is known,
            # otherwise let the OS tile it. pywebview Window geometry is
            # moved via move(x, y), not attribute assignment (reviewer-
            # caught: ``window.x = N`` silently no-ops).
            if (getattr(main_window, "x", None) is not None
                    and getattr(main_window, "width", None) is not None
                    and getattr(atlas_window, "move", None) is not None):
                atlas_window.move(
                    int(getattr(main_window, "x")) + int(getattr(main_window, "width")) + 8,
                    int(getattr(main_window, "y", 0) or 0),
                )
        except Exception:  # noqa: BLE001 -- tiling is best-effort
            pass
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
        # Vision stage 7: proactive surfacing — a SECOND, independent
        # consumer of the SAME alert feed above, filtered to a short
        # hardcoded allowlist (dourmouse.proactive.ALLOWED_ALERT_KINDS) and
        # rendered as a real small dismissible popup window instead of (as
        # well as) a native notification. Env-gated like the notifier above:
        # DOURMOUSE_PROACTIVE_SURFACE=0 disables.
        if os.environ.get("DOURMOUSE_PROACTIVE_SURFACE", "1") != "0" \
                and events_hub is not None:
            from dourmouse.proactive import ProactiveSurfacer, default_popup_factory

            surfacer = ProactiveSurfacer(url, default_popup_factory(webview))

            class _ProactiveSink:
                """Minimal SSE-stream-shaped sink for the broadcast hub —
                same shape as _HubSink above, a second independent
                registration on the same hub."""

                def emit(self, payload: dict[str, Any]) -> None:  # noqa: N802 -- SSE API
                    surfacer.on_event(payload)

            proactive_sink = _ProactiveSink()
            events_hub.register(proactive_sink)
        # Brand the native app before starting the window loop — sets the
        # Dock icon and menu-bar name to "DourMouse" instead of "Python".
        _brand_native_app()
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
        _stop_vision_helpers(vision_procs)
        if notifier_sink is not None and events_hub is not None:
            try:
                events_hub.unregister(notifier_sink)
            except Exception:  # noqa: BLE001 -- cleanup is best-effort
                pass
        if proactive_sink is not None and events_hub is not None:
            try:
                events_hub.unregister(proactive_sink)
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
