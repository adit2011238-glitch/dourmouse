"""Tests for dourmouse/tray.py (Vision stage 3: tray + kill-switch).

Real pystray + Pillow ARE installed in this project's .venv (verified: both
import cleanly and can be instantiated headlessly — Icon/Menu construction
needs no display or menu-bar backend). What genuinely CANNOT be exercised
here is ``TrayApp.run()`` / ``pystray.Icon.run()`` itself: that blocks on
the real OS event loop and needs a live desktop session with an actual menu
bar, which this environment does not have. Everything up to (but not
including) that blocking call — state persistence, the kill-switch state
machine, menu construction, action handlers, and the icon bitmap itself
actually changing pixels on state change — is exercised for real below.
"""

from __future__ import annotations

import sys
import threading

import pytest

from dourmouse import tray


# --------------------------------------------------------------------------- #
# State path resolution
# --------------------------------------------------------------------------- #

class TestStatePath:
    def test_default_uses_workspace_env(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_PRIVACY_STATE", raising=False)
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", "/tmp/some-workspace")
        assert str(tray._state_path()) == "/tmp/some-workspace/privacy_state.json"

    def test_explicit_override_wins(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", "/tmp/some-workspace")
        monkeypatch.setenv("DOURMOUSE_PRIVACY_STATE", "/tmp/explicit.json")
        assert str(tray._state_path()) == "/tmp/explicit.json"

    def test_default_workspace_relative(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_PRIVACY_STATE", raising=False)
        monkeypatch.delenv("DOURMOUSE_WORKSPACE", raising=False)
        assert str(tray._state_path()) == "workspace/privacy_state.json"


# --------------------------------------------------------------------------- #
# Persisted state: load/save honesty
# --------------------------------------------------------------------------- #

class TestLoadSaveState:
    def test_missing_file_defaults_to_both_enabled(self, tmp_path):
        state = tray.load_state(tmp_path / "nope.json")
        assert state.mic_enabled is True
        assert state.camera_enabled is True

    def test_roundtrip(self, tmp_path):
        path = tmp_path / "state.json"
        original = tray.KillSwitchState(
            mic_enabled=False, camera_enabled=True, updated_at="2026-08-28T00:00:00"
        )
        tray.save_state(original, path)
        loaded = tray.load_state(path)
        assert loaded == original

    def test_corrupt_json_falls_back_to_defaults(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("{not valid json", encoding="utf-8")
        state = tray.load_state(path)
        assert state.mic_enabled is True
        assert state.camera_enabled is True

    def test_non_dict_json_falls_back_to_defaults(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        state = tray.load_state(path)
        assert state.mic_enabled is True
        assert state.camera_enabled is True

    def test_save_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "nested" / "dir" / "state.json"
        tray.save_state(tray.KillSwitchState(), path)
        assert path.exists()

    def test_mic_allowed_camera_allowed_helpers(self, tmp_path):
        path = tmp_path / "state.json"
        tray.save_state(
            tray.KillSwitchState(mic_enabled=False, camera_enabled=True), path
        )
        assert tray.mic_allowed(path) is False
        assert tray.camera_allowed(path) is True


# --------------------------------------------------------------------------- #
# KillSwitch state machine
# --------------------------------------------------------------------------- #

class TestKillSwitch:
    def test_default_state_both_enabled(self, tmp_path):
        ks = tray.KillSwitch(path=tmp_path / "state.json")
        assert ks.state.mic_enabled is True
        assert ks.state.camera_enabled is True

    def test_kill_all_disables_both_atomically(self, tmp_path):
        path = tmp_path / "state.json"
        ks = tray.KillSwitch(path=path)
        result = ks.kill_all()
        assert result.mic_enabled is False
        assert result.camera_enabled is False
        # persisted to disk, not just in-memory
        assert tray.load_state(path).mic_enabled is False
        assert tray.load_state(path).camera_enabled is False

    def test_kill_all_is_a_single_call_no_confirmation_hook(self, tmp_path):
        """The task requires ONE control, no confirmation dialog. Verify
        kill_all() takes no confirmation/callback argument and applies
        immediately — calling it is sufficient, nothing else required."""
        ks = tray.KillSwitch(path=tmp_path / "state.json")
        ks.kill_all()
        assert ks.state.mic_enabled is False and ks.state.camera_enabled is False

    def test_set_mic_independent_of_camera(self, tmp_path):
        ks = tray.KillSwitch(path=tmp_path / "state.json")
        ks.set_mic(False)
        assert ks.state.mic_enabled is False
        assert ks.state.camera_enabled is True

    def test_set_camera_independent_of_mic(self, tmp_path):
        ks = tray.KillSwitch(path=tmp_path / "state.json")
        ks.set_camera(False)
        assert ks.state.camera_enabled is False
        assert ks.state.mic_enabled is True

    def test_on_change_callback_invoked_with_new_state(self, tmp_path):
        seen = []
        ks = tray.KillSwitch(path=tmp_path / "state.json", on_change=seen.append)
        ks.kill_all()
        assert len(seen) == 1
        assert seen[0].mic_enabled is False
        assert seen[0].camera_enabled is False

    def test_broken_on_change_callback_does_not_corrupt_state(self, tmp_path):
        def _raiser(_state):
            raise RuntimeError("boom")

        ks = tray.KillSwitch(path=tmp_path / "state.json", on_change=_raiser)
        result = ks.kill_all()  # must not raise
        assert result.mic_enabled is False

    def test_updated_at_is_populated(self, tmp_path):
        ks = tray.KillSwitch(path=tmp_path / "state.json")
        result = ks.kill_all()
        assert result.updated_at != ""

    def test_state_property_returns_a_copy(self, tmp_path):
        ks = tray.KillSwitch(path=tmp_path / "state.json")
        snap = ks.state
        snap.mic_enabled = False  # mutating the returned copy...
        assert ks.state.mic_enabled is True  # ...must not affect internal state

    def test_concurrent_kills_do_not_crash_and_end_consistent(self, tmp_path):
        ks = tray.KillSwitch(path=tmp_path / "state.json")

        def _hammer():
            for _ in range(20):
                ks.kill_all()
                ks.set_mic(True)

        threads = [threading.Thread(target=_hammer) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        # No assertion on final value beyond "did not crash and is a valid
        # bool state" — concurrency races the actual value; the persisted
        # file must still be well-formed.
        final = tray.load_state(tmp_path / "state.json")
        assert isinstance(final.mic_enabled, bool)
        assert isinstance(final.camera_enabled, bool)


# --------------------------------------------------------------------------- #
# Honest degradation when pystray/Pillow are missing
# --------------------------------------------------------------------------- #

class TestImportPystray:
    def test_missing_pystray_raises_not_configured(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "pystray", None)
        with pytest.raises(RuntimeError, match="NOT CONFIGURED"):
            tray._import_pystray()

    def test_real_pystray_available_in_this_venv(self):
        """pystray + Pillow are genuinely installed here — confirms the
        happy path actually works, not just that the except-branch exists."""
        pystray_mod, image_module, draw_module = tray._import_pystray()
        assert pystray_mod.__name__ == "pystray"
        assert image_module.__name__.endswith("Image")
        assert draw_module.__name__.endswith("ImageDraw")


# --------------------------------------------------------------------------- #
# The icon bitmap itself encodes state (not just a tooltip)
# --------------------------------------------------------------------------- #

class TestBuildIconImage:
    def test_both_armed_dots_are_green(self):
        pystray_mod, Image, ImageDraw = tray._import_pystray()
        img = tray._build_icon_image(
            Image, ImageDraw, mic_enabled=True, camera_enabled=True
        )
        assert img.size == (tray._ICON_SIZE, tray._ICON_SIZE)
        mic_pixel = img.getpixel((21, 32))
        cam_pixel = img.getpixel((43, 32))
        assert mic_pixel == tray._COLOR_ON
        assert cam_pixel == tray._COLOR_ON

    def test_both_killed_dots_are_red(self):
        pystray_mod, Image, ImageDraw = tray._import_pystray()
        img = tray._build_icon_image(
            Image, ImageDraw, mic_enabled=False, camera_enabled=False
        )
        mic_pixel = img.getpixel((21, 32))
        cam_pixel = img.getpixel((43, 32))
        assert mic_pixel == tray._COLOR_OFF
        assert cam_pixel == tray._COLOR_OFF

    def test_mixed_state_dots_differ(self):
        pystray_mod, Image, ImageDraw = tray._import_pystray()
        img = tray._build_icon_image(
            Image, ImageDraw, mic_enabled=True, camera_enabled=False
        )
        mic_pixel = img.getpixel((21, 32))
        cam_pixel = img.getpixel((43, 32))
        assert mic_pixel == tray._COLOR_ON
        assert cam_pixel == tray._COLOR_OFF
        assert mic_pixel != cam_pixel


# --------------------------------------------------------------------------- #
# TrayApp: menu construction + action handlers (no blocking event loop)
# --------------------------------------------------------------------------- #

class TestTrayApp:
    def test_build_icon_reflects_current_state(self, tmp_path):
        ks = tray.KillSwitch(path=tmp_path / "state.json")
        app = tray.TrayApp(kill_switch=ks)
        icon = app._build_icon()
        assert "mic on" in icon.title
        assert "cam on" in icon.title

    def test_kill_all_action_updates_icon_title_and_state(self, tmp_path):
        ks = tray.KillSwitch(path=tmp_path / "state.json")
        app = tray.TrayApp(kill_switch=ks)
        app._icon = app._build_icon()
        app._on_kill_all(app._icon, None)
        assert ks.state.mic_enabled is False
        assert ks.state.camera_enabled is False
        assert "MIC KILLED" in app._icon.title
        assert "CAM KILLED" in app._icon.title

    def test_toggle_mic_action(self, tmp_path):
        ks = tray.KillSwitch(path=tmp_path / "state.json")
        app = tray.TrayApp(kill_switch=ks)
        app._icon = app._build_icon()
        app._on_toggle_mic(app._icon, None)
        assert ks.state.mic_enabled is False
        assert ks.state.camera_enabled is True  # untouched

    def test_toggle_camera_action(self, tmp_path):
        ks = tray.KillSwitch(path=tmp_path / "state.json")
        app = tray.TrayApp(kill_switch=ks)
        app._icon = app._build_icon()
        app._on_toggle_camera(app._icon, None)
        assert ks.state.camera_enabled is False
        assert ks.state.mic_enabled is True  # untouched

    def test_quit_action_stops_icon(self, tmp_path):
        ks = tray.KillSwitch(path=tmp_path / "state.json")
        app = tray.TrayApp(kill_switch=ks)

        stopped = []

        class _FakeIcon:
            def stop(self):
                stopped.append(True)

        app._on_quit(_FakeIcon(), None)
        assert stopped == [True]

    def test_menu_kill_item_is_default_no_confirmation(self, tmp_path):
        """The 'Kill camera + mic NOW' menu item must be reachable with a
        single action and marked default=True (task: no confirmation
        dialog to fight through)."""
        ks = tray.KillSwitch(path=tmp_path / "state.json")
        app = tray.TrayApp(kill_switch=ks)
        icon = app._build_icon()
        items = list(icon.menu)
        kill_items = [i for i in items if "Kill camera + mic" in str(i.text)]
        assert len(kill_items) == 1
        assert kill_items[0].default is True


class TestLaunch:
    def test_launch_reports_not_configured_honestly(self, monkeypatch, capsys):
        monkeypatch.setitem(sys.modules, "pystray", None)
        code = tray.launch()
        assert code == 1
        out = capsys.readouterr().out
        assert "NOT CONFIGURED" in out
