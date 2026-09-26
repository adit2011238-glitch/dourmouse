"""Finding #152: the MEDIA screen's routes (os_api/media.py) on a port-0 server."""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from dourmouse.general_roster import build_general_registry


@pytest.fixture
def server(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(ws))
    monkeypatch.setenv("DOURMOUSE_CONFIG_DIR", str(tmp_path / "cfg"))
    home = tmp_path / "home"
    (home / "Movies").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    import dourmouse.media_convert as mc

    # the bundled ffmpeg can hang on a machine whose first-run scan is pending: never run it here
    monkeypatch.setattr(mc, "probe", lambda src: {"ok": False, "error": "ffmpeg is stubbed out in this test"})
    from dourmouse.webui import run_server

    srv = run_server(build_general_registry(), port=0, client=None, config=None)
    srv.test_home = home
    srv.test_ws = ws
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def call(srv, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    conn.request(method, path, body=json.dumps(body) if body is not None else None)
    resp = conn.getresponse()
    data = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, data


def test_library_starts_empty_and_names_the_fixed_folders_and_formats(server):
    status, data = call(server, "GET", "/api/os/media/library")
    assert status == 200 and data["recent"] == []
    names = {r["name"] for r in data["roots"]}
    assert {"workspace", "movies", "music", "downloads", "desktop", "documents"} <= names
    assert "mp3" in data["formats"]["audio"] and "mkv" in data["formats"]["convert_video"]
    assert "flac" in data["formats"]["convert_audio"]


def test_open_a_real_file_returns_urls_and_records_it_as_recent(server):
    f = server.test_home / "Movies" / "clip name.wav"
    f.write_bytes(b"RIFF" + b"\x00" * 64)
    status, data = call(server, "POST", "/api/os/media/open", {"path": str(f)})
    assert status == 200 and data["kind"] == "audio" and data["needs_convert"] is False
    assert data["name"] == "clip name.wav" and data["size"] == 68
    assert data["urls"]["media"] == "/api/files/media?path=" + str(f).replace(" ", "%20")
    assert data["subtitles"] == []
    recent = call(server, "GET", "/api/os/media/library")[1]["recent"]
    assert [r["name"] for r in recent] == ["clip name.wav"]


def test_a_convertible_container_is_flagged_and_an_image_is_accepted(server):
    mkv = server.test_home / "Movies" / "a.mkv"
    mkv.write_bytes(b"x")
    png = server.test_home / "Movies" / "p.png"
    png.write_bytes(b"x")
    assert call(server, "POST", "/api/os/media/open", {"path": str(mkv)})[1]["needs_convert"] is True
    d = call(server, "POST", "/api/os/media/open", {"path": str(png)})[1]
    assert d["kind"] == "image" and d["probe"] is None


def test_refusals_are_specific(server, tmp_path):
    outside = tmp_path / "elsewhere.mp3"
    outside.write_bytes(b"x")
    txt = server.test_home / "Movies" / "notes.txt"
    txt.write_text("hi")
    cases = [
        ({}, 400, "path is required"),
        ({"path": "relative/clip.mp3"}, 400, "full path"),
        ({"path": str(server.test_home / "Movies" / "missing.mp3")}, 404, "no file"),
        ({"path": str(txt)}, 415, "audio, video and images"),
        ({"path": str(outside)}, 403, "outside the folders"),
        ({"path": "x" * 2000}, 400, "not a usable"),
        ({"path": "/etc/passwd"}, 415, "audio, video and images"),
    ]
    for body, code, words in cases:
        status, data = call(server, "POST", "/api/os/media/open", body)
        assert status == code and words in data["error"], (body, status, data)


def test_traversal_and_symlink_escape_are_refused(server, tmp_path):
    outside = tmp_path / "secret.mp3"
    outside.write_bytes(b"x")
    movies = server.test_home / "Movies"
    (movies / "link.mp3").symlink_to(outside)
    status, data = call(server, "POST", "/api/os/media/open", {"path": str(movies / "link.mp3")})
    assert status == 403
    status, data = call(server, "POST", "/api/os/media/open", {"path": str(movies / ".." / ".." / "secret.mp3")})
    assert status in (403, 404)


def test_browse_lists_one_named_folder_newest_first_and_caps_it(server):
    movies = server.test_home / "Movies"
    for i in range(3):
        (movies / f"f{i}.mp4").write_bytes(b"x" * (i + 1))
    (movies / "skip.txt").write_text("no")
    (movies / ".hidden.mp4").write_bytes(b"x")
    (movies / "sub").mkdir()
    (movies / "sub" / "deep.mp4").write_bytes(b"x")
    d = call(server, "GET", "/api/os/media/library?root=movies")[1]
    assert d["exists"] and {f["name"] for f in d["files"]} == {"f0.mp4", "f1.mp4", "f2.mp4"}
    assert d["truncated"] is False and d["total"] == 3
    assert call(server, "GET", "/api/os/media/library?root=..%2Fetc")[0] == 400
    assert call(server, "GET", "/api/os/media/library?root=music")[1]["exists"] is False


def test_recent_list_drops_files_that_are_gone_and_is_capped(server):
    movies = server.test_home / "Movies"
    files = []
    for i in range(25):
        f = movies / f"r{i}.mp3"
        f.write_bytes(b"x")
        files.append(f)
        assert call(server, "POST", "/api/os/media/open", {"path": str(f)})[0] == 200
    recent = call(server, "GET", "/api/os/media/library")[1]["recent"]
    assert len(recent) == 20 and recent[0]["name"] == "r24.mp3"
    files[24].unlink()
    recent = call(server, "GET", "/api/os/media/library")[1]["recent"]
    assert recent[0]["name"] == "r23.mp3"


def test_a_corrupt_recent_file_reads_as_empty(server):
    server.test_ws.mkdir(parents=True, exist_ok=True)
    (server.test_ws / "media_recent.json").write_text("{not json")
    assert call(server, "GET", "/api/os/media/library")[1]["recent"] == []


def test_a_good_probe_fills_the_facts(server, monkeypatch):
    import dourmouse.media_convert as mc

    monkeypatch.setattr(mc, "probe", lambda src: {"ok": True, "video": ["h264"], "audio": ["aac"], "duration": 178.5, "subtitles": []})
    f = server.test_home / "Movies" / "v.mp4"
    f.write_bytes(b"x")
    (server.test_home / "Movies" / "v.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n")
    d = call(server, "POST", "/api/os/media/open", {"path": str(f)})[1]
    assert d["probe"] == {"video": ["h264"], "audio": ["aac"], "duration": 178.5} and d["probe_error"] is None
    assert d["subtitles"] and d["subtitles"][0]["url"].startswith("/api/files/subtitle.vtt?path=")


def test_an_ffmpeg_that_never_answers_costs_the_deadline_not_the_request(server, monkeypatch):
    import time

    import dourmouse.media_convert as mc
    from dourmouse.os_api import media

    monkeypatch.setattr(media, "PROBE_DEADLINE_S", 0.3)
    monkeypatch.setattr(mc, "probe", lambda src: time.sleep(5) or {"ok": True})
    f = server.test_home / "Movies" / "slow.mp4"
    f.write_bytes(b"x")
    t = time.monotonic()
    status, d = call(server, "POST", "/api/os/media/open", {"path": str(f)})
    assert status == 200 and time.monotonic() - t < 3
    assert d["probe"] is None and "did not answer" in d["probe_error"]
