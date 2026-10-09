"""FS2 P4-54: the downloads watcher keys on file identity, not on a name seen once."""

from __future__ import annotations

import os

from dourmouse.security import downloads as dl


def _bump(path, ns=2_000_000_000):
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + ns))


def test_a_name_reused_after_deletion_is_assessed_again(tmp_path):
    (tmp_path / "setup.dmg").write_bytes(b"first")
    w = dl.DownloadsWatcher(tmp_path)
    assert w.poll_once() == []  # primed
    (tmp_path / "setup.dmg").unlink()
    assert w.poll_once() == []  # gone: forgotten
    (tmp_path / "setup.dmg").write_bytes(b"evil")
    assert w.poll_once() == []  # first sight
    assert [p.name for p in w.poll_once()] == ["setup.dmg"]


def test_a_replaced_file_with_the_same_name_is_assessed_again(tmp_path):
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF-1")
    w = dl.DownloadsWatcher(tmp_path)
    w.poll_once()
    f.write_bytes(b"%PDF-1 but now a different and longer body")
    assert w.poll_once() == []
    assert [p.name for p in w.poll_once()] == ["report.pdf"]
    assert w.poll_once() == []


def test_a_download_paused_then_finished_is_assessed_twice(tmp_path):
    w = dl.DownloadsWatcher(tmp_path)
    w.poll_once()
    f = tmp_path / "update.zip"
    f.write_bytes(b"half")
    assert w.poll_once() == []
    assert [p.name for p in w.poll_once()] == ["update.zip"]  # stalled: assessed as it is
    f.write_bytes(b"half and then the rest of the file")
    assert w.poll_once() == []
    assert [p.name for p in w.poll_once()] == ["update.zip"]  # finished: assessed again
    assert w.poll_once() == []


def test_an_untouched_file_is_never_reported_again(tmp_path):
    f = tmp_path / "a.pdf"
    f.write_bytes(b"x")
    w = dl.DownloadsWatcher(tmp_path)
    w.poll_once()
    for _ in range(4):
        assert w.poll_once() == []


def test_same_size_rewrite_is_caught_by_mtime(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"1234")
    w = dl.DownloadsWatcher(tmp_path)
    w.poll_once()
    f.write_bytes(b"abcd")
    _bump(f)
    assert w.poll_once() == []
    assert [p.name for p in w.poll_once()] == ["a.bin"]
