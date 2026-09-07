"""Tests for dourmouse/app_control.py — real macOS app control (backlog:
"control other apps in the background, like Claude Desktop"). Mocks
subprocess.run at the boundary (no assumption this test runs on a real
Mac with Accessibility permission granted) and asserts the exact
AppleScript built for each action, plus the honest error paths."""

from __future__ import annotations

import subprocess

import pytest

from dourmouse import app_control


class _FakeCompleted:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


@pytest.fixture
def fake_darwin(monkeypatch):
    monkeypatch.setattr(app_control.sys, "platform", "darwin")


@pytest.fixture
def capture_osascript(monkeypatch, fake_darwin):
    """Records every `osascript -e <script>` call and lets the test
    control what it returns."""
    calls: list[str] = []
    result = _FakeCompleted(stdout="")

    def fake_run(cmd, **kwargs):
        calls.append(cmd[2])  # ["osascript", "-e", script]
        return result

    monkeypatch.setattr(app_control.subprocess, "run", fake_run)
    return calls, result


class TestPlatformGate:
    def test_refuses_honestly_on_non_macos(self, monkeypatch):
        monkeypatch.setattr(app_control.sys, "platform", "linux")
        with pytest.raises(app_control.AppControlError, match="NOT CONFIGURED"):
            app_control.list_running_apps()


class TestBlocklist:
    @pytest.mark.parametrize("name", ["System Settings", "Keychain Access", "Finder", "finder"])
    def test_blocked_apps_refused_before_any_osascript(self, fake_darwin, monkeypatch, name):
        calls = []
        monkeypatch.setattr(app_control.subprocess, "run", lambda *a, **k: calls.append(1))
        with pytest.raises(app_control.AppControlError, match="REFUSED"):
            app_control.activate_app(name)
        assert calls == [], "blocklist must short-circuit before touching the system at all"


class TestListRunningApps:
    def test_parses_names_and_marks_frontmost(self, fake_darwin, monkeypatch):
        outputs = iter(["Finder, Safari, Claude", "Safari"])
        monkeypatch.setattr(
            app_control.subprocess,
            "run",
            lambda cmd, **k: _FakeCompleted(stdout=next(outputs)),
        )
        apps = app_control.list_running_apps()
        assert apps == [
            {"name": "Finder", "frontmost": False},
            {"name": "Safari", "frontmost": True},
            {"name": "Claude", "frontmost": False},
        ]


class TestActivateApp:
    def test_builds_the_real_activate_script(self, capture_osascript):
        calls, _ = capture_osascript
        out = app_control.activate_app("Claude")
        assert calls == ['tell application "Claude" to activate']
        assert out == "ACTIVATED: Claude"

    def test_escapes_quotes_in_app_name(self, capture_osascript):
        calls, _ = capture_osascript
        app_control.activate_app('Weird"App')
        assert calls == ['tell application "Weird\\"App" to activate']

    def test_empty_name_refused(self, fake_darwin):
        with pytest.raises(app_control.AppControlError, match="required"):
            app_control.activate_app("  ")


class TestSendKeystrokes:
    def test_script_activates_then_types(self, capture_osascript):
        calls, _ = capture_osascript
        out = app_control.send_keystrokes("Claude", "hello world")
        assert len(calls) == 1
        script = calls[0]
        assert 'tell application "Claude" to activate' in script
        assert 'keystroke "hello world"' in script
        assert out == "TYPED into Claude: 11 character(s)"

    def test_empty_text_refused(self, fake_darwin):
        with pytest.raises(app_control.AppControlError, match="required"):
            app_control.send_keystrokes("Claude", "")


class TestPressKey:
    def test_known_key_maps_to_key_code(self, capture_osascript):
        calls, _ = capture_osascript
        app_control.press_key("Claude", "return")
        assert "key code 36" in calls[0]

    def test_unknown_key_refused_honestly(self, fake_darwin):
        with pytest.raises(app_control.AppControlError, match="unknown key"):
            app_control.press_key("Claude", "banana")

    def test_modifiers_build_using_clause(self, capture_osascript):
        calls, _ = capture_osascript
        app_control.press_key("Claude", "return", ["command", "shift"])
        assert "using {command down, shift down}" in calls[0]

    def test_unknown_modifier_refused_honestly(self, fake_darwin):
        with pytest.raises(app_control.AppControlError, match="unknown modifier"):
            app_control.press_key("Claude", "return", ["hyper"])


class TestClickMenuItem:
    def test_two_level_path_builds_correct_chain(self, capture_osascript):
        calls, _ = capture_osascript
        app_control.click_menu_item("Claude", ["File", "New Window"])
        script = calls[0]
        assert (
            'click menu item "New Window" of menu "File" of menu bar item "File" of menu bar 1'
            in script
        )

    def test_three_level_path_nests_correctly(self, capture_osascript):
        calls, _ = capture_osascript
        app_control.click_menu_item("Claude", ["File", "Export", "PDF"])
        script = calls[0]
        assert (
            'click menu item "PDF" of menu "Export" of menu "File" '
            'of menu bar item "File" of menu bar 1' in script
        )

    def test_single_item_path_refused(self, fake_darwin):
        with pytest.raises(app_control.AppControlError, match="at least"):
            app_control.click_menu_item("Claude", ["File"])


class TestQuitApp:
    def test_builds_the_real_quit_script(self, capture_osascript):
        calls, _ = capture_osascript
        out = app_control.quit_app("Claude")
        assert calls == ['tell application "Claude" to quit']
        assert out == "QUIT: Claude"


class TestListWindows:
    def test_parses_window_titles(self, fake_darwin, monkeypatch):
        monkeypatch.setattr(
            app_control.subprocess, "run", lambda *a, **k: _FakeCompleted(stdout="Inbox, Drafts")
        )
        assert app_control.list_windows("Mail") == ["Inbox", "Drafts"]

    def test_empty_name_refused(self, fake_darwin):
        with pytest.raises(app_control.AppControlError, match="required"):
            app_control.list_windows("")


class TestHonestErrorSurfacing:
    def test_accessibility_permission_missing_reports_not_configured(self, fake_darwin, monkeypatch):
        monkeypatch.setattr(
            app_control.subprocess,
            "run",
            lambda *a, **k: _FakeCompleted(
                stderr="osascript: not allowed assistive access", returncode=1
            ),
        )
        with pytest.raises(app_control.AppControlError, match="NOT CONFIGURED.*Accessibility"):
            app_control.activate_app("Claude")

    def test_automation_permission_missing_reports_not_configured(self, fake_darwin, monkeypatch):
        monkeypatch.setattr(
            app_control.subprocess,
            "run",
            lambda *a, **k: _FakeCompleted(stderr="(-1743) osascript is not allowed", returncode=1),
        )
        with pytest.raises(app_control.AppControlError, match="NOT CONFIGURED.*Automation"):
            app_control.activate_app("Claude")

    def test_generic_failure_includes_real_stderr(self, fake_darwin, monkeypatch):
        monkeypatch.setattr(
            app_control.subprocess,
            "run",
            lambda *a, **k: _FakeCompleted(stderr="Application isn't running.", returncode=1),
        )
        with pytest.raises(app_control.AppControlError, match="Application isn't running"):
            app_control.activate_app("NotRunningApp")

    def test_timeout_reported_honestly(self, fake_darwin, monkeypatch):
        def raise_timeout(*a, **k):
            raise subprocess.TimeoutExpired(cmd="osascript", timeout=15)

        monkeypatch.setattr(app_control.subprocess, "run", raise_timeout)
        with pytest.raises(app_control.AppControlError, match="timed out"):
            app_control.activate_app("Claude")
