"""Native desktop launcher tests (v2.5, dourmouse/desktop.py).

PyWebView opens a real GUI window, which can never run in a headless test
suite — so the window layer is exercised through the ``webview_loader`` test
seam with a fake webview module, while everything real (server lifecycle,
URL wiring, port selection, honest browser fallback, the Agent Map bridge)
runs against the actual code paths. No network, no GUI, no fabricated
results (Rule 2.1).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from dourmouse import desktop, webui
from dourmouse.tests.test_webui import _echo_registry

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _no_real_vision_helpers(monkeypatch):
    """v13: desktop.launch() now spawns tray.py/overlay.py/wakeword.py as
    real subprocesses by default (see vision_autostart_enabled()) — every
    existing test in this file calls launch() without knowing that's new,
    and none of them should actually open a system tray icon / native
    overlay window / start a mic-capture listener as a side effect of a
    hermetic unit test. Forced off here for the whole file; the dedicated
    TestVisionAutostart class below opts back in per-test with a fake
    spawn function to test the feature itself without real subprocesses."""
    monkeypatch.setenv(desktop._VISION_AUTOSTART_ENV, "0")


def _usable_bash() -> str | None:
    """Return a real bash executable, or None if only the WSL stub exists.

    On GitHub's Windows runners, ``bash`` on PATH resolves to the WSL shim
    ("Windows Subsystem for Linux has no installed distributions..." exit 1),
    not Git Bash — so ``bash -n`` fails there. Prefer Git Bash's canonical
    path when present, and verify the candidate actually runs.
    """
    candidates = []
    if os.name == "nt":
        candidates += [
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
        ]
    for cand in candidates:
        if Path(cand).exists():
            return cand
    on_path = shutil.which("bash")
    if on_path:
        try:
            r = subprocess.run([on_path, "--version"], capture_output=True,
                               timeout=10)
            if r.returncode == 0:
                return on_path
        except (OSError, subprocess.TimeoutExpired):
            pass
    return None


# --------------------------------------------------------------------------- #
# Fake webview module (no GUI possible in CI)
# --------------------------------------------------------------------------- #

class _FakeWindow:
    def __init__(self, title: str, url: str, **kwargs):
        self.title = title
        self.url = url
        self.kwargs = kwargs
        self.shown = False

    def show(self) -> None:
        self.shown = True


class _FakeWebview:
    """Mirrors the subset of the pywebview API launch() uses."""

    def __init__(self):
        self.windows: list[_FakeWindow] = []
        self.started = False
        self.start_kwargs: dict = {}

    def create_window(self, title, url=None, **kwargs):
        win = _FakeWindow(title, url, **kwargs)
        self.windows.append(win)
        return win

    def start(self, **kwargs):
        self.started = True
        self.start_kwargs = kwargs


def _loader(fake: _FakeWebview):
    return lambda: fake


# --------------------------------------------------------------------------- #
# Port selection
# --------------------------------------------------------------------------- #

class TestPickPort:
    def test_default_port(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_UI_PORT", raising=False)
        assert desktop._pick_port() == 8765

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_UI_PORT", "9999")
        assert desktop._pick_port() == 9999

    def test_invalid_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_UI_PORT", "not-a-port")
        assert desktop._pick_port() == 8765


# --------------------------------------------------------------------------- #
# Honest fallback when pywebview is missing (Rule 2.2: never a silent stub)
# --------------------------------------------------------------------------- #

class TestBrowserFallback:
    def test_missing_webview_opens_browser_and_reports(self, monkeypatch, capsys):
        monkeypatch.setenv("DOURMOUSE_UI_PORT", "0")
        monkeypatch.setenv("DOURMOUSE_LEARN", "0")  # v2.9: hermetic — no real memory store  # ephemeral port for tests

        def _raise_not_configured():
            raise RuntimeError("NOT CONFIGURED: the native window needs pywebview")

        opened: list[str] = []
        monkeypatch.setattr(desktop, "webbrowser", type("_WB", (), {"open": opened.append})())
        monkeypatch.setattr(desktop, "_wait_forever", lambda: None)

        code = desktop.launch(
            _echo_registry(),
            port=0,
            webview_loader=_raise_not_configured,
        )
        assert code == 1
        assert len(opened) == 1
        assert opened[0].startswith("http://127.0.0.1:")
        out = capsys.readouterr().out
        assert "NOT CONFIGURED" in out
        assert "Falling back to your default browser" in out


# --------------------------------------------------------------------------- #
# Native window launch (fake webview)
# --------------------------------------------------------------------------- #

class TestStartFailureFallback:
    def test_webview_start_failure_falls_back_to_browser(self, monkeypatch, capsys):
        """If the GUI backend fails at start() (headless session), degrade to
        the browser honestly instead of dying with a raw traceback."""
        monkeypatch.setenv("DOURMOUSE_UI_PORT", "0")
        monkeypatch.setenv("DOURMOUSE_LEARN", "0")  # v2.9: hermetic — no real memory store

        class _BrokenWebview(_FakeWebview):
            def start(self, **kwargs):
                raise RuntimeError("no GUI session available")

        opened: list[str] = []
        monkeypatch.setattr(desktop, "webbrowser", type("_WB", (), {"open": opened.append})())
        monkeypatch.setattr(desktop, "_wait_forever", lambda: None)

        code = desktop.launch(
            _echo_registry(),
            port=0,
            webview_loader=_loader(_BrokenWebview()),
        )
        assert code == 1
        assert len(opened) == 1
        assert opened[0].startswith("http://127.0.0.1:")
        out = capsys.readouterr().out
        assert "Native window could not start" in out
        assert "Falling back to your default browser" in out


class TestNativeWindowLaunch:
    def test_launch_default_is_single_main_window(self, monkeypatch):
        """v5.22.2/v5.22.6: default opens the main window + the ATLAS
        strategy-lab window, no agent windows at startup — agents open on
        demand from the UI. The map window is created hidden but counted."""
        fake = _FakeWebview()
        monkeypatch.setenv("DOURMOUSE_UI_PORT", "0")
        monkeypatch.setenv("DOURMOUSE_LEARN", "0")
        code = desktop.launch(_echo_registry(), port=0, webview_loader=_loader(fake))
        assert code == 0
        assert fake.started is True
        # main + hidden map window + ATLAS window (no agent windows at startup)
        assert len(fake.windows) == 3

        titles = [w.title for w in fake.windows]
        assert "DOURMOUSE // CENTRAL AGENT DISPATCH" in titles
        assert "AGENT ORCHESTRATION MAP" in titles
        assert "ATLAS // STRATEGY LAB" in titles

        main = next(w for w in fake.windows if w.title.startswith("DOURMOUSE"))
        map_win = next(w for w in fake.windows if w.title.startswith("AGENT ORCHESTRATION"))
        atlas = next(w for w in fake.windows if w.title.startswith("ATLAS"))
        # Main window points at the live server; map is hidden.
        assert main.url.startswith("http://127.0.0.1:")
        assert map_win.url.endswith("/map")
        assert map_win.kwargs.get("hidden") is True
        # v8.9: the ATLAS strategy-lab window is ALSO created hidden by
        # default now — two windows appearing unbidden at launch reads as a
        # malfunction to anyone who didn't build this (see desktop.py's own
        # comment on this change). It's still pre-created (the thread-safe
        # window-creation pattern) and revealed on demand through the
        # bridge, or at launch via DOURMOUSE_OPEN_ATLAS_LAB=1 (see the
        # dedicated opt-in test below) — this test pins the new default.
        assert atlas.url.endswith("/atlas-lab")
        assert atlas.kwargs.get("hidden") is True

    def test_launch_atlas_lab_opt_in_env_var_makes_it_visible(self, monkeypatch):
        """DOURMOUSE_OPEN_ATLAS_LAB=1 restores the pre-v8.9 behavior: the
        ATLAS window opens visible at launch instead of hidden."""
        fake = _FakeWebview()
        monkeypatch.setenv("DOURMOUSE_UI_PORT", "0")
        monkeypatch.setenv("DOURMOUSE_LEARN", "0")
        monkeypatch.setenv("DOURMOUSE_OPEN_ATLAS_LAB", "1")
        code = desktop.launch(_echo_registry(), port=0, webview_loader=_loader(fake))
        assert code == 0
        atlas = next(w for w in fake.windows if w.title.startswith("ATLAS"))
        assert atlas.kwargs.get("hidden") is not True

    def test_launch_open_all_windows_opt_out(self, monkeypatch):
        """open_all_windows=False keeps the pre-v2.8 window behavior."""
        fake = _FakeWebview()
        monkeypatch.setenv("DOURMOUSE_UI_PORT", "0")
        monkeypatch.setenv("DOURMOUSE_LEARN", "0")
        code = desktop.launch(
            _echo_registry(),
            port=0,
            webview_loader=_loader(fake),
            open_all_windows=True,
        )
        assert code == 0
        # main + map + ATLAS + one agent window (echo_agent in the echo registry)
        assert len(fake.windows) == 4
        titles = [w.title for w in fake.windows]
        assert "AGENT // ECHO_AGENT" in titles

    def test_proactive_surfacer_registers_on_the_real_sse_hub_by_default(self, monkeypatch):
        """Vision stage 7: launch() must wire a ProactiveSurfacer onto the
        SAME real hub the notifier uses, and unregister it again on
        shutdown -- exercised against the REAL _SSEBroadcast (not a fake),
        since that's what dourmouse.webui.run_server actually creates."""
        fake = _FakeWebview()
        monkeypatch.setenv("DOURMOUSE_UI_PORT", "0")
        monkeypatch.setenv("DOURMOUSE_LEARN", "0")
        seen_hubs = []
        real_register = None

        from dourmouse.webui import _SSEBroadcast

        real_register = _SSEBroadcast.register

        def spying_register(self, stream):
            seen_hubs.append(stream)
            return real_register(self, stream)

        monkeypatch.setattr(_SSEBroadcast, "register", spying_register)
        code = desktop.launch(_echo_registry(), port=0, webview_loader=_loader(fake))
        assert code == 0
        # both the notifier sink and the proactive sink registered
        assert len(seen_hubs) == 2
        emit_capable = [s for s in seen_hubs if hasattr(s, "emit")]
        assert len(emit_capable) == 2

    def test_proactive_surfacer_disabled_via_env(self, monkeypatch):
        fake = _FakeWebview()
        monkeypatch.setenv("DOURMOUSE_UI_PORT", "0")
        monkeypatch.setenv("DOURMOUSE_LEARN", "0")
        monkeypatch.setenv("DOURMOUSE_PROACTIVE_SURFACE", "0")

        from dourmouse.webui import _SSEBroadcast

        real_register = _SSEBroadcast.register
        seen_hubs = []

        def spying_register(self, stream):
            seen_hubs.append(stream)
            return real_register(self, stream)

        monkeypatch.setattr(_SSEBroadcast, "register", spying_register)
        code = desktop.launch(_echo_registry(), port=0, webview_loader=_loader(fake))
        assert code == 0
        # only the notifier sink registered -- proactive surfacing opted out
        assert len(seen_hubs) == 1

    def test_bridge_opens_map_window(self, monkeypatch):
        fake = _FakeWebview()
        monkeypatch.setenv("DOURMOUSE_UI_PORT", "0")
        monkeypatch.setenv("DOURMOUSE_LEARN", "0")  # v2.9: hermetic — no real memory store
        desktop.launch(_echo_registry(), port=0, webview_loader=_loader(fake))
        map_win = next(w for w in fake.windows if w.title.startswith("AGENT ORCHESTRATION"))
        assert map_win.shown is False  # hidden until the bridge reveals it

        main = next(w for w in fake.windows if w.title.startswith("DOURMOUSE"))
        bridge = main.kwargs.get("js_api")
        assert bridge is not None
        bridge.open_map()
        assert map_win.shown is True

    def test_server_serves_roster_during_launch(self, monkeypatch):
        """The window must point at a REAL live server (not a stub)."""
        import http.client
        import urllib.parse

        fake = _FakeWebview()
        monkeypatch.setenv("DOURMOUSE_UI_PORT", "0")
        monkeypatch.setenv("DOURMOUSE_LEARN", "0")  # v2.9: hermetic — no real memory store

        def _probe_and_start(**_kwargs):
            main = next(w for w in fake.windows if w.title.startswith("DOURMOUSE"))
            # v13.6: real urllib.parse instead of naive string-splitting —
            # main.url now carries a real path (initial_href defaults to
            # "/workspace", see desktop.py's own comment), which the old
            # `.split("://")[1].rsplit(":", 1)` didn't account for (it
            # folded the path onto the port, e.g. "8765/workspace",
            # int()-crashed, and that crash's own except-branch fell
            # through to the REAL _wait_forever() — a genuine, by-design
            # infinite wait — hanging this exact test. urlsplit() parses
            # the URL correctly regardless of what path is present.
            parsed = urllib.parse.urlsplit(main.url)
            host, port = parsed.hostname, parsed.port
            conn = http.client.HTTPConnection(host, int(port), timeout=5)
            conn.request("GET", "/api/roster")
            resp = conn.getresponse()
            assert resp.status == 200
            body = resp.read().decode()
            assert '"subagents"' in body
            conn.close()
            fake.started = True

        monkeypatch.setattr(fake, "start", _probe_and_start)
        code = desktop.launch(_echo_registry(), port=0, webview_loader=_loader(fake))
        assert code == 0

    def test_launch_never_touches_the_real_workspace_sessions_dir_by_default(
        self, monkeypatch, tmp_path
    ):
        """Real, live-caught regression: launch() briefly resolved "the
        most recent session" via a real, unguarded filesystem lookup
        INSIDE itself. Every hermetic test in this file calls launch()
        directly with no DOURMOUSE_WORKSPACE override, so that lookup
        reached into this actual repo's real, shared, actively-growing
        workspace/sessions/ directory — picking up a real multi-hundred-KB
        session from live testing and racing other concurrently-running
        test processes writing to that same real directory. Confirmed live:
        this specific test genuinely hung. session_file must default to
        None and be threaded straight through, unresolved, unless a caller
        passes one explicitly."""
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))  # belt-and-suspenders isolation
        monkeypatch.setenv("DOURMOUSE_UI_PORT", "0")
        monkeypatch.setenv("DOURMOUSE_LEARN", "0")
        captured = {}
        real_run_server = webui.run_server

        def _capturing_run_server(*args, **kwargs):
            captured["session_file"] = kwargs.get("session_file")
            return real_run_server(*args, **kwargs)

        monkeypatch.setattr(webui, "run_server", _capturing_run_server)
        fake = _FakeWebview()
        code = desktop.launch(_echo_registry(), port=0, webview_loader=_loader(fake))
        assert code == 0
        # The parameter reaches run_server, unresolved -- launch() itself
        # never called most_recent_session_file().
        assert captured["session_file"] is None

    def test_launch_threads_an_explicit_session_file_through_to_run_server(
        self, monkeypatch, tmp_path
    ):
        """A caller (the real __main__ entry point) that DOES pass a real
        session_file gets it honored end to end -- a real message sent
        through the real running server lands in that exact file."""
        import http.client
        import urllib.parse

        monkeypatch.setenv("DOURMOUSE_UI_PORT", "0")
        monkeypatch.setenv("DOURMOUSE_LEARN", "0")
        target = tmp_path / "session_prior.jsonl"
        fake = _FakeWebview()

        def _probe_and_start(**_kwargs):
            main = next(w for w in fake.windows if w.title.startswith("DOURMOUSE"))
            parsed = urllib.parse.urlsplit(main.url)
            conn = http.client.HTTPConnection(parsed.hostname, int(parsed.port), timeout=5)
            conn.request(
                "POST", "/api/chat",
                body='{"prompt": "hi"}',
                headers={"Content-Type": "application/json"},
            )
            conn.getresponse().read()
            conn.close()
            fake.started = True

        monkeypatch.setattr(fake, "start", _probe_and_start)
        code = desktop.launch(
            _echo_registry(), port=0, webview_loader=_loader(fake), session_file=target,
        )
        assert code == 0
        assert target.exists()


# --------------------------------------------------------------------------- #
# Vision stage 6: generalized multi-window opener
# --------------------------------------------------------------------------- #

class TestGeneralizedTaskWindows:
    def _bridge(self):
        fake = _FakeWebview()
        map_window = fake.create_window("AGENT ORCHESTRATION MAP", "http://x/map", hidden=True)
        bridge = desktop.DesktopBridge(map_window, fake, "http://127.0.0.1:9999")
        return bridge, fake

    def test_opens_a_window_for_an_arbitrary_task_id(self):
        bridge, fake = self._bridge()
        ok = bridge.open_task_window("world-monitor", "/#/world", title="WORLD MONITOR")
        assert ok is True
        win = next(w for w in fake.windows if w.title == "WORLD MONITOR")
        assert win.url == "http://127.0.0.1:9999/#/world"

    def test_reuses_the_same_window_on_repeat_calls(self):
        bridge, fake = self._bridge()
        bridge.open_task_window("world-monitor", "/#/world")
        bridge.open_task_window("world-monitor", "/#/world")
        matching = [w for w in fake.windows if w.url.endswith("/#/world")]
        assert len(matching) == 1
        assert matching[0].shown is True  # brought to front on the 2nd call

    def test_recreates_after_the_user_closed_it(self):
        bridge, fake = self._bridge()
        bridge.open_task_window("t1", "/#/atlas")
        first = next(w for w in fake.windows if w.url.endswith("/#/atlas"))
        first.closed = True
        bridge.open_task_window("t1", "/#/atlas")
        matching = [w for w in fake.windows if w.url.endswith("/#/atlas")]
        assert len(matching) == 2  # a fresh window was created, not reused

    def test_hash_route_is_prefixed_with_slash(self):
        bridge, _fake = self._bridge()
        bridge.open_task_window("t1", "#/markets")
        win = next(w for w in _fake.windows if "markets" in w.url)
        assert win.url.endswith("/#/markets")

    def test_default_title_is_the_task_id_uppercased(self):
        bridge, fake = self._bridge()
        bridge.open_task_window("mail-watch", "/#/alerts")
        win = next(w for w in fake.windows if w.url.endswith("/#/alerts"))
        assert win.title == "MAIL-WATCH"

    def test_empty_task_id_is_refused(self):
        bridge, fake = self._bridge()
        before = len(fake.windows)
        assert bridge.open_task_window("", "/#/world") is False
        assert len(fake.windows) == before

    def test_non_path_non_hash_is_refused(self):
        """Guards the bridge against opening an arbitrary external URL --
        window creation stays scoped to this app's own server."""
        bridge, fake = self._bridge()
        before = len(fake.windows)
        assert bridge.open_task_window("evil", "https://example.com") is False
        assert len(fake.windows) == before

    def test_open_agent_delegates_to_open_task_window(self):
        bridge, fake = self._bridge()
        bridge.open_agent("researcher")
        win = next(w for w in fake.windows if w.url.endswith("/agent/researcher"))
        assert win.title == "AGENT // RESEARCHER"
        # reuse semantics preserved through the delegation
        bridge.open_agent("researcher")
        assert len([w for w in fake.windows if w.url.endswith("/agent/researcher")]) == 1

    def test_open_all_hands_delegates_to_open_task_window(self):
        bridge, fake = self._bridge()
        ok = bridge.open_all_hands("run-42", goal="ship the thing")
        assert ok is True
        win = next(w for w in fake.windows if w.url.endswith("/all-hands?run=run-42"))
        assert "SHIP THE THING" in win.title
        assert bridge.open_all_hands("") is False  # unchanged honest-empty behaviour

    def test_windows_from_different_task_kinds_all_coexist(self):
        """The generalization must not collapse distinct task kinds into one
        window -- an agent window, an ALL HANDS window, and an arbitrary
        hash-route window must all be independently trackable at once."""
        bridge, fake = self._bridge()
        bridge.open_agent("researcher")
        bridge.open_all_hands("run-1")
        bridge.open_task_window("world-monitor", "/#/world")
        urls = {w.url for w in fake.windows}
        assert any(u.endswith("/agent/researcher") for u in urls)
        assert any(u.endswith("/all-hands?run=run-1") for u in urls)
        assert any(u.endswith("/#/world") for u in urls)
        assert len(bridge._agent_windows) == 3


# --------------------------------------------------------------------------- #
# Launcher scripts must at least parse (bash -n)
# --------------------------------------------------------------------------- #

class TestLauncherScriptSyntax:
    @pytest.mark.parametrize(
        "rel",
        ["start.command", "stop.command", "build_app.command"],
    )
    def test_bash_n_passes(self, rel):
        script = _PROJECT_ROOT / rel
        if not script.exists():
            pytest.skip(f"{rel} not present in this checkout")
        bash = _usable_bash()
        if bash is None:
            pytest.skip(f"no usable bash on this runner ({rel})")
        result = subprocess.run(
            [bash, "-n", str(script)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{rel} failed bash -n: {result.stderr}"


# --------------------------------------------------------------------------- #
# v5.22.7: split-screen bridge
# --------------------------------------------------------------------------- #

class _FakeSplitWindow:
    """Minimal window with move/resize for split tests."""

    def __init__(self):
        self.x = 0
        self.y = 0
        self.w = 800
        self.h = 600
        self.moved = False
        self.shown = False

    def move(self, x, y):
        self.x, self.y = x, y
        self.moved = True

    def resize(self, w, h):
        self.w, self.h = w, h

    def show(self):
        self.shown = True


def _bridge_with_atlas(atlas=None):
    bridge = desktop.DesktopBridge(_FakeSplitWindow(), _FakeWebview(), "http://127.0.0.1:1")
    atlas = atlas or _FakeSplitWindow()
    bridge.attach_atlas_window(atlas)
    return bridge, atlas


class TestSystemBrowserBridge:
    """v5.22.11+: DesktopBridge.open_external — the system-browser hop for
    the Google sign-in bridge. PREFERS Google Chrome (AppKit bundle lookup),
    falls back to the default browser. http(s)-only; honest False on
    anything else or on failure; never crashes the webview."""

    @staticmethod
    def _no_chrome(monkeypatch):
        """Force the fallback path: Chrome lookup always fails."""
        monkeypatch.setattr(
            desktop.DesktopBridge, "_open_in_chrome",
            classmethod(lambda cls, url: False))

    @staticmethod
    def _chrome_ok(monkeypatch):
        """Force the Chrome path: the launch always succeeds."""
        monkeypatch.setattr(
            desktop.DesktopBridge, "_open_in_chrome",
            classmethod(lambda cls, url: True))

    def test_open_external_opens_https_via_default_browser_fallback(
            self, monkeypatch):
        self._no_chrome(monkeypatch)
        bridge, _ = _bridge_with_atlas()
        opened: list[str] = []
        monkeypatch.setattr(
            desktop.webbrowser, "open",
            lambda url: opened.append(url) or True)
        assert bridge.open_external(
            "https://accounts.google.com/o/oauth2/v2/auth?fake=1") is True
        assert opened and opened[0].startswith("https://accounts.google.com")

    def test_open_external_prefers_chrome_and_skips_default(self, monkeypatch):
        self._chrome_ok(monkeypatch)
        bridge, _ = _bridge_with_atlas()
        called: list[str] = []
        monkeypatch.setattr(
            desktop.webbrowser, "open",
            lambda url: called.append(url) or True)
        assert bridge.open_external("https://accounts.google.com/x") is True
        assert called == [], "webbrowser must not fire when Chrome launched"

    def test_open_external_chrome_failure_falls_back_to_default(
            self, monkeypatch):
        bridge, _ = _bridge_with_atlas()
        chrome_calls: list[str] = []
        monkeypatch.setattr(
            desktop.DesktopBridge, "_open_in_chrome",
            classmethod(lambda cls, url: chrome_calls.append(url) or False))
        opened: list[str] = []
        monkeypatch.setattr(
            desktop.webbrowser, "open",
            lambda url: opened.append(url) or True)
        assert bridge.open_external("https://example.com") is True
        assert chrome_calls and opened == ["https://example.com"]

    def test_open_external_http_ok_but_no_other_schemes(self, monkeypatch):
        # The scheme guard fires BEFORE any browser launch — neither Chrome
        # nor webbrowser may ever see a file:// / javascript: URL.
        bridge, _ = _bridge_with_atlas()
        raiser = lambda url: (_ for _ in ()).throw(AssertionError(f"reached browser: {url}"))
        monkeypatch.setattr(desktop.DesktopBridge, "_open_in_chrome",
                            classmethod(lambda cls, url: raiser(url)))
        monkeypatch.setattr(desktop.webbrowser, "open", raiser)
        assert bridge.open_external("file:///etc/passwd") is False
        assert bridge.open_external("javascript:alert(1)") is False
        assert bridge.open_external("not-a-url") is False
        assert bridge.open_external("") is False
        assert bridge.open_external(12345) is False
        # http stays allowed (guard is scheme, not port).
        monkeypatch.undo()
        self._no_chrome(monkeypatch)
        monkeypatch.setattr(desktop.webbrowser, "open", lambda url: True)
        assert bridge.open_external("http://127.0.0.1:8765/login") is True

    def test_open_external_failure_is_honest_false(self, monkeypatch):
        self._no_chrome(monkeypatch)
        bridge, _ = _bridge_with_atlas()
        monkeypatch.setattr(desktop.webbrowser, "open", lambda url: (_ for _ in ()).throw(RuntimeError("boom")))
        assert bridge.open_external("https://example.com") is False

    def test_open_external_never_raises_on_bad_input(self, monkeypatch):
        self._no_chrome(monkeypatch)
        bridge, _ = _bridge_with_atlas()
        assert bridge.open_external(None) is False


class TestOpenInChrome:
    """v5.22.12: the Chrome-preference itself — AppKit bundle lookup,
    honest fallbacks. AppKit/Foundation are faked so the suite is hermetic
    regardless of whether pyobjc is installed."""

    @staticmethod
    def _fake_appkit(monkeypatch, chrome_found: bool, open_ok: bool = True):
        """Install fake AppKit/Foundation modules with a scripted NSWorkspace."""
        import sys
        import types

        calls: dict[str, list] = {"opened": []}

        class FakeNSURL:
            @staticmethod
            def URLWithString_(s):
                return s

        class FakeWorkspace:
            @staticmethod
            def sharedWorkspace():
                return FakeWorkspace

            @staticmethod
            def URLForApplicationWithBundleIdentifier_(bundle):
                calls["looked_up"] = bundle
                return "file:///Applications/Google Chrome.app" if chrome_found else None

            @staticmethod
            def openURLs_withApplicationAtURL_options_configuration_error_(
                    urls, app, opts, cfg, err):
                calls["opened"] = list(urls)
                calls["app"] = app
                return open_ok

        appkit = types.ModuleType("AppKit")
        appkit.NSWorkspace = FakeWorkspace
        foundation = types.ModuleType("Foundation")
        foundation.NSURL = FakeNSURL
        monkeypatch.setitem(sys.modules, "AppKit", appkit)
        monkeypatch.setitem(sys.modules, "Foundation", foundation)
        return calls

    def test_launches_chrome_when_installed(self, monkeypatch):
        calls = self._fake_appkit(monkeypatch, chrome_found=True)
        monkeypatch.setattr(desktop.webbrowser, "open",
                            lambda url: (_ for _ in ()).throw(AssertionError("default must not fire")))
        assert desktop.DesktopBridge._open_in_chrome("https://accounts.google.com/x") is True
        assert calls["looked_up"] == "com.google.Chrome"
        assert calls["opened"] == ["https://accounts.google.com/x"]
        assert "Google Chrome.app" in calls["app"]

    def test_falls_back_when_chrome_missing(self, monkeypatch):
        calls = self._fake_appkit(monkeypatch, chrome_found=False)
        assert desktop.DesktopBridge._open_in_chrome("https://x.com") is False
        assert calls.get("opened") == []

    def test_honest_false_when_launch_fails(self, monkeypatch):
        self._fake_appkit(monkeypatch, chrome_found=True, open_ok=False)
        assert desktop.DesktopBridge._open_in_chrome("https://x.com") is False

    def test_falls_back_when_appkit_unavailable(self, monkeypatch):
        import sys
        monkeypatch.setitem(sys.modules, "AppKit", None)
        monkeypatch.setitem(sys.modules, "Foundation", None)
        assert desktop.DesktopBridge._open_in_chrome("https://x.com") is False


class TestSplitScreen:
    def test_attach_and_split_in_window_flag(self):
        bridge, atlas = _bridge_with_atlas()
        assert bridge.split_in_window() is True
        bridge.attach_atlas_window(None)
        assert bridge.split_in_window() is False

    def test_screen_size_fallback_without_appkit(self, monkeypatch):
        import sys
        monkeypatch.setitem(sys.modules, "AppKit", None)
        monkeypatch.setattr(
            desktop, "sys",
            type("S", (), {"platform": "darwin", "exit": sys.exit})(),
        )
        bridge, _ = _bridge_with_atlas()
        size = bridge.screen_size()
        assert size["width"] > 0 and size["height"] > 0

    def test_split_with_app_moves_atlas_left(self, monkeypatch):
        bridge, atlas = _bridge_with_atlas()
        monkeypatch.setattr(desktop, "sys",
                            type("S", (), {"platform": "darwin"})())
        monkeypatch.setattr(bridge, "screen_size", lambda: {"width": 2560, "height": 1440})
        monkeypatch.setattr(
            desktop.subprocess, "run",
            lambda *a, **k: type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
        )
        result = bridge.split_with_app("Spotify")
        assert result["atlas_half"] is True
        assert result["other_half"] is True
        assert atlas.moved is True
        assert atlas.w == 1280  # left half of 2560

    def test_split_non_macos_honest(self, monkeypatch):
        bridge, _ = _bridge_with_atlas()
        monkeypatch.setattr(desktop, "sys",
                            type("S", (), {"platform": "win32"})())
        result = bridge.split_with_app("Spotify")
        assert result["ok"] is False
        assert "macOS" in result["error"]

    def test_split_missing_app_name(self, monkeypatch):
        bridge, _ = _bridge_with_atlas()
        monkeypatch.setattr(desktop, "sys",
                            type("S", (), {"platform": "darwin"})())
        monkeypatch.setattr(bridge, "screen_size", lambda: {"width": 2560, "height": 1440})
        result = bridge.split_with_app("   ")
        assert result["ok"] is False
        assert "no app name" in result["error"]

    def test_split_osascript_accessibility_error_honest(self, monkeypatch):
        bridge, _ = _bridge_with_atlas()
        monkeypatch.setattr(desktop, "sys",
                            type("S", (), {"platform": "darwin"})())
        monkeypatch.setattr(bridge, "screen_size", lambda: {"width": 2560, "height": 1440})
        monkeypatch.setattr(
            desktop.subprocess, "run",
            lambda *a, **k: type("P", (), {
                "returncode": 1,
                "stdout": "",
                "stderr": "osascript is not allowed assistive access",
            })(),
        )
        result = bridge.split_with_app("Spotify")
        assert result["ok"] is False
        assert "Accessibility" in result["error"]
        # ATLAS half still moved — honest partial success.
        assert result["atlas_half"] is True

    def test_split_with_app_prefers_bundle_id(self, monkeypatch):
        """Reviewer-caught: targeting by bundle id (com.spotify.client)
        is immune to display names with invisible Unicode marks or
        quotes. The osascript script must use ``tell application id``."""
        bridge, _ = _bridge_with_atlas()
        monkeypatch.setattr(desktop, "sys",
                            type("S", (), {"platform": "darwin"})())
        monkeypatch.setattr(bridge, "screen_size", lambda: {"width": 2560, "height": 1440})
        captured: dict = {}

        def fake_run(cmd, **kw):
            captured["script"] = cmd[cmd.index("-e") + 1]
            return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        monkeypatch.setattr(desktop.subprocess, "run", fake_run)
        result = bridge.split_with_app("\u200eWhatsApp", bundle_id="whatsapp.mac")
        assert result["other_half"] is True
        assert 'tell application id "whatsapp.mac"' in captured["script"]
        assert 'bundle identifier is "whatsapp.mac"' in captured["script"]
        # The invisible char must NOT appear anywhere in the script.
        assert "\u200e" not in captured["script"]

    def test_split_with_app_escapes_quoted_name(self, monkeypatch):
        """Without a bundle id, a display name containing a double quote
        must be AppleScript-escaped, not interpolated raw."""
        bridge, _ = _bridge_with_atlas()
        monkeypatch.setattr(desktop, "sys",
                            type("S", (), {"platform": "darwin"})())
        monkeypatch.setattr(bridge, "screen_size", lambda: {"width": 2560, "height": 1440})
        captured: dict = {}

        def fake_run(cmd, **kw):
            captured["script"] = cmd[cmd.index("-e") + 1]
            return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        monkeypatch.setattr(desktop.subprocess, "run", fake_run)
        result = bridge.split_with_app('Weird"App')
        assert result["other_half"] is True
        assert 'tell application "Weird\\"App"' in captured["script"]
        # Raw, unescaped quote must never reach osascript.
        assert 'Weird"App"' not in captured["script"]

    def test_list_running_apps_non_macos_empty(self, monkeypatch):
        bridge, _ = _bridge_with_atlas()
        monkeypatch.setattr(desktop, "sys",
                            type("S", (), {"platform": "linux"})())
        assert bridge.list_running_apps() == []


# --------------------------------------------------------------------------- #
# Packaging metadata
# --------------------------------------------------------------------------- #

class TestPackaging:
    def test_desktop_requirements_manifest(self):
        manifest = _PROJECT_ROOT / "requirements-desktop.txt"
        assert manifest.is_file()
        text = manifest.read_text(encoding="utf-8")
        assert "pywebview" in text

    def test_start_command_launches_desktop_module(self):
        launcher = (_PROJECT_ROOT / "start.command").read_text(encoding="utf-8")
        assert "dourmouse.desktop" in launcher
        assert "requirements-desktop.txt" in launcher

    def test_build_app_command_uses_osacompile(self):
        builder = (_PROJECT_ROOT / "build_app.command").read_text(encoding="utf-8")
        assert "osacompile" in builder

    def test_build_app_command_no_longer_self_locates_at_runtime(self):
        """v13.5, real live-reproduced bug: the applet used to find the
        project root via AppleScript's `path to me` (the bundle's OWN
        current location) — broke the instant a built .app was copied to
        ~/Applications (the whole point of "add it to my Applications
        folder"): `path to me` resolved to ~/Applications, `cd` landed
        there, and `bash ./start.command` failed outright (confirmed
        live: `ls ~/Applications/start.command` -> No such file or
        directory). Fixed by baking the real project root in at BUILD
        time instead. This pins the regression at the source level;
        TestBuildAppCommandBakesRealRoot below proves it end to end with
        a real osacompile + osadecompile round trip."""
        builder = (_PROJECT_ROOT / "build_app.command").read_text(encoding="utf-8")
        # The FUNCTIONAL AppleScript call, not the prose explaining the old
        # bug in this file's own comments (which legitimately still says
        # "path to me" describing what used to happen).
        assert "POSIX path of (path to me)" not in builder
        assert "PROJECT_ROOT" in builder


class TestBuildAppCommandBakesRealRoot:
    """Real end-to-end proof of the fix above: actually run build_app.command
    (real osacompile) against a throwaway output dir and decompile the
    resulting applet (real osadecompile) to confirm the literal, real
    project root path is baked into the compiled script — not a dynamic
    `path to me` call that breaks once the .app is moved."""

    def test_compiled_applet_contains_the_real_project_root(self, tmp_path):
        osacompile = shutil.which("osacompile")
        osadecompile = shutil.which("osadecompile")
        if not osacompile or not osadecompile:
            pytest.skip("osacompile/osadecompile not available on this runner (macOS-only)")
        bash = _usable_bash()
        if bash is None:
            pytest.skip("no usable bash on this runner")
        # tmp_path is a real pytest-managed directory, always on the same
        # filesystem class as the rest of the test run's tmp storage — a
        # deliberately different location than _PROJECT_ROOT, to actually
        # exercise the "app built/placed somewhere else" case this fix is
        # for, not just the same-directory default.
        result = subprocess.run(
            [bash, str(_PROJECT_ROOT / "build_app.command"), str(tmp_path)],
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, f"build_app.command failed: {result.stderr}"
        app_dir = tmp_path / "dourmouse.app"
        assert app_dir.is_dir()
        scpt = app_dir / "Contents" / "Resources" / "Scripts" / "main.scpt"
        assert scpt.is_file()
        decompiled = subprocess.run(
            [osadecompile, str(scpt)], capture_output=True, text=True, timeout=30,
        )
        assert decompiled.returncode == 0, decompiled.stderr
        source = decompiled.stdout
        assert "path to me" not in source
        assert str(_PROJECT_ROOT) in source  # the REAL root, baked in literally
        assert "bash ./start.command" in source


# --------------------------------------------------------------------------- #
# Vision helper auto-start (v13) — real bug fixed: nothing ever launched
# tray.py/overlay.py/wakeword.py, so the VISION screen honestly reported
# "not running" for everything, forever, on a normal app launch.
# --------------------------------------------------------------------------- #

class TestVisionAutostartConfig:
    def test_on_by_default(self, monkeypatch):
        monkeypatch.delenv(desktop._VISION_AUTOSTART_ENV, raising=False)
        assert desktop.vision_autostart_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "False", "no", "off", "OFF"])
    def test_explicit_off_values(self, monkeypatch, value):
        monkeypatch.setenv(desktop._VISION_AUTOSTART_ENV, value)
        assert desktop.vision_autostart_enabled() is False

    def test_explicit_on_value(self, monkeypatch):
        monkeypatch.setenv(desktop._VISION_AUTOSTART_ENV, "1")
        assert desktop.vision_autostart_enabled() is True


# --------------------------------------------------------------------------- #
# v13.11 ("package it as one application"): overlay/wakeword/tray now run
# IN this process (see desktop.py's own module comment for the real
# pystray/pywebview source-reading behind this) instead of as three
# subprocesses. Each helper is faked at its own real import boundary
# (dourmouse.overlay.create_overlay_window, dourmouse.wakeword.
# WakeWordListener, dourmouse.tray.TrayApp) exactly where
# _start_vision_helpers_in_process lazily imports it.
# --------------------------------------------------------------------------- #

class _FakeOverlayPoller:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class _FakeOverlayWindow:
    def __init__(self):
        class _Events:
            closed = None
        self.events = _Events()


def _fake_create_overlay_window(webview, base_url=None):
    return _FakeOverlayWindow(), _FakeOverlayPoller()


class _FakeWakeWordListener:
    instances: list["_FakeWakeWordListener"] = []

    def __init__(self, start_ok=True, start_reason="listening"):
        self.started = False
        self.stopped = False
        self._start_ok = start_ok
        self._start_reason = start_reason
        _FakeWakeWordListener.instances.append(self)

    def start(self):
        self.started = True
        return self._start_ok, self._start_reason

    def stop(self):
        self.stopped = True


class _FakeTrayApp:
    instances: list["_FakeTrayApp"] = []

    def __init__(self):
        self.run_detached_called = False
        self.stop_detached_called = False
        _FakeTrayApp.instances.append(self)

    def run_detached(self):
        self.run_detached_called = True

    def stop_detached(self):
        self.stop_detached_called = True


@pytest.fixture(autouse=True)
def _reset_fake_vision_helpers():
    _FakeWakeWordListener.instances = []
    _FakeTrayApp.instances = []
    yield
    _FakeWakeWordListener.instances = []
    _FakeTrayApp.instances = []


@pytest.fixture
def _patched_vision_helper_imports(monkeypatch):
    """Patches every real module _start_vision_helpers_in_process lazily
    imports from, so no test here ever touches a real camera, mic, or
    system tray."""
    import dourmouse.overlay as overlay_module
    import dourmouse.tray as tray_module
    import dourmouse.wakeword as wakeword_module

    monkeypatch.setattr(overlay_module, "create_overlay_window", _fake_create_overlay_window)
    monkeypatch.setattr(wakeword_module, "WakeWordListener", _FakeWakeWordListener)
    monkeypatch.setattr(tray_module, "TrayApp", _FakeTrayApp)


class TestSpawnVisionHelpers:
    def test_starts_all_three_helpers_in_process(self, _patched_vision_helper_imports):
        handles = desktop._start_vision_helpers_in_process(_FakeWebview(), "http://x")
        assert isinstance(handles.get("overlay_window"), _FakeOverlayWindow)
        assert isinstance(handles.get("overlay_poller"), _FakeOverlayPoller)
        assert len(_FakeWakeWordListener.instances) == 1
        assert _FakeWakeWordListener.instances[0].started is True
        assert handles["wakeword_listener"] is _FakeWakeWordListener.instances[0]
        assert len(_FakeTrayApp.instances) == 1
        assert _FakeTrayApp.instances[0].run_detached_called is True
        assert handles["tray_app"] is _FakeTrayApp.instances[0]

    def test_a_failing_helper_does_not_block_the_others(self, monkeypatch, capsys):
        import dourmouse.overlay as overlay_module
        import dourmouse.tray as tray_module
        import dourmouse.wakeword as wakeword_module

        def _broken_overlay(webview, base_url=None):
            raise RuntimeError("no display")

        monkeypatch.setattr(overlay_module, "create_overlay_window", _broken_overlay)
        monkeypatch.setattr(wakeword_module, "WakeWordListener", _FakeWakeWordListener)
        monkeypatch.setattr(tray_module, "TrayApp", _FakeTrayApp)

        handles = desktop._start_vision_helpers_in_process(_FakeWebview(), "http://x")
        assert "overlay_window" not in handles  # the broken one is simply absent
        assert "wakeword_listener" in handles  # the other two still started
        assert "tray_app" in handles
        assert "non-fatal" in capsys.readouterr().out

    def test_stop_stops_every_started_helper(self, _patched_vision_helper_imports):
        handles = desktop._start_vision_helpers_in_process(_FakeWebview(), "http://x")
        desktop._stop_vision_helpers_in_process(handles)
        assert handles["overlay_poller"].stopped is True
        assert handles["wakeword_listener"].stopped is True
        assert handles["tray_app"].stop_detached_called is True

    def test_stop_skips_a_helper_that_never_started(self, _patched_vision_helper_imports):
        """A helper missing from `handles` (it failed at startup) must be
        silently skipped, never crash the shutdown path."""
        desktop._stop_vision_helpers_in_process({})  # must not raise


class TestLaunchWithVisionAutostart:
    def test_launch_starts_helpers_in_process_by_default(
        self, monkeypatch, _patched_vision_helper_imports
    ):
        fake = _FakeWebview()
        monkeypatch.setenv("DOURMOUSE_UI_PORT", "0")
        monkeypatch.setenv("DOURMOUSE_LEARN", "0")
        monkeypatch.delenv(desktop._VISION_AUTOSTART_ENV, raising=False)
        code = desktop.launch(_echo_registry(), port=0, webview_loader=_loader(fake))
        assert code == 0
        assert len(_FakeWakeWordListener.instances) == 1
        assert len(_FakeTrayApp.instances) == 1
        # the launch's own finally block must have cleaned them all up
        assert _FakeWakeWordListener.instances[0].stopped is True
        assert _FakeTrayApp.instances[0].stop_detached_called is True

    def test_launch_respects_explicit_false(self, monkeypatch, _patched_vision_helper_imports):
        fake = _FakeWebview()
        monkeypatch.setenv("DOURMOUSE_UI_PORT", "0")
        monkeypatch.setenv("DOURMOUSE_LEARN", "0")
        code = desktop.launch(
            _echo_registry(), port=0, webview_loader=_loader(fake), vision_autostart=False
        )
        assert code == 0
        assert _FakeWakeWordListener.instances == []
        assert _FakeTrayApp.instances == []

    def test_launch_respects_env_var_off(self, monkeypatch, _patched_vision_helper_imports):
        fake = _FakeWebview()
        monkeypatch.setenv("DOURMOUSE_UI_PORT", "0")
        monkeypatch.setenv("DOURMOUSE_LEARN", "0")
        monkeypatch.setenv(desktop._VISION_AUTOSTART_ENV, "0")
        code = desktop.launch(_echo_registry(), port=0, webview_loader=_loader(fake))
        assert code == 0
        assert _FakeWakeWordListener.instances == []
        assert _FakeTrayApp.instances == []
