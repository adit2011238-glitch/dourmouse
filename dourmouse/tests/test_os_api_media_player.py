"""Phase D: the MEDIA screen's PDF highlights, queue and player-state routes
(os_api/media.py), and the player_control event the screen listens for.

Real routes on a port-0 server with isolated HOME/workspace/config. ffmpeg is
stubbed (never run here); the PDF is a real file read by the real PDFium."""

from __future__ import annotations

import http.client
import json
import threading
import time
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
    (home / "Documents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    import dourmouse.media_convert as mc

    monkeypatch.setattr(mc, "probe", lambda src: {"ok": False, "error": "ffmpeg is stubbed out in this test"})
    from dourmouse.os_api import media as media_api

    media_api._player_state.clear()
    from dourmouse.webui import run_server

    srv = run_server(build_general_registry(), port=0, client=None, config=None)
    srv.test_home = home
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


def make_pdf(path: Path, pages: int = 3) -> Path:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument.new()
    for _ in range(pages):
        doc.new_page(200, 300)
    doc.save(str(path))
    return path


def make_audio(srv, name):
    f = srv.test_home / "Movies" / name
    f.write_bytes(b"ID3" + b"\0" * 64)
    return f


RECT = {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.05}


def test_open_pdf_returns_page_count_page_urls_and_no_highlights(server):
    pdf = make_pdf(server.test_home / "Documents" / "paper.pdf", pages=3)
    status, d = call(server, "POST", "/api/os/media/open", {"path": str(pdf)})
    assert status == 200 and d["kind"] == "pdf" and d["page_count"] == 3
    assert d["highlights"] == [] and d["pdf_error"] is None and d["probe"] is None
    assert d["urls"]["pdf_page"].endswith("&page=") and "pdf-page.png" in d["urls"]["pdf_page"]
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=20)
    conn.request("GET", d["urls"]["pdf_page"] + "1")
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    assert resp.status == 200 and body[:8] == b"\x89PNG\r\n\x1a\n", "the page URL the screen builds renders a real PNG"


def test_a_broken_pdf_reports_why_instead_of_a_page_count(server):
    bad = server.test_home / "Documents" / "broken.pdf"
    bad.write_bytes(b"not a pdf at all")
    status, d = call(server, "POST", "/api/os/media/open", {"path": str(bad)})
    assert status == 200 and d["page_count"] is None and d["pdf_error"]


def test_highlights_add_list_persist_remove_and_clear(server):
    pdf = make_pdf(server.test_home / "Documents" / "paper.pdf")
    p = str(pdf)
    status, d = call(server, "POST", "/api/os/media/highlights", {"path": p, "op": "add", "page": 1, "rect": RECT, "color": "green", "note": "  key claim "})
    assert status == 200 and len(d["highlights"]) == 1
    h = d["highlights"][0]
    assert h["page"] == 1 and h["color"] == "green" and h["note"] == "key claim" and h["rect"] == RECT and h["id"]
    call(server, "POST", "/api/os/media/highlights", {"path": p, "page": 0, "rect": RECT})  # default op=add, default colour
    status, d = call(server, "GET", "/api/os/media/highlights?path=" + p)
    assert [x["page"] for x in d["highlights"]] == [1, 0] and d["highlights"][1]["color"] == "yellow"
    # the stored list comes back with the file on open, and survives on disk
    _, opened = call(server, "POST", "/api/os/media/open", {"path": p})
    assert len(opened["highlights"]) == 2
    assert (Path(server.test_home).parent / "ws" / "media_highlights.json").is_file()
    status, d = call(server, "POST", "/api/os/media/highlights", {"path": p, "op": "remove", "id": h["id"]})
    assert status == 200 and len(d["highlights"]) == 1
    status, d = call(server, "POST", "/api/os/media/highlights", {"path": p, "op": "remove", "id": h["id"]})
    assert status == 404
    status, d = call(server, "POST", "/api/os/media/highlights", {"path": p, "op": "clear"})
    assert status == 200 and d["highlights"] == []


@pytest.mark.parametrize("patch,code", [
    ({"rect": {"x": 0.9, "y": 0.1, "w": 0.3, "h": 0.1}}, 400),   # runs off the page
    ({"rect": {"x": 0.1, "y": 0.1, "w": 0.0, "h": 0.1}}, 400),   # too small
    ({"rect": {"x": -0.1, "y": 0.1, "w": 0.3, "h": 0.1}}, 400),  # outside 0..1
    ({"rect": {"x": "a", "y": 0.1, "w": 0.3, "h": 0.1}}, 400),
    ({"rect": None}, 400),
    ({"page": -1}, 400),
    ({"page": "x"}, 400),
    ({"color": "orange"}, 400),
    ({"op": "explode"}, 400),
])
def test_highlight_input_is_validated(server, patch, code):
    pdf = make_pdf(server.test_home / "Documents" / "paper.pdf")
    body = {"path": str(pdf), "op": "add", "page": 0, "rect": RECT, **patch}
    status, d = call(server, "POST", "/api/os/media/highlights", body)
    assert status == code and d["error"], (patch, status, d)


def test_highlights_only_belong_to_pdfs_inside_the_allowed_folders(server, tmp_path):
    audio = make_audio(server, "a.mp3")
    status, d = call(server, "POST", "/api/os/media/highlights", {"path": str(audio), "page": 0, "rect": RECT})
    assert status == 415
    outside = make_pdf(tmp_path / "elsewhere.pdf")
    status, d = call(server, "POST", "/api/os/media/highlights", {"path": str(outside), "page": 0, "rect": RECT})
    assert status == 403 and "outside the folders" in d["error"]


def test_queue_add_order_dedupe_move_remove_clear(server):
    a, b, c = (make_audio(server, n) for n in ("a.mp3", "b.mp3", "c.mp3"))
    assert call(server, "GET", "/api/os/media/queue")[1]["queue"] == []
    for f in (a, b, c, a):  # the repeat must not duplicate
        status, d = call(server, "POST", "/api/os/media/queue", {"op": "add", "path": str(f)})
        assert status == 200
    names = lambda d: [r["name"] for r in d["queue"]]  # noqa: E731
    assert names(d) == ["a.mp3", "b.mp3", "c.mp3"]
    _, d = call(server, "POST", "/api/os/media/queue", {"op": "move", "path": str(c), "to": 0})
    assert names(d) == ["c.mp3", "a.mp3", "b.mp3"]
    _, d = call(server, "POST", "/api/os/media/queue", {"op": "move", "path": str(c), "to": 99})
    assert names(d) == ["a.mp3", "b.mp3", "c.mp3"], "an index past the end lands last"
    _, d = call(server, "POST", "/api/os/media/queue", {"op": "remove", "path": str(b)})
    assert names(d) == ["a.mp3", "c.mp3"]
    assert names(call(server, "GET", "/api/os/media/queue")[1]) == ["a.mp3", "c.mp3"], "saved, not just returned"
    assert call(server, "POST", "/api/os/media/queue", {"op": "remove", "path": str(b)})[0] == 404
    _, d = call(server, "POST", "/api/os/media/queue", {"op": "clear"})
    assert d["queue"] == []


def test_queue_refuses_non_av_and_drops_files_that_vanished(server):
    img = server.test_home / "Movies" / "p.png"
    img.write_bytes(b"x")
    pdf = make_pdf(server.test_home / "Documents" / "paper.pdf")
    for f in (img, pdf):
        assert call(server, "POST", "/api/os/media/queue", {"op": "add", "path": str(f)})[0] == 415
    assert call(server, "POST", "/api/os/media/queue", {"op": "add", "path": "relative.mp3"})[0] == 400
    assert call(server, "POST", "/api/os/media/queue", {"op": "nope"})[0] == 400
    a, b = make_audio(server, "a.mp3"), make_audio(server, "b.mp3")
    for f in (a, b):
        call(server, "POST", "/api/os/media/queue", {"op": "add", "path": str(f)})
    a.unlink()
    assert [r["name"] for r in call(server, "GET", "/api/os/media/queue")[1]["queue"]] == ["b.mp3"]


def test_player_state_round_trip_and_age(server):
    status, d = call(server, "GET", "/api/os/media/player-state")
    assert status == 200 and d["reported"] is False
    f = make_audio(server, "song.mp3")
    status, _ = call(server, "POST", "/api/os/media/player-state",
                     {"path": str(f), "kind": "audio", "playing": True, "position": 12.3456, "duration": 200})
    assert status == 200
    _, d = call(server, "GET", "/api/os/media/player-state")
    assert d["reported"] and d["name"] == "song.mp3" and d["playing"] is True
    assert d["position"] == 12.346 and d["duration"] == 200 and d["kind"] == "audio" and d["age_s"] >= 0
    # garbage never becomes a number, and "playing" with no file is false
    call(server, "POST", "/api/os/media/player-state", {"path": "", "playing": True, "position": "NaN", "duration": -4, "kind": "other"})
    _, d = call(server, "GET", "/api/os/media/player-state")
    assert d["playing"] is False and d["position"] is None and d["duration"] is None and d["kind"] is None and d["path"] is None


def read_sse_until(srv, want_type, deadline_s=5.0):
    """Open /api/events, return a function that yields the first event of a type."""
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=deadline_s)
    conn.request("GET", "/api/events")
    resp = conn.getresponse()
    assert resp.status == 200
    return conn, resp


def test_player_control_event_has_the_shape_the_screen_acts_on(server):
    """The J endpoint broadcasts; this proves the event on the wire is what the
    screen's controlPlan() consumes (path + action + seconds)."""
    f = make_audio(server, "song.mp3")
    conn, resp = read_sse_until(server, "player_control")
    try:
        # prime the server's idea of "last opened" the way the browser-pane route does
        status, _ = call(server, "POST", "/api/browser-pane/open", {"url": "/file_preview.html?path=" + str(f)})
        assert status == 200
        status, d = call(server, "POST", "/api/player/control", {"action": "seek", "seconds": 42})
        assert status == 200 and d["path"] == str(f)
        got = None
        end = time.time() + 5
        buf = b""
        while time.time() < end and got is None:
            chunk = resp.fp.readline()
            buf += chunk
            if chunk.startswith(b"data:"):
                evt = json.loads(chunk[5:].decode())
                if evt.get("type") == "player_control":
                    got = evt
        assert got == {"type": "player_control", "action": "seek", "seconds": 42.0, "path": str(f)}
    finally:
        conn.close()
