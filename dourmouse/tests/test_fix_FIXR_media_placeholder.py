"""FIX-R R-12: a conversion that cannot start is reported as failed, not left as "converting" forever."""

from __future__ import annotations

import time

import pytest

from dourmouse import media_convert as mc

INFO = {"ok": True, "video": ["hevc"], "audio": ["ac3"], "subtitles": [], "duration": 10.0}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    mc._jobs.clear()
    yield
    mc._jobs.clear()


def _movie(tmp_path):
    f = tmp_path / "film.mkv"
    f.write_bytes(b"not really a movie")
    return f


def test_an_unwritable_cache_folder_fails_the_job_instead_of_leaving_it_converting(tmp_path, monkeypatch):
    monkeypatch.setattr(mc, "probe", lambda src: INFO)
    monkeypatch.setattr(mc, "ffmpeg_exe", lambda: "/usr/bin/true")

    def broken_cache_dir():
        raise PermissionError("[Errno 13] Permission denied: 'media_cache' (the disk is read-only)")

    monkeypatch.setattr(mc, "cache_dir", broken_cache_dir)
    f = _movie(tmp_path)
    job = mc.ensure_playable(f)
    assert job["state"] == "failed" and "Permission denied" in job["error"] and job.get("failed_at")
    again = mc.ensure_playable(f)  # what the player's next poll sees
    assert again["state"] == "failed", "the placeholder must not stay 'converting' with no thread behind it"


def test_an_error_inside_plan_fails_the_job_too(tmp_path, monkeypatch):
    monkeypatch.setattr(mc, "probe", lambda src: INFO)
    monkeypatch.setattr(mc, "ffmpeg_exe", lambda: "/usr/bin/true")

    def bad_plan(info):
        raise KeyError("out_ext")

    monkeypatch.setattr(mc, "plan", bad_plan)
    job = mc.ensure_playable(_movie(tmp_path))
    assert job["state"] == "failed" and "KeyError" in job["error"]


def test_the_failure_is_retried_after_the_retry_window(tmp_path, monkeypatch):
    monkeypatch.setattr(mc, "probe", lambda src: INFO)
    monkeypatch.setattr(mc, "ffmpeg_exe", lambda: "/usr/bin/true")
    calls = {"n": 0}

    def flaky_cache_dir():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("volume not mounted")
        d = tmp_path / "cache"
        d.mkdir(exist_ok=True)
        return d

    monkeypatch.setattr(mc, "cache_dir", flaky_cache_dir)
    monkeypatch.setattr(mc, "_run", lambda *a, **k: None)
    f = _movie(tmp_path)
    assert mc.ensure_playable(f)["state"] == "failed"
    key = next(iter(mc._jobs))
    mc._jobs[key]["failed_at"] = time.time() - mc._RETRY_FAILED_S - 1
    assert mc.ensure_playable(f)["state"] == "converting"
