"""FS2 P5-26/27/28/29: media_convert pipe deadlock, failed-job cache, subtitle glob, cover art."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from dourmouse import media_convert as mc


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    mc._jobs.clear()
    yield
    mc._jobs.clear()


def _fake_ffmpeg(tmp_path, body: str) -> str:
    p = tmp_path / "fakeff"
    p.write_text(f"#!{sys.executable}\nimport sys\nout = sys.argv[-1]\n{body}\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def _job(key="k1"):
    mc._jobs[key] = {"state": "converting", "progress": 0.0}
    return key


def test_run_survives_a_stderr_flood(tmp_path, monkeypatch):
    exe = _fake_ffmpeg(tmp_path, """
sys.stderr.write("Non-monotonous DTS in output stream\\n" * 60000)
sys.stderr.flush()
print("out_time_us=1000000", flush=True)
open(out, "wb").write(b"mp4data")
""")
    monkeypatch.setattr(mc, "ffmpeg_exe", lambda: exe)
    key = _job()
    out = tmp_path / "o.mp4"
    t = threading.Thread(target=mc._run, args=(key, tmp_path / "s.mkv", out, {"args": []}, 2.0), daemon=True)
    t.start()
    t.join(30)
    try:
        assert not t.is_alive(), "ffmpeg blocked on a full stderr pipe"
        assert mc._jobs[key]["state"] == "ready"
    finally:
        pid = mc._jobs[key].get("pid")
        if t.is_alive() and pid:
            os.kill(pid, 9)


def test_failed_run_reports_stderr_tail(tmp_path, monkeypatch):
    exe = _fake_ffmpeg(tmp_path, 'sys.stderr.write("bad stream\\nreal reason\\n")\nsys.exit(1)')
    monkeypatch.setattr(mc, "ffmpeg_exe", lambda: exe)
    key = _job()
    mc._run(key, tmp_path / "s.mkv", tmp_path / "o.mp4", {"args": []}, None)
    assert mc._jobs[key]["state"] == "failed" and "real reason" in mc._jobs[key]["error"]


def test_run_has_an_overall_timeout(tmp_path, monkeypatch):
    exe = _fake_ffmpeg(tmp_path, "import time\ntime.sleep(60)")
    monkeypatch.setattr(mc, "ffmpeg_exe", lambda: exe)
    monkeypatch.setattr(mc, "_MIN_RUN_LIMIT_S", 1.0)
    key = _job()
    t0 = time.monotonic()
    mc._run(key, tmp_path / "s.mkv", tmp_path / "o.mp4", {"args": []}, None)
    assert time.monotonic() - t0 < 15
    assert mc._jobs[key]["state"] == "failed" and "timed out" in mc._jobs[key]["error"]


def _src(tmp_path):
    src = tmp_path / "movie.mkv"
    src.write_bytes(b"x")
    return src


def test_missing_ffmpeg_failure_is_not_cached(tmp_path, monkeypatch):
    src = _src(tmp_path)
    monkeypatch.setattr(mc, "ffmpeg_exe", lambda: None)
    assert mc.ensure_playable(src)["state"] == "failed"
    calls = []

    def probe(p):
        calls.append(p)
        return {"ok": False, "error": "now installed but unreadable"}

    monkeypatch.setattr(mc, "probe", probe)
    assert "unreadable" in mc.ensure_playable(src)["error"]
    assert calls, "the failed probe result was served from the cache"


def test_failed_job_is_retried_after_the_retry_window(tmp_path, monkeypatch):
    src = _src(tmp_path)
    n = []

    def probe(p):
        n.append(1)
        return {"ok": False, "error": "disk full"}

    monkeypatch.setattr(mc, "probe", probe)
    monkeypatch.setattr(mc, "_RETRY_FAILED_S", 0.0)
    mc.ensure_playable(src)
    mc.ensure_playable(src)
    assert len(n) == 2


def test_failed_job_within_window_is_not_reprobed(tmp_path, monkeypatch):
    src = _src(tmp_path)
    n = []
    monkeypatch.setattr(mc, "probe", lambda p: n.append(1) or {"ok": False, "error": "bad"})
    monkeypatch.setattr(mc, "_RETRY_FAILED_S", 3600.0)
    mc.ensure_playable(src)
    mc.ensure_playable(src)
    assert len(n) == 1


def test_probe_timeout_is_caught_and_does_not_hold_the_lock(tmp_path, monkeypatch):
    slow, other = _src(tmp_path), tmp_path / "other.mkv"
    other.write_bytes(b"y")
    started, release = threading.Event(), threading.Event()

    def probe(p):
        if p == slow:
            started.set()
            release.wait(10)
            raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=60)
        return {"ok": False, "error": "other"}

    monkeypatch.setattr(mc, "probe", probe)
    res = {}
    t = threading.Thread(target=lambda: res.update(mc.ensure_playable(slow)), daemon=True)
    t.start()
    assert started.wait(5)
    t0 = time.monotonic()
    assert mc.ensure_playable(other)["error"] == "other"
    assert time.monotonic() - t0 < 2, "another media request waited for the slow probe"
    release.set()
    t.join(5)
    assert res["state"] == "failed" and "timed out" in res["error"]


def test_sidecar_subtitles_handles_brackets_and_prefix_names(tmp_path):
    (tmp_path / "Movie [2020].mkv").write_bytes(b"")
    (tmp_path / "Movie [2020].srt").write_text("1")
    (tmp_path / "Movie [2020].en.srt").write_text("1")
    (tmp_path / "Matrix.mkv").write_bytes(b"")
    (tmp_path / "Matrix.srt").write_text("1")
    (tmp_path / "Matrix Reloaded.srt").write_text("1")
    (tmp_path / "Matrix2.vtt").write_text("1")
    got = {Path(s["path"]).name: s["label"] for s in mc.sidecar_subtitles(tmp_path / "Movie [2020].mkv")}
    assert got == {"Movie [2020].srt": "default", "Movie [2020].en.srt": "en"}
    got = {Path(s["path"]).name for s in mc.sidecar_subtitles(tmp_path / "Matrix.mkv")}
    assert got == {"Matrix.srt"}


_FLAC_WITH_ART = """\
Input #0, flac, from 'song.flac':
  Duration: 00:03:00.00, start: 0.000000, bitrate: 900 kb/s
  Stream #0:0: Audio: flac, 44100 Hz, stereo, s16
  Stream #0:1: Video: mjpeg (Baseline), yuvj420p(pc, bt470bg/unknown), 501x501 [SAR 1:1 DAR 1:1], 90k tbr, 90k tbn (attached pic)
"""


def test_cover_art_is_not_counted_as_video(monkeypatch, tmp_path):
    monkeypatch.setattr(mc, "ffmpeg_exe", lambda: "ffmpeg")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(stderr=_FLAC_WITH_ART, returncode=0))
    info = mc.probe(tmp_path / "song.flac")
    assert info["video"] == [] and info["audio"] == ["flac"]
    p = mc.plan(info)
    assert p["out_ext"] == ".m4a" and "libx264" not in p["args"]


def test_real_video_stream_still_counted(monkeypatch, tmp_path):
    text = _FLAC_WITH_ART.replace(" (attached pic)", "").replace("mjpeg", "h264")
    monkeypatch.setattr(mc, "ffmpeg_exe", lambda: "ffmpeg")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(stderr=text, returncode=0))
    assert mc.probe(tmp_path / "x.mkv")["video"] == ["h264"]
