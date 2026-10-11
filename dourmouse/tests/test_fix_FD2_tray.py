"""FD2 fix P3-68: the kill switch fails closed on a bad state file and never undoes another process's change."""

from __future__ import annotations

import json
import os

import pytest

from dourmouse import tray


def _write(path, mic=True, camera=True):
    path.write_text(json.dumps({"mic_enabled": mic, "camera_enabled": camera, "updated_at": "x"}))


@pytest.mark.parametrize(
    "content",
    ["{not valid json", "[1, 2, 3]", '"just a string"', "", '{"mic_enabled": "false", "camera_enabled": true}',
     '{"mic_enabled": true}', '{"mic_enabled": null, "camera_enabled": null}'],
)
def test_unreadable_or_malformed_existing_file_means_killed(tmp_path, content):
    p = tmp_path / "state.json"
    p.write_text(content)
    s = tray.load_state(p)
    assert s.mic_enabled is False and s.camera_enabled is False
    assert tray.mic_allowed(p) is False and tray.camera_allowed(p) is False


def test_missing_file_is_still_a_fresh_install_both_enabled(tmp_path):
    s = tray.load_state(tmp_path / "nope.json")
    assert s.mic_enabled is True and s.camera_enabled is True


def test_toggling_mic_does_not_undo_a_camera_kill_made_elsewhere(tmp_path):
    p = tmp_path / "state.json"
    ks = tray.KillSwitch(path=p)  # loaded: both enabled
    _write(p, mic=True, camera=False)  # the overlay / another process kills the camera
    ks.set_mic(False)
    on_disk = tray.load_state(p)
    assert on_disk.mic_enabled is False
    assert on_disk.camera_enabled is False  # still killed


def test_state_property_reflects_another_process(tmp_path):
    p = tmp_path / "state.json"
    ks = tray.KillSwitch(path=p)
    assert ks.state.camera_enabled is True
    _write(p, mic=True, camera=False)
    assert ks.state.camera_enabled is False


def test_corrupt_file_is_kept_aside_on_write_and_camera_stays_killed(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("garbage {")
    ks = tray.KillSwitch(path=p)
    assert ks.state.mic_enabled is False and ks.state.camera_enabled is False
    ks.set_mic(True)  # a deliberate re-arm of the mic only
    asides = list(tmp_path.glob("state.json.corrupt-*"))
    assert len(asides) == 1 and asides[0].read_text() == "garbage {"
    final = tray.load_state(p)
    assert final.mic_enabled is True and final.camera_enabled is False


def test_save_is_atomic(tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    _write(p, mic=False, camera=False)
    before = p.read_text()

    def boom(src, dst):
        raise OSError("disk full")

    with monkeypatch.context() as m:
        m.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            tray.save_state(tray.KillSwitchState(), p)
    assert p.read_text() == before
    assert tray.load_state(p).mic_enabled is False
