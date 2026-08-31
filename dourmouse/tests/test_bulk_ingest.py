"""dourmouse/bulk_ingest.py — bulk indexing into the shared RAG database
from the local filesystem and Google Drive. Real behavior, verified
against a real temp filesystem tree and a real (in-memory) MemoryStore —
not mocked at the store level, only at the network boundary for Drive.
"""

from __future__ import annotations

import json

import pytest

from dourmouse.bulk_ingest import (
    _extract_local_text,
    _read_drive_file_text,
    ingest_drive,
    ingest_local_tree,
    iter_local_files,
    list_all_drive_files,
)
from dourmouse.memory_store import MemoryStore, MemoryStoreUnavailable


@pytest.fixture
def store(tmp_path):
    try:
        s = MemoryStore(tmp_path / "test_memory.db")
    except MemoryStoreUnavailable:
        pytest.skip("SQLite FTS5 not available in this environment")
    yield s
    s.close()


class TestIterLocalFiles:
    def test_walks_nested_directories(self, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "b").mkdir()
        (tmp_path / "a" / "one.txt").write_text("x")
        (tmp_path / "a" / "b" / "two.txt").write_text("y")
        found = {p.name for p in iter_local_files(tmp_path)}
        assert found == {"one.txt", "two.txt"}

    def test_never_follows_symlinks(self, tmp_path):
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        (real_dir / "f.txt").write_text("x")
        link = tmp_path / "link"
        link.symlink_to(real_dir)
        found = list(iter_local_files(tmp_path))
        # the real file is found once (via real_dir), never via the symlink
        assert sum(1 for p in found if p.name == "f.txt") == 1

    def test_missing_root_yields_nothing(self, tmp_path):
        assert list(iter_local_files(tmp_path / "nope")) == []

    def test_unreadable_directory_does_not_abort_the_walk(self, tmp_path):
        blocked = tmp_path / "blocked"
        blocked.mkdir()
        (tmp_path / "visible.txt").write_text("x")
        blocked.chmod(0o000)
        try:
            found = {p.name for p in iter_local_files(tmp_path)}
        finally:
            blocked.chmod(0o755)
        assert "visible.txt" in found


class TestExtractLocalText:
    def test_plain_text_file(self, tmp_path):
        p = tmp_path / "note.md"
        p.write_text("real content here")
        assert _extract_local_text(p) == "real content here"

    def test_extensionless_file_treated_as_text(self, tmp_path):
        p = tmp_path / "README"
        p.write_text("no extension but real text")
        assert _extract_local_text(p) == "no extension but real text"

    def test_no_content_type_returns_none_not_garbage(self, tmp_path):
        p = tmp_path / "photo.jpg"
        p.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 40)
        assert _extract_local_text(p) is None

    def test_binary_executable_returns_none(self, tmp_path):
        p = tmp_path / "a.out"
        p.write_bytes(b"\x7fELF" + b"\x00" * 40)
        assert _extract_local_text(p) is None


class TestIngestLocalTree:
    def test_indexes_real_text_files_into_the_real_store(self, tmp_path, store):
        root = tmp_path / "docs"
        root.mkdir()
        (root / "one.txt").write_text("the quick brown fox")
        (root / "two.md").write_text("jumps over the lazy dog")
        (root / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
        checkpoint = tmp_path / "ckpt.json"

        stats = ingest_local_tree(store, root, checkpoint)
        assert stats["indexed"] == 2
        assert stats["skipped_no_text"] == 1
        assert stats["errors"] == 0

        hits = store.search("fox", limit=5)
        assert any("one.txt" in h["title"] for h in hits)

    def test_second_run_with_same_checkpoint_skips_everything_already_done(self, tmp_path, store):
        root = tmp_path / "docs2"
        root.mkdir()
        (root / "one.txt").write_text("content")
        checkpoint = tmp_path / "ckpt2.json"

        first = ingest_local_tree(store, root, checkpoint)
        assert first["indexed"] == 1

        second = ingest_local_tree(store, root, checkpoint)
        assert second["indexed"] == 0
        assert second["skipped_done"] == 1

    def test_checkpoint_file_is_real_resumable_json(self, tmp_path, store):
        root = tmp_path / "docs3"
        root.mkdir()
        (root / "a.txt").write_text("aaa")
        checkpoint = tmp_path / "ckpt3.json"
        ingest_local_tree(store, root, checkpoint)
        saved = json.loads(checkpoint.read_text())
        assert any("a.txt" in p for p in saved)

    def test_status_file_written_and_marked_done(self, tmp_path, store):
        root = tmp_path / "docs4"
        root.mkdir()
        (root / "a.txt").write_text("aaa")
        checkpoint = tmp_path / "ckpt4.json"
        ingest_local_tree(store, root, checkpoint, status_every=1)
        status_path = checkpoint.with_name(checkpoint.stem + ".status.json")
        assert status_path.is_file()
        status = json.loads(status_path.read_text())
        assert status["done"] is True
        assert status["indexed"] == 1

    def test_one_bad_file_does_not_abort_the_whole_walk(self, tmp_path, store, monkeypatch):
        root = tmp_path / "docs5"
        root.mkdir()
        (root / "good.txt").write_text("fine")
        (root / "bad.txt").write_text("also fine on disk")
        checkpoint = tmp_path / "ckpt5.json"

        real_extract = _extract_local_text
        def flaky_extract(path):
            if path.name == "bad.txt":
                raise OSError("simulated real I/O failure")
            return real_extract(path)
        monkeypatch.setattr("dourmouse.bulk_ingest._extract_local_text", flaky_extract)

        stats = ingest_local_tree(store, root, checkpoint)
        assert stats["indexed"] == 1
        assert stats["errors"] == 1
        hits = store.search("fine", limit=5)
        assert any("good.txt" in h["title"] for h in hits)

    def test_oversized_file_is_truncated_not_dropped_or_silently_full(self, tmp_path, store):
        import dourmouse.bulk_ingest as bi

        root = tmp_path / "docs6"
        root.mkdir()
        big = "x" * (bi._MAX_TEXT_CHARS + 5000)
        (root / "huge.txt").write_text(big)
        checkpoint = tmp_path / "ckpt6.json"
        stats = ingest_local_tree(store, root, checkpoint)
        assert stats["indexed"] == 1
        hits = store.search("huge", limit=5)
        found = [h for h in hits if "huge.txt" in h["title"]]
        # snippet check aside, confirm the real stored row is capped+labeled
        row = store._conn.execute(
            "SELECT body FROM facts WHERE title LIKE ?", (f"%huge.txt%",)
        ).fetchone()
        assert "TRUNCATED" in row["body"]
        assert len(row["body"]) < len(big)


class _FakeDriveResp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def decode(self, *a, **k):
        return self._payload.decode(*a, **k)


class TestIngestDrive:
    def test_lists_files_across_real_pagination(self, monkeypatch):
        calls = []

        def fake_http_json(method, url, token):
            calls.append(url)
            if "pageToken" not in url:
                return {"nextPageToken": "p2", "files": [{"id": "1", "name": "a", "mimeType": "text/plain"}]}
            return {"files": [{"id": "2", "name": "b", "mimeType": "text/plain"}]}

        monkeypatch.setattr("dourmouse.google_services._http_json", fake_http_json)
        files = list(list_all_drive_files("tok"))
        assert [f["id"] for f in files] == ["1", "2"]
        assert len(calls) == 2

    def test_reads_plain_file_content(self, monkeypatch):
        monkeypatch.setattr(
            "dourmouse.google_services._http_raw",
            lambda *a, **k: b"real drive file content",
        )
        text = _read_drive_file_text("tok", {"id": "x", "name": "notes.txt", "mimeType": "text/plain"})
        assert text == "real drive file content"

    def test_google_doc_uses_export_endpoint(self, monkeypatch):
        seen_urls = []

        def fake_raw(method, url, token, max_bytes=None):
            seen_urls.append(url)
            return b"exported doc text"

        monkeypatch.setattr("dourmouse.google_services._http_raw", fake_raw)
        text = _read_drive_file_text(
            "tok", {"id": "x", "name": "Doc", "mimeType": "application/vnd.google-apps.document"}
        )
        assert text == "exported doc text"
        assert "/export" in seen_urls[0]

    def test_non_exportable_google_type_skipped(self):
        # e.g. a Google Form, Site, or a folder -- no text form exists
        text = _read_drive_file_text("tok", {"id": "x", "name": "Form", "mimeType": "application/vnd.google-apps.form"})
        assert text is None

    def test_oversized_binary_skipped_by_size_metadata(self, monkeypatch):
        import dourmouse.bulk_ingest as bi

        called = {"raw": False}
        def fake_raw(*a, **k):
            called["raw"] = True
            return b""
        monkeypatch.setattr("dourmouse.google_services._http_raw", fake_raw)
        text = _read_drive_file_text(
            "tok", {"id": "x", "name": "huge.bin", "mimeType": "application/octet-stream",
                    "size": str(bi._MAX_DRIVE_BYTES + 1)},
        )
        assert text is None
        assert called["raw"] is False  # never even attempted the read

    def test_ingest_drive_end_to_end_into_real_store(self, tmp_path, store, monkeypatch):
        def fake_list(token, page_size=1000):
            yield {"id": "f1", "name": "one.txt", "mimeType": "text/plain"}
            yield {"id": "f2", "name": "two.txt", "mimeType": "text/plain"}

        monkeypatch.setattr("dourmouse.bulk_ingest.list_all_drive_files", fake_list)
        monkeypatch.setattr(
            "dourmouse.bulk_ingest._read_drive_file_text",
            lambda token, meta: f"content of {meta['name']}",
        )
        checkpoint = tmp_path / "drive_ckpt.json"
        stats = ingest_drive(store, "tok", checkpoint)
        assert stats["indexed"] == 2
        hits = store.search("one.txt", limit=5)
        assert any("one.txt" in h["title"] for h in hits)

    def test_drive_checkpoint_resumes_by_file_id(self, tmp_path, store, monkeypatch):
        def fake_list(token, page_size=1000):
            yield {"id": "f1", "name": "one.txt", "mimeType": "text/plain"}

        monkeypatch.setattr("dourmouse.bulk_ingest.list_all_drive_files", fake_list)
        monkeypatch.setattr("dourmouse.bulk_ingest._read_drive_file_text", lambda t, m: "text")
        checkpoint = tmp_path / "drive_ckpt2.json"
        first = ingest_drive(store, "tok", checkpoint)
        assert first["indexed"] == 1
        second = ingest_drive(store, "tok", checkpoint)
        assert second["indexed"] == 0
        assert second["skipped_done"] == 1

    def test_one_bad_drive_file_does_not_abort_the_run(self, tmp_path, store, monkeypatch):
        def fake_list(token, page_size=1000):
            yield {"id": "f1", "name": "bad.txt", "mimeType": "text/plain"}
            yield {"id": "f2", "name": "good.txt", "mimeType": "text/plain"}

        def flaky_read(token, meta):
            if meta["id"] == "f1":
                raise RuntimeError("simulated real API failure")
            return "good content"

        monkeypatch.setattr("dourmouse.bulk_ingest.list_all_drive_files", fake_list)
        monkeypatch.setattr("dourmouse.bulk_ingest._read_drive_file_text", flaky_read)
        checkpoint = tmp_path / "drive_ckpt3.json"
        stats = ingest_drive(store, "tok", checkpoint)
        assert stats["indexed"] == 1
        assert stats["errors"] == 1
