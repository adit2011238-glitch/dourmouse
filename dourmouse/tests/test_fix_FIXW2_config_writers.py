"""FIX-W2 R2-2 / R2-4 / R2-10 / H-FW-2: one line-preserving, locked, atomic writer
for the credentials file, shared by config.py, settings_registry.py and firstrun.py."""
from __future__ import annotations

import os
import threading

import pytest
from dotenv import dotenv_values

from dourmouse import config, firstrun
from dourmouse import settings_registry as sr


@pytest.fixture(autouse=True)
def cfg(monkeypatch, tmp_path):
    d = tmp_path / "cfgdir"
    monkeypatch.setenv("DOURMOUSE_CONFIG_DIR", str(d))
    return d


def _env():
    return config.user_env_path()


def _seed(text: str):
    _env().parent.mkdir(parents=True, exist_ok=True)
    _env().write_text(text, encoding="utf-8")


# -- R2-2: values that decode to a newline no longer break every save -------- #

@pytest.mark.parametrize("seed", [
    'A="line1\\nline2"\nB=1\n',
    'A="-----BEGIN KEY-----\nabc\n-----END KEY-----"\nB=1\n',
    'A="it\'s ${HOME}"\nB=1\n',
])
def test_existing_awkward_values_do_not_block_a_save_and_stay_byte_for_byte(seed):
    _seed(seed)
    result = config.save_grounded_mode_setting(True)
    assert result["ok"] is True, result
    text = _env().read_text(encoding="utf-8")
    assert seed.rstrip("\n") in text  # the awkward entry is untouched
    assert dotenv_values(_env())["DOURMOUSE_GROUNDED_MODE"] == "1"
    assert sr._write_user_setting("DOURMOUSE_NETWATCH", "0") is None
    assert seed.rstrip("\n") in _env().read_text(encoding="utf-8")


def test_every_config_saver_survives_a_multiline_value():
    _seed('PEM="a\nb\nc"\n')
    assert config.save_auto_approve_setting(False)["ok"]
    assert config.save_claude_front_mode_setting(True)["ok"]
    assert config.save_google_oauth_full_scopes_setting(True)["ok"]
    assert config.save_app_control_dry_run_setting(True)["ok"]
    assert config.save_orchestrator_model_setting("m", backend="ollama")["ok"]
    assert config.save_api_key_setting("GEMINI_API_KEY", "k")["ok"]
    assert dotenv_values(_env())["PEM"] == "a\nb\nc"


def test_changing_a_multiline_key_replaces_the_whole_block():
    _seed('KEEP=1\nGEMINI_API_KEY="x\ny"\nTAIL=2\n')
    assert config.save_api_key_setting("GEMINI_API_KEY", "new")["ok"]
    values = dotenv_values(_env())
    assert values["GEMINI_API_KEY"] == "new" and values["KEEP"] == "1" and values["TAIL"] == "2"
    assert "y\"" not in _env().read_text(encoding="utf-8")


def test_clearing_a_key_removes_only_that_entry():
    _seed("# note\nGEMINI_API_KEY=abc\nOTHER=1\n")
    assert config.save_api_key_setting("GEMINI_API_KEY", "")["ok"]
    text = _env().read_text(encoding="utf-8")
    assert "GEMINI_API_KEY" not in text and "# note" in text and "OTHER=1" in text


# -- R2-10: comments and order survive; "${" is refused in one place --------- #

def test_comments_and_order_survive_every_writer():
    _seed("# my note\nZED=1\n\n# another\nALPHA=2  # trailing\n")
    assert config.save_grounded_mode_setting(True)["ok"]
    sr._write_user_setting("DOURMOUSE_NETWATCH", "0")
    assert firstrun.save_config({"OLLAMA_MODEL": "m"})["ok"]
    text = _env().read_text(encoding="utf-8")
    assert "# my note" in text and "# another" in text and "# trailing" in text
    assert text.index("ZED=1") < text.index("ALPHA=2")


def test_env_lines_refuses_an_interpolation_marker():
    with pytest.raises(ValueError, match=r"\$\{"):
        config.env_lines({"K": "a${B}"})


def test_first_run_wizard_refuses_a_key_the_loader_would_expand():
    result = firstrun.save_config({"NVIDIA_API_KEY": "abc${X}"})
    assert result["ok"] is False and "${" in result["detail"]
    assert not _env().exists() or "NVIDIA_API_KEY" not in dotenv_values(_env())


# -- R2-4: unique temp, fsync, one lock, symlink, unreadable ----------------- #

def test_mixed_concurrent_writers_lose_nothing_and_raise_nothing():
    _seed("# keep me\nSEED=1\n")
    errors: list[BaseException] = []

    def run(i):
        try:
            if i % 3 == 0:
                sr._write_user_setting(f"DOURMOUSE_W2_S{i}", str(i))
            elif i % 3 == 1:
                res = config.save_api_key_setting("GEMINI_API_KEY", f"k{i}")
                assert res["ok"], res
            else:
                res = firstrun.save_config({"OLLAMA_MODEL": f"m{i}"})
                assert res["ok"], res
            # the file must always be readable and never empty while others write
            assert "SEED" in config._read_user_config_file()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(30)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors, errors
    saved = config._read_user_config_file()
    assert {f"DOURMOUSE_W2_S{i}" for i in range(0, 30, 3)} <= set(saved)
    assert "GEMINI_API_KEY" in saved and "OLLAMA_MODEL" in saved
    assert "# keep me" in _env().read_text(encoding="utf-8")
    assert [p.name for p in _env().parent.iterdir()] == [".env"], "no temp file left behind"


def test_the_write_is_fsynced_before_the_replace(monkeypatch):
    order: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(os, "replace", lambda a, b: (order.append("replace"), real_replace(a, b))[1])
    assert config.save_grounded_mode_setting(True)["ok"]
    assert order and order.index("fsync") < order.index("replace")


def test_a_symlinked_config_file_stays_a_symlink(tmp_path):
    real = tmp_path / "dotfiles" / "dourmouse.env"
    real.parent.mkdir()
    real.write_text("A=1\n", encoding="utf-8")
    _env().parent.mkdir(parents=True, exist_ok=True)
    _env().symlink_to(real)
    assert config.save_grounded_mode_setting(True)["ok"]
    assert _env().is_symlink()
    assert dotenv_values(real)["DOURMOUSE_GROUNDED_MODE"] == "1" and dotenv_values(real)["A"] == "1"


def test_config_savers_refuse_instead_of_overwriting_an_unreadable_file():
    _env().parent.mkdir(parents=True, exist_ok=True)
    raw = b"ANTHROPIC_API_KEY=sk-real\n\xff\xfe broken\n"
    _env().write_bytes(raw)
    result = config.save_grounded_mode_setting(True)
    assert result["ok"] is False and "not changed" in result["detail"]
    assert _env().read_bytes() == raw
    assert firstrun.save_config({"OLLAMA_MODEL": "m"})["ok"] is False
    assert _env().read_bytes() == raw


def test_new_file_is_private_and_the_directory_0700():
    import stat

    old = os.umask(0o022)
    try:
        assert config.save_grounded_mode_setting(True)["ok"]
    finally:
        os.umask(old)
    assert stat.S_IMODE(_env().stat().st_mode) == 0o600
    assert stat.S_IMODE(_env().parent.stat().st_mode) == 0o700
