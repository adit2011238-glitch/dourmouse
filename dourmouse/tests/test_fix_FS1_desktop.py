"""FS1 fixes in dourmouse/desktop.py: P4-17, P4-18, P4-19."""

from __future__ import annotations

import sys
import types

import pytest

from dourmouse import desktop


class _Win:
    def move(self, *a):
        pass

    def resize(self, *a):
        pass

    def show(self):
        pass


@pytest.fixture
def bridge(monkeypatch):
    b = desktop.DesktopBridge(_Win(), object(), "http://127.0.0.1:1")
    b.attach_atlas_window(_Win())
    monkeypatch.setattr(desktop, "sys", type("S", (), {"platform": "darwin"})())
    monkeypatch.setattr(b, "screen_size", lambda: {"width": 2000, "height": 1000})
    scripts: list[str] = []

    def fake_run(cmd, **kw):
        scripts.append(cmd[cmd.index("-e") + 1])
        return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(desktop.subprocess, "run", fake_run)
    return b, scripts


# -- P4-17 ------------------------------------------------------------------- #

EVIL = 'x" to activate\ndo shell script "touch /tmp/pwned"\ntell application id "x'


def test_bundle_id_with_a_quote_is_not_interpolated(bridge):
    b, scripts = bridge
    b.split_with_app("Notes", bundle_id=EVIL)
    assert scripts, "the name fallback should still run"
    script = scripts[0]
    assert "do shell script" not in script.replace('\\"', "")
    assert 'tell application id "x"' not in script
    assert 'tell application "Notes" to activate' in script


def test_a_real_bundle_id_is_still_used(bridge):
    b, scripts = bridge
    b.split_with_app("Spotify", bundle_id="com.spotify.client")
    assert 'tell application id "com.spotify.client"' in scripts[0]


def test_escape_handles_newlines_and_returns():
    assert desktop._applescript_escape('a\nb\r"c\\') == 'a\\nb\\r\\"c\\\\'


def test_name_with_newline_cannot_break_out(bridge):
    b, scripts = bridge
    b.split_with_app('Notes"\ndo shell script "id')
    lines = scripts[0].split("\n")
    assert not any(line.lstrip().startswith("do shell script") for line in lines)


# -- P4-18 ------------------------------------------------------------------- #

def _fake_appkit(monkeypatch, result):
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
            return "file:///Applications/Google Chrome.app"

        @staticmethod
        def openURLs_withApplicationAtURL_options_configuration_error_(urls, app, opts, cfg, err):
            return result

    appkit = types.ModuleType("AppKit")
    appkit.NSWorkspace = FakeWorkspace
    foundation = types.ModuleType("Foundation")
    foundation.NSURL = FakeNSURL
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setitem(sys.modules, "Foundation", foundation)


def test_chrome_launch_failure_tuple_is_reported_as_failure(monkeypatch):
    """PyObjC returns (running_app_or_None, NSError_or_None) for this
    selector (its last argument is an o^@ output pointer)."""
    _fake_appkit(monkeypatch, (None, "NSError: launch refused"))
    assert desktop.DesktopBridge._open_in_chrome("https://x.com") is False


def test_chrome_launch_success_tuple_is_success(monkeypatch):
    _fake_appkit(monkeypatch, ("<NSRunningApplication Chrome>", None))
    assert desktop.DesktopBridge._open_in_chrome("https://x.com") is True


# -- P4-19 ------------------------------------------------------------------- #

def test_notification_keeps_non_ascii_text(monkeypatch):
    scripts: list[str] = []
    monkeypatch.setattr(desktop.shutil, "which", lambda name: "/usr/bin/osascript")
    monkeypatch.setattr(
        desktop.subprocess, "run",
        lambda cmd, **kw: scripts.append(cmd[cmd.index("-e") + 1]),
    )
    desktop.DesktopNotifier._default_notify('EUR/£ spike "big"', "café \U0001F4C8\nline2")
    script = scripts[0]
    assert "£" in script and "café" in script and "\U0001F4C8" in script
    assert "\\u00a3" not in script and "\\ud83d" not in script
    assert 'with title "EUR/£ spike \\"big\\""' in script
