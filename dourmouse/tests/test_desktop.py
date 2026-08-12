"""Native desktop launcher tests (v2.5, dourmouse/desktop.py).

PyWebView opens a real GUI window, which can never run in a headless test
suite — so the window layer is exercised through the ``webview_loader`` test
seam with a fake webview module, while everything real (server lifecycle,
URL wiring, port selection, honest browser fallback, the Agent Map bridge)
runs against the actual code paths. No network, no GUI, no fabricated
results (Rule 2.1).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from dourmouse import desktop
from dourmouse.tests.test_webui import _echo_registry

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


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

    def create_window(self, title, url=None, **kwargs):
        win = _FakeWindow(title, url, **kwargs)
        self.windows.append(win)
        return win

    def start(self):
        self.started = True


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
            def start(self):
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
        # The ATLAS window is the dedicated strategy lab, visible, sized.
        assert atlas.url.endswith("/atlas-lab")
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

        fake = _FakeWebview()
        monkeypatch.setenv("DOURMOUSE_UI_PORT", "0")
        monkeypatch.setenv("DOURMOUSE_LEARN", "0")  # v2.9: hermetic — no real memory store

        def _probe_and_start():
            main = next(w for w in fake.windows if w.title.startswith("DOURMOUSE"))
            host, port = main.url.split("://")[1].rsplit(":", 1)
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
        result = subprocess.run(
            ["bash", "-n", str(script)],
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
        text = manifest.read_text()
        assert "pywebview" in text

    def test_start_command_launches_desktop_module(self):
        launcher = (_PROJECT_ROOT / "start.command").read_text()
        assert "dourmouse.desktop" in launcher
        assert "requirements-desktop.txt" in launcher

    def test_build_app_command_uses_osacompile(self):
        builder = (_PROJECT_ROOT / "build_app.command").read_text()
        assert "osacompile" in builder
