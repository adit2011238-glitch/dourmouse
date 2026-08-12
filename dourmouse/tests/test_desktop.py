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
    def test_launch_creates_main_map_and_agent_windows(self, monkeypatch):
        """v2.8: launch opens main + map + EVERY agent's own window at
        startup (open_all_windows=True default) so the whole roster is
        live and immediately working."""
        fake = _FakeWebview()
        monkeypatch.setenv("DOURMOUSE_UI_PORT", "0")
        monkeypatch.setenv("DOURMOUSE_LEARN", "0")  # v2.9: hermetic — no real memory store
        code = desktop.launch(_echo_registry(), port=0, webview_loader=_loader(fake))
        assert code == 0
        assert fake.started is True
        # main + map + one agent window (echo_agent in the echo registry)
        assert len(fake.windows) == 3

        titles = [w.title for w in fake.windows]
        assert "DOURMOUSE // CENTRAL AGENT DISPATCH" in titles
        assert "AGENT ORCHESTRATION MAP" in titles
        assert "AGENT // ECHO_AGENT" in titles

        main = next(w for w in fake.windows if w.title.startswith("DOURMOUSE"))
        map_win = next(w for w in fake.windows if w.title.startswith("AGENT ORCHESTRATION"))
        agent_win = next(w for w in fake.windows if w.title.startswith("AGENT //"))
        # Main window points at the live server, map at /map (hidden), agent
        # window at /agent/echo_agent — all pointing at the same live server.
        assert main.url.startswith("http://127.0.0.1:")
        assert map_win.url.endswith("/map")
        assert map_win.kwargs.get("hidden") is True
        assert agent_win.url.endswith("/agent/echo_agent")

    def test_launch_open_all_windows_opt_out(self, monkeypatch):
        """open_all_windows=False keeps the pre-v2.8 two-window behavior."""
        fake = _FakeWebview()
        monkeypatch.setenv("DOURMOUSE_UI_PORT", "0")
        monkeypatch.setenv("DOURMOUSE_LEARN", "0")  # v2.9: hermetic — no real memory store
        code = desktop.launch(
            _echo_registry(),
            port=0,
            webview_loader=_loader(fake),
            open_all_windows=False,
        )
        assert code == 0
        assert len(fake.windows) == 2  # main + map only

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
