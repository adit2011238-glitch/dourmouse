"""Fix agent FD1: P4-6, P4-5, P4-7 (bulk_ingest) and the unreadable-checkpoint overwrite found next to them."""

from __future__ import annotations

import json

import pytest

import dourmouse.bulk_ingest as bi
from dourmouse.memory_store import MemoryStore


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "m.db")
    yield s
    s.close()


# P4-6 ----------------------------------------------------------------------- #

def _drive(monkeypatch, files, reader):
    monkeypatch.setattr(bi, "list_all_drive_files", lambda token, page_size=1000: iter(files))
    monkeypatch.setattr("dourmouse.google_services._http_raw", reader)


def test_drive_auth_failure_is_an_error_and_not_checkpointed(tmp_path, store, monkeypatch):
    def reader(method, url, token, **kw):
        raise RuntimeError("GOOGLE API 401 on https://x: b'expired'")

    _drive(monkeypatch, [{"id": "f1", "name": "a.txt", "mimeType": "text/plain"}], reader)
    ckpt = tmp_path / "c.json"
    stats = bi.ingest_drive(store, "tok", ckpt)
    assert stats["errors"] == 1
    assert stats["skipped_no_text"] == 0
    assert "f1" not in json.loads(ckpt.read_text())


def test_drive_failed_file_is_retried_on_the_next_run(tmp_path, store, monkeypatch):
    files = [{"id": "f1", "name": "a.txt", "mimeType": "text/plain"}]

    def bad(method, url, token, **kw):
        raise RuntimeError("GOOGLE API 503")

    ckpt = tmp_path / "c.json"
    _drive(monkeypatch, files, bad)
    bi.ingest_drive(store, "tok", ckpt)
    _drive(monkeypatch, files, lambda method, url, token, **kw: b"real content here")
    second = bi.ingest_drive(store, "tok", ckpt)
    assert second["indexed"] == 1 and second["errors"] == 0


def test_drive_genuine_non_text_is_still_a_counted_skip(tmp_path, store, monkeypatch):
    _drive(monkeypatch, [{"id": "f1", "name": "x", "mimeType": "application/vnd.google-apps.form"}],
           lambda *a, **k: b"")
    ckpt = tmp_path / "c.json"
    stats = bi.ingest_drive(store, "tok", ckpt)
    assert stats["skipped_no_text"] == 1 and stats["errors"] == 0
    assert "f1" in json.loads(ckpt.read_text())


def test_drive_over_cap_response_is_a_skip_not_an_error(tmp_path, store, monkeypatch):
    def reader(method, url, token, **kw):
        # FIX-W2 W2R-7: classified by the typed flag, no longer by matching the message text
        from dourmouse.google_services import GoogleApiError

        raise GoogleApiError("GOOGLE API: response exceeds the 8,000,000 B cap on x - nothing was read.", over_cap=True)

    _drive(monkeypatch, [{"id": "f1", "name": "big.txt", "mimeType": "text/plain"}], reader)
    stats = bi.ingest_drive(store, "tok", tmp_path / "c.json")
    assert stats["skipped_no_text"] == 1 and stats["errors"] == 0


# P4-7 ----------------------------------------------------------------------- #

def test_local_read_error_is_not_checkpointed(tmp_path, store, monkeypatch):
    root = tmp_path / "r"
    root.mkdir()
    (root / "a.txt").write_text("hello world")
    monkeypatch.setattr(bi, "_extract_local_text", lambda p: (_ for _ in ()).throw(OSError("busy")))
    ckpt = tmp_path / "c.json"
    stats = bi.ingest_local_tree(store, root, ckpt)
    assert stats["errors"] == 1
    assert json.loads(ckpt.read_text()) == []


def test_local_store_error_is_retried_next_run(tmp_path, store, monkeypatch):
    root = tmp_path / "r"
    root.mkdir()
    (root / "a.txt").write_text("hello world")
    ckpt = tmp_path / "c.json"

    class Locked:
        def remember(self, *a, **k):
            raise RuntimeError("database is locked")

    first = bi.ingest_local_tree(Locked(), root, ckpt)
    assert first["errors"] == 1
    second = bi.ingest_local_tree(store, root, ckpt)
    assert second["indexed"] == 1


def test_big_file_is_read_as_a_bounded_prefix(tmp_path, monkeypatch):
    big = tmp_path / "huge"  # no extension
    big.write_bytes(b"a" * (bi._MAX_TEXT_CHARS * 4 + 1000))
    reads = []
    real_open = type(big).open

    class Spy:
        def __init__(self, fh):
            self.fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.fh.close()

        def read(self, n=-1):
            reads.append(n)
            return self.fh.read(n)

    monkeypatch.setattr(type(big), "open", lambda self, *a, **k: Spy(real_open(self, *a, **k)))
    text = bi._extract_local_text(big)
    assert reads and all(n > 0 for n in reads), "read() with no bound loads the whole file"
    assert len(text) > bi._MAX_TEXT_CHARS
    assert len(text) < bi._MAX_TEXT_CHARS * 4 + 1000


# P4-5 ----------------------------------------------------------------------- #

def test_checkpoint_is_saved_even_when_the_boundary_file_is_skipped(tmp_path, store, monkeypatch):
    root = tmp_path / "r"
    root.mkdir()
    for i in range(6):
        (root / f"f{i}.png").write_bytes(b"\x89PNG")  # no-text skips only
    saves = []
    real_save = bi._save_checkpoint
    monkeypatch.setattr(bi, "_save_checkpoint", lambda p, d: (saves.append(len(d)), real_save(p, d)))
    bi.ingest_local_tree(store, root, tmp_path / "c.json", status_every=2)
    # periodic saves happened during the walk, plus the final one
    assert len(saves) >= 3


# checkpoint overwrite -------------------------------------------------------- #

def test_unreadable_checkpoint_is_kept_aside_not_overwritten(tmp_path, store):
    root = tmp_path / "r"
    root.mkdir()
    (root / "a.txt").write_text("alpha text")
    ckpt = tmp_path / "c.json"
    ckpt.write_text('["/old/done/path", ')  # truncated by a crash
    logs = []
    bi.ingest_local_tree(store, root, ckpt, log=logs.append)
    aside = list(tmp_path.glob("c.json.corrupt-*"))
    assert len(aside) == 1 and aside[0].read_text().startswith('["/old/done/path"')
    assert any("checkpoint" in m for m in logs)
    assert json.loads(ckpt.read_text())  # a fresh, valid checkpoint now
