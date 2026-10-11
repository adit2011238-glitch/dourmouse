"""FD2 fix P5-57: settings writers create the credentials file private, never overwrite an
unreadable one, serialise concurrent saves, and refuse a folder name the stored format cannot hold."""

from __future__ import annotations

import os
import stat
import threading

import pytest

from dourmouse import config
from dourmouse import settings_registry as sr


@pytest.fixture(autouse=True)
def cfg(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_CONFIG_DIR", str(tmp_path / "cfg"))


def test_new_file_is_never_world_readable_even_if_chmod_is_skipped(monkeypatch):
    monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
    monkeypatch.setattr(type(config.user_env_path()), "chmod", lambda *a, **k: None, raising=False)
    old = os.umask(0o022)
    try:
        sr._write_user_setting("DOURMOUSE_NETWATCH", "0")
    finally:
        os.umask(old)
    assert stat.S_IMODE(config.user_env_path().stat().st_mode) == 0o600


def test_unreadable_settings_file_is_not_overwritten():
    path = config.user_env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = b"ANTHROPIC_API_KEY=sk-real\n\xff\xfe broken bytes\n"
    path.write_bytes(raw)
    with pytest.raises(OSError) as exc:
        sr._write_user_setting("DOURMOUSE_NETWATCH", "0")
    assert "not changed" in str(exc.value) or "unreadable" in str(exc.value)
    assert path.read_bytes() == raw
    with pytest.raises(OSError):
        sr._remove_user_settings(["DOURMOUSE_NETWATCH"])
    assert path.read_bytes() == raw


def test_concurrent_saves_do_not_lose_each_other():
    errs = []

    def save(i):
        try:
            sr._write_user_setting(f"DOURMOUSE_FD2_KEY{i}", str(i))
        except Exception as exc:  # noqa: BLE001
            errs.append(exc)

    threads = [threading.Thread(target=save, args=(i,)) for i in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errs
    saved = config._read_user_config_file()
    assert {f"DOURMOUSE_FD2_KEY{i}" for i in range(16)} <= set(saved)


def test_other_keys_survive_a_write():
    sr._write_user_setting("ANTHROPIC_API_KEY", "sk-keep")
    sr._write_user_setting("DOURMOUSE_NETWATCH", "0")
    saved = config._read_user_config_file()
    assert saved["ANTHROPIC_API_KEY"] == "sk-keep" and saved["DOURMOUSE_NETWATCH"] == "0"


def test_folder_containing_the_path_separator_is_refused(tmp_path):
    odd = tmp_path / f"a{os.pathsep}b"
    odd.mkdir()
    with pytest.raises(ValueError, match="separator"):
        sr.save_feature("DOURMOUSE_LIBRARIAN_ROOTS", str(odd))
