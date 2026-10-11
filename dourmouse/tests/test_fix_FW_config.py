"""FW A-14 / A-15: the user config file writers.

A-14: reading must understand the same dotenv syntax the file is loaded with
(`export KEY=...`, quotes), and writing must quote values so what is saved is
what is read back (a value containing ' #' used to be truncated).
A-15: the file holding the API keys is created 0600 from the first byte and
replaced atomically; a new config directory is 0700.
"""
from __future__ import annotations

import os
import stat

import pytest
from dotenv import dotenv_values

from dourmouse import config


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    d = tmp_path / "cfgdir"
    monkeypatch.setenv("DOURMOUSE_CONFIG_DIR", str(d))
    return d


def _env(cfg_dir):
    return cfg_dir / ".env"


# -- A-14 ---------------------------------------------------------------- #

def test_an_export_line_does_not_break_every_save(cfg):
    cfg.mkdir()
    _env(cfg).write_text("export FOO=bar\nOTHER=1\n", encoding="utf-8")
    assert config._read_user_config_file()["FOO"] == "bar"
    result = config.save_grounded_mode_setting(True)
    assert result["ok"] is True, result
    values = dotenv_values(_env(cfg))
    assert values["FOO"] == "bar" and values["OTHER"] == "1"
    assert config.save_auto_approve_setting(False)["ok"] is True
    assert config.save_orchestrator_model_setting("m", backend="ollama")["ok"] is True
    assert config.save_api_key_setting("GEMINI_API_KEY", "k")["ok"] is True


@pytest.mark.parametrize("secret", [
    "ab #cd", "a$b", "it's", 'say "hi"', "back\\slash", " lead", "trail ", "a=b",
    "'q'", '"q"', "#start", "k#", "$(id)", "x\\ny", "end\\", "eé ü",
])
def test_saved_key_reads_back_exactly(cfg, secret):
    result = config.save_api_key_setting("GEMINI_API_KEY", secret)
    assert result["ok"] is True, result
    assert dotenv_values(_env(cfg))["GEMINI_API_KEY"] == secret.strip()  # the saver strips
    assert config.api_key_setting("GEMINI_API_KEY") == secret.strip()


def test_saved_key_survives_the_real_loader_into_the_environment(cfg, monkeypatch):
    from dotenv import load_dotenv

    config.save_api_key_setting("OLLAMA_API_KEY", "ab #cd")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    load_dotenv(_env(cfg), override=True)
    assert os.environ["OLLAMA_API_KEY"] == "ab #cd"
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)


def test_an_interpolation_marker_in_a_new_value_is_refused_not_rewritten(cfg):
    result = config.save_api_key_setting("GEMINI_API_KEY", "abc${HOME}def")
    assert result["ok"] is False and "${" in result["detail"]
    assert not _env(cfg).exists() or "GEMINI_API_KEY" not in dotenv_values(_env(cfg))


def test_a_hand_written_interpolation_line_is_preserved_on_save(cfg):
    cfg.mkdir()
    _env(cfg).write_text("PATHISH=${HOME}/x\nQ=\"a b\"\n", encoding="utf-8")
    assert config.save_grounded_mode_setting(True)["ok"] is True
    raw = dotenv_values(_env(cfg), interpolate=False)
    assert raw["PATHISH"] == "${HOME}/x" and raw["Q"] == "a b"


def test_line_breaks_in_values_are_still_refused(cfg):
    with pytest.raises(ValueError):
        config.env_lines({"A": "x\nDOURMOUSE_AUTO_APPROVE=1"})
    result = config.save_api_key_setting("GEMINI_API_KEY", "x\nDOURMOUSE_AUTO_APPROVE=1")
    assert result["ok"] is False


def test_plain_values_are_still_written_unquoted():
    assert config.env_lines({"A": "plain-value_1.2"}) == ["A=plain-value_1.2"]


# -- A-15 ---------------------------------------------------------------- #

def test_new_config_file_is_private_from_creation_not_after_a_chmod(cfg, monkeypatch):
    old = os.umask(0o022)
    try:
        monkeypatch.setattr(config.os, "chmod", lambda *a, **k: None)  # nothing may rely on a later chmod
        assert config.save_api_key_setting("GEMINI_API_KEY", "k")["ok"] is True
    finally:
        os.umask(old)
    assert stat.S_IMODE(_env(cfg).stat().st_mode) == 0o600
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o700
    assert [p.name for p in cfg.iterdir()] == [".env"], "no temp file left behind"


def test_an_existing_loose_file_comes_back_private(cfg):
    cfg.mkdir()
    _env(cfg).write_text("A=1\n", encoding="utf-8")
    os.chmod(_env(cfg), 0o644)
    assert config.save_grounded_mode_setting(True)["ok"] is True
    assert stat.S_IMODE(_env(cfg).stat().st_mode) == 0o600


def test_a_failed_write_leaves_the_old_file_intact(cfg, monkeypatch):
    cfg.mkdir()
    _env(cfg).write_text("KEEP=me\n", encoding="utf-8")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(config.os, "replace", boom)
    result = config.save_grounded_mode_setting(True)
    assert result["ok"] is False and "disk full" in result["detail"]
    assert _env(cfg).read_text(encoding="utf-8") == "KEEP=me\n"
    assert [p.name for p in cfg.iterdir()] == [".env"]
