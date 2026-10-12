"""FIX-R3 H-FIXW2-5: the MEDIA recent list, queue and PDF highlights never write an empty list over a
saved file they failed to read. A file that is not valid JSON is kept aside as <name>.corrupt-<time>
(state_file) and the write starts fresh; a file that cannot be read right now is left exactly as it is
and the change is refused (the recent list, a convenience, is just not updated)."""
from __future__ import annotations

import os

import pytest

from dourmouse.config import workspace_dir
from dourmouse.os_api import ApiError, Request
from dourmouse.os_api import media


@pytest.fixture
def ws():
    root = workspace_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _req(body):
    return Request(server=None, body=body)


@pytest.fixture
def unreadable():
    made = []

    def make(path, text):
        path.write_text(text, encoding="utf-8")
        os.chmod(path, 0)
        made.append(path)
        return path

    yield make
    for path in made:
        os.chmod(path, 0o600)


def _asides(path):
    return sorted(path.parent.glob(path.name + ".corrupt-*"))


def test_a_corrupt_queue_is_kept_aside_not_overwritten(ws):
    song = ws / "song.wav"
    song.write_bytes(b"RIFF")
    saved = ws / "media_queue.json"
    saved.write_text('{"paths": ["/x/a.mp3", ', encoding="utf-8")
    status, data = media.queue_write(_req({"op": "add", "path": str(song)}))
    assert status == 200 and [r["path"] for r in data["queue"]] == [str(song.resolve())]
    kept = _asides(saved)
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8") == '{"paths": ["/x/a.mp3", '
    assert "notice" in data


def test_an_unreadable_queue_refuses_the_change_and_is_left_alone(ws, unreadable):
    song = ws / "song.wav"
    song.write_bytes(b"RIFF")
    saved = unreadable(ws / "media_queue.json", '{"paths": ["/x/keep.mp3"]}')
    with pytest.raises(ApiError) as exc:
        media.queue_write(_req({"op": "clear"}))
    assert exc.value.status >= 500
    os.chmod(saved, 0o600)
    assert saved.read_text(encoding="utf-8") == '{"paths": ["/x/keep.mp3"]}'
    assert _asides(saved) == []


def test_a_corrupt_highlights_file_is_kept_aside(ws):
    pdf = ws / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    saved = ws / "media_highlights.json"
    saved.write_text("[1, 2", encoding="utf-8")
    status, data = media.highlights_write(_req({"path": str(pdf), "op": "add", "page": 0,
                                                 "rect": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}}))
    assert status == 200 and len(data["highlights"]) == 1
    assert [p.read_text(encoding="utf-8") for p in _asides(saved)] == ["[1, 2"]


def test_an_unreadable_highlights_file_refuses_the_change(ws, unreadable):
    pdf = ws / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    saved = unreadable(ws / "media_highlights.json", '{"k": [{"id": "1"}]}')
    with pytest.raises(ApiError) as exc:
        media.highlights_write(_req({"path": str(pdf), "op": "clear"}))
    assert exc.value.status >= 500
    os.chmod(saved, 0o600)
    assert saved.read_text(encoding="utf-8") == '{"k": [{"id": "1"}]}'


def test_an_unreadable_recent_list_is_not_replaced_when_a_file_opens(ws, unreadable):
    pic = ws / "pic.png"
    pic.write_bytes(b"\x89PNG")
    saved = unreadable(ws / "media_recent.json", '{"paths": ["/x/older.png"]}')
    status, data = media.open_file(_req({"path": str(pic)}))
    assert status == 200 and data["ok"] is True  # opening never depends on the recent list
    os.chmod(saved, 0o600)
    assert saved.read_text(encoding="utf-8") == '{"paths": ["/x/older.png"]}'


def test_a_corrupt_recent_list_is_kept_aside_and_restarted(ws):
    pic = ws / "pic.png"
    pic.write_bytes(b"\x89PNG")
    saved = ws / "media_recent.json"
    saved.write_text('{"paths": "not a list"}', encoding="utf-8")
    media.open_file(_req({"path": str(pic)}))
    assert [p.read_text(encoding="utf-8") for p in _asides(saved)] == ['{"paths": "not a list"}']
    assert media._read_recent() == [str(pic.resolve())]
