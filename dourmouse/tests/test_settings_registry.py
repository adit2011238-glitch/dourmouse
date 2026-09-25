"""Finding #120 (OS-8.3): every background switch is readable and savable
without a terminal, validated, and persisted to the private config file."""

from __future__ import annotations

import stat

import pytest

from dourmouse import config
from dourmouse import settings_registry as sr


def test_every_runtime_switch_is_listed_with_its_current_value(monkeypatch):
    monkeypatch.setenv("DOURMOUSE_NETWATCH", "0")
    monkeypatch.delenv("DOURMOUSE_LIBRARIAN", raising=False)
    got = {f["key"]: f for f in sr.feature_settings()}
    assert got["DOURMOUSE_NETWATCH"]["value"] is False
    assert got["DOURMOUSE_LIBRARIAN"]["value"] is True  # default when unset
    assert all(f["label"] and f["help"] and f["restart_required"] for f in got.values())


def test_a_saved_switch_persists_privately_and_applies(monkeypatch):
    monkeypatch.setenv("DOURMOUSE_SECURITY_ANALYST", "1")
    r = sr.save_feature("DOURMOUSE_SECURITY_ANALYST", False)
    assert r == {"ok": True, "key": "DOURMOUSE_SECURITY_ANALYST", "value": False, "restart_required": True}
    assert config._read_user_config_file()["DOURMOUSE_SECURITY_ANALYST"] == "0"
    assert stat.S_IMODE(config.user_env_path().stat().st_mode) == 0o600
    from dourmouse.security.netwatch import analyst_enabled

    assert analyst_enabled() is False


def test_folders_are_validated(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_LIBRARIAN_ROOTS", "")
    r = sr.save_feature("DOURMOUSE_LIBRARIAN_ROOTS", f"{tmp_path}\n\n")
    assert r["value"] == [str(tmp_path)]
    for bad in ("relative/folder", str(tmp_path / "missing")):
        with pytest.raises(ValueError):
            sr.save_feature("DOURMOUSE_LIBRARIAN_ROOTS", bad)


def test_unknown_keys_and_wrong_types_are_refused():
    with pytest.raises(ValueError, match="not a setting"):
        sr.save_feature("OLLAMA_API_KEY", "steal")
    with pytest.raises(ValueError, match="on or off"):
        sr.save_feature("DOURMOUSE_NETWATCH", "yes")
