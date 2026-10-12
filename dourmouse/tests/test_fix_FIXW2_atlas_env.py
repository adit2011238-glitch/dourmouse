"""FIX-W2 H-FD2-4: the ATLAS scheduler loads .env with python-dotenv, from the repo root."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from dourmouse.atlas import atlas_scheduler as sched


@pytest.fixture(autouse=True)
def _private_environ(monkeypatch):
    """_load_env writes into os.environ; give each test its own copy so nothing leaks into other tests."""
    monkeypatch.setattr(os, "environ", dict(os.environ))


def test_quotes_export_and_comments_mean_what_they_mean_to_the_main_loader(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('export FOREX_DATA_PATH="/data/forex"\nTV_WEBHOOK_SECRET=\'s3\'  # note\nPLAIN=1\n', encoding="utf-8")
    for k in ("FOREX_DATA_PATH", "TV_WEBHOOK_SECRET", "PLAIN"):
        os.environ.pop(k, None)
    sched._load_env(env)
    assert os.environ["FOREX_DATA_PATH"] == "/data/forex"
    assert os.environ["TV_WEBHOOK_SECRET"] == "s3"
    assert os.environ["PLAIN"] == "1"


def test_a_real_environment_value_wins(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("PLAIN=from-file\n", encoding="utf-8")
    os.environ["PLAIN"] = "from-env"
    sched._load_env(env)
    assert os.environ["PLAIN"] == "from-env"


def test_a_missing_or_undecodable_file_is_quiet(tmp_path):
    sched._load_env(tmp_path / "none.env")
    bad = tmp_path / "bad.env"
    bad.write_bytes(b"\xff\xfe\x00bad")
    sched._load_env(bad)


def test_the_default_path_is_the_repo_root_not_the_package_directory():
    root = Path(sched.__file__).resolve().parents[2]
    assert sched._default_env_path() == root / ".env"
    assert (root / "dourmouse").is_dir()
