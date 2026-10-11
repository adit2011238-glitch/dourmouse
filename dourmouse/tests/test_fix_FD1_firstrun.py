"""Fix agent FD1: P3-18, save_config merges the .env line by line (export NAME=value, comments kept) and writes
it atomically with mode 0600."""

from __future__ import annotations

import os
import stat

import pytest
from dotenv import dotenv_values

from dourmouse import config, firstrun


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_CONFIG_DIR", str(tmp_path / "cfg"))
    for k in ("NVIDIA_API_KEY", "DOURMOUSE_LLM_BACKEND", "OLLAMA_MODEL", "DOURMOUSE_SETUP_DONE"):
        monkeypatch.setenv(k, "x")
        monkeypatch.delenv(k)


def _path():
    p = config.user_env_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def test_export_prefixed_line_does_not_break_the_save():
    p = _path()
    p.write_text("export NVIDIA_API_KEY=oldkey\nOTHER=keep\n")
    r = firstrun.save_config({"NVIDIA_API_KEY": "newkey", "DOURMOUSE_LLM_BACKEND": "nvidia"})
    assert r["ok"] is True, r
    vals = dotenv_values(p)
    assert vals["NVIDIA_API_KEY"] == "newkey"
    assert vals["OTHER"] == "keep"
    assert vals["DOURMOUSE_LLM_BACKEND"] == "nvidia"
    assert list(vals).count("NVIDIA_API_KEY") == 1
    assert "export NVIDIA_API_KEY" not in p.read_text()


def test_comments_and_blank_lines_survive_a_save():
    p = _path()
    p.write_text("# my notes about keys\n\nFOO=bar  # trailing\n# another\nNVIDIA_API_KEY=old\n")
    firstrun.save_config({"NVIDIA_API_KEY": "new"})
    text = p.read_text()
    assert "# my notes about keys" in text and "# another" in text
    assert "FOO=bar  # trailing" in text
    assert dotenv_values(p)["NVIDIA_API_KEY"] == "new"


def test_a_quoted_value_round_trips_without_double_quoting():
    p = _path()
    p.write_text("")
    firstrun.save_config({"OLLAMA_MODEL": "a model with spaces"})
    firstrun.save_config({"DOURMOUSE_LLM_BACKEND": "ollama"})  # a second save rewrites around the first
    assert dotenv_values(p)["OLLAMA_MODEL"] == "a model with spaces"


def test_file_is_0600_and_no_temp_file_is_left():
    firstrun.save_config({"OLLAMA_MODEL": "m"})
    p = config.user_env_path()
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert [f for f in p.parent.iterdir() if f.name.endswith(".tmp")] == []


def test_an_unreadable_existing_file_is_left_untouched():
    p = _path()
    p.write_bytes(b"NVIDIA_API_KEY=\xff\xfe\x80broken\n")
    r = firstrun.save_config({"OLLAMA_MODEL": "m"})
    assert r["ok"] is False
    assert p.read_bytes() == b"NVIDIA_API_KEY=\xff\xfe\x80broken\n"
