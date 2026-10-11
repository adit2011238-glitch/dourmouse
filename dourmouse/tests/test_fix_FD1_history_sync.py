"""Fix agent FD1: P4-26 (touch -t in the remote zone), P4-27 (marker advanced past failed pulls), P4-28 (lstrip)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from dourmouse import history_sync
from dourmouse.memory_store import MemoryStore


def _ok(stdout=""):
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "m.db")
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _no_codex(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_CODEX_STATE_DB", str(tmp_path / "none.sqlite"))


def test_remote_touch_is_pinned_to_utc():
    seen = {}

    def fake(cmd, timeout):
        seen["cmd"] = cmd[-1]
        return _ok("")

    with patch.object(history_sync, "_run_ssh_command", side_effect=fake):
        history_sync.list_remote_changed_files("k", "u", "h", "~/r", 1705314600)
    assert "TZ=UTC touch -t 202401151030.00" in seen["cmd"]


def test_leading_dot_names_keep_their_dot():
    out = "./.archive/s.jsonl\n./proj/a.jsonl\n./..hidden/b.jsonl\n"
    with patch.object(history_sync, "_run_ssh_command", return_value=_ok(out)):
        got = history_sync.list_remote_changed_files("k", "u", "h", "~/r", 0)
    assert got == [".archive/s.jsonl", "proj/a.jsonl", "..hidden/b.jsonl"]


def _cfg(tmp_path):
    return {"host": "h", "user": "u", "key": "k", "remote_root": "~/r", "mirror_root": str(tmp_path / "mirror")}


def test_marker_does_not_advance_when_a_pull_failed(store, tmp_path):
    cfg = _cfg(tmp_path)
    with patch.object(history_sync, "list_remote_changed_files", return_value=["a.jsonl", "b.jsonl", "c.jsonl"]), \
            patch.object(history_sync, "pull_files", return_value=2):
        result = history_sync.sync_and_import(store, config=cfg)
    assert result["ok"] is False
    assert result["reason"] == "partial_pull"
    assert result["pulled"] == 2 and result["changed_remote_files"] == 3
    assert history_sync._read_marker(tmp_path / "mirror") == 0


def test_marker_advances_when_every_pull_succeeded(store, tmp_path):
    cfg = _cfg(tmp_path)
    with patch.object(history_sync, "list_remote_changed_files", return_value=["a.jsonl"]), \
            patch.object(history_sync, "pull_files", return_value=1):
        result = history_sync.sync_and_import(store, config=cfg)
    assert result["ok"] is True
    assert history_sync._read_marker(tmp_path / "mirror") > 0


def test_marker_write_is_atomic(tmp_path):
    mirror = tmp_path / "mirror"
    history_sync._write_marker(mirror, 99)
    assert history_sync._read_marker(mirror) == 99
    assert not list(tmp_path.glob(".mirror.last_sync.*.tmp"))
