"""Finding #117 (OS-10): formats a browser cannot play are converted, the
cheapest way first, and the result is what a browser plays."""

from __future__ import annotations

import subprocess

import pytest

from dourmouse import media_convert as mc

FF = mc.ffmpeg_exe()
pytestmark = pytest.mark.skipif(FF is None, reason="ffmpeg (imageio-ffmpeg) not installed")


def _make(path, *args):
    subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-y", *args, str(path)], check=True, timeout=120)
    return path


def _video(tmp_path, name, vcodec, acodec):
    return _make(tmp_path / name, "-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=15", "-f", "lavfi",
                 "-i", "sine=frequency=440:duration=2", "-c:v", vcodec, "-c:a", acodec)


def test_plan_copies_what_a_browser_already_plays():
    assert mc.plan({"video": ["h264"], "audio": ["aac"]})["route"] == "remux"
    assert mc.plan({"video": ["hevc"], "audio": ["aac"]})["route"] == "transcode"
    p = mc.plan({"video": ["h264"], "audio": ["ac3"]})
    assert p["route"] == "transcode" and "copy" in p["args"][p["args"].index("-c:v") + 1]  # only audio re-encoded
    assert mc.plan({"video": [], "audio": ["flac"]})["out_ext"] == ".m4a"


def test_an_mkv_with_h264_is_remuxed_and_plays_as_mp4(tmp_path):
    src = _video(tmp_path, "movie.mkv", "libx264", "aac")
    job = mc.ensure_playable(src, wait_s=60)
    assert job["state"] == "ready" and job["route"] == "remux" and job["content_type"] == "video/mp4"
    out = mc.probe(__import__("pathlib").Path(job["path"]))
    assert out["video"] == ["h264"] and out["audio"] == ["aac"]
    assert mc.ensure_playable(src)["path"] == job["path"]  # cached: no second conversion


def test_an_avi_with_mpeg4_is_transcoded(tmp_path):
    src = _video(tmp_path, "old.avi", "mpeg4", "libmp3lame")
    job = mc.ensure_playable(src, wait_s=90)
    assert job["state"] == "ready" and job["route"] == "transcode"
    assert mc.probe(__import__("pathlib").Path(job["path"]))["video"] == ["h264"]


def test_lossless_audio_becomes_aac(tmp_path):
    src = _make(tmp_path / "song.flac", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:a", "flac")
    job = mc.ensure_playable(src, wait_s=60)
    assert job["state"] == "ready" and job["content_type"] == "audio/mp4"


def test_a_file_that_is_not_media_fails_honestly(tmp_path):
    bad = tmp_path / "fake.mkv"
    bad.write_bytes(b"this is not a video" * 50)
    job = mc.ensure_playable(bad, wait_s=10)
    assert job["state"] == "failed" and "ffmpeg cannot read this file" in job["error"]


def test_sidecar_subtitles_become_webvtt(tmp_path):
    media = tmp_path / "film.mkv"
    media.write_bytes(b"x")
    (tmp_path / "film.en.srt").write_text("1\n00:00:01,250 --> 00:00:02,000\nHi\n", encoding="utf-8")
    (tmp_path / "film.vtt").write_text("WEBVTT\n\n00:01.000 --> 00:02.000\nYo\n", encoding="utf-8")
    subs = mc.sidecar_subtitles(media)
    assert [s["label"] for s in subs] == ["en", "default"]
    assert mc.subtitle_vtt(tmp_path / "film.en.srt").startswith("WEBVTT\n\n1\n00:00:01.250 --> 00:00:02.000")
