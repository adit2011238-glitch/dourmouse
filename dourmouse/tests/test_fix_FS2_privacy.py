"""FS2 P5-52: privacy mode fails closed when its settings file cannot be read."""

from __future__ import annotations

import json
import os

import pytest

from dourmouse.security import privacy


@pytest.fixture(autouse=True)
def _cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_CONFIG_DIR", str(tmp_path / "cfg"))
    (tmp_path / "cfg").mkdir()
    return tmp_path / "cfg"


def test_missing_file_means_off(_cfg):
    assert privacy.privacy_mode() is False


def test_valid_file_is_respected(_cfg):
    privacy.set_privacy_mode(True)
    assert privacy.privacy_mode() is True
    privacy.set_privacy_mode(False)
    assert privacy.privacy_mode() is False


@pytest.mark.parametrize("text", ["{not json", "", "[1, 2]", '"just a string"', "\x00\x01"])
def test_corrupt_file_means_on(_cfg, text):
    (_cfg / "security.json").write_text(text)
    assert privacy.privacy_mode() is True


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_unreadable_file_means_on(_cfg):
    f = _cfg / "security.json"
    f.write_text(json.dumps({"privacy_mode": False}))
    f.chmod(0)
    try:
        assert privacy.privacy_mode() is True
    finally:
        f.chmod(0o600)


def test_rewriting_a_corrupt_file_keeps_a_copy_of_it(_cfg):
    f = _cfg / "security.json"
    f.write_text("{broken but precious")
    privacy.set_privacy_mode(True)
    assert json.loads(f.read_text())["privacy_mode"] is True
    backups = [p for p in _cfg.iterdir() if p.name.startswith("security.json.corrupt")]
    assert backups and backups[0].read_text() == "{broken but precious"


def test_other_keys_survive_a_normal_toggle(_cfg):
    (_cfg / "security.json").write_text(json.dumps({"privacy_mode": False, "keep": 1}))
    privacy.set_privacy_mode(True)
    assert json.loads((_cfg / "security.json").read_text()) == {"privacy_mode": True, "keep": 1}
