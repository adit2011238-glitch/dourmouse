"""FIX-R R-7: a file handed on once is reported again only when it really changed, not when it was touched."""

from __future__ import annotations

import os
import time

from dourmouse.security.downloads import DownloadsWatcher


def _settle(w: DownloadsWatcher) -> list:
    """Poll until two equal polls have happened (what the loop does between its sleeps)."""
    got = []
    for _ in range(4):
        got += w.poll_once()
    return got


def _watcher(tmp_path) -> DownloadsWatcher:
    w = DownloadsWatcher(folder=tmp_path)
    w.poll_once()  # prime on an empty folder
    return w


def test_a_new_file_is_handed_on_once_after_it_settles(tmp_path):
    w = _watcher(tmp_path)
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4 one")
    assert [p.name for p in _settle(w)] == ["a.pdf"]
    assert _settle(w) == []


def test_touching_a_handed_on_file_does_not_assess_it_again(tmp_path):
    w = _watcher(tmp_path)
    f = tmp_path / "keep.pkg"
    f.write_bytes(b"installer bytes")
    assert len(_settle(w)) == 1
    later = time.time() + 3600
    os.utime(f, (later, later))  # a backup or sync tool touching it
    assert _settle(w) == []
    os.utime(f, (later + 60, later + 60))
    assert _settle(w) == []


def test_a_file_that_really_changed_is_assessed_again(tmp_path):
    w = _watcher(tmp_path)
    f = tmp_path / "grow.zip"
    f.write_bytes(b"first")
    assert len(_settle(w)) == 1
    f.write_bytes(b"first and a lot more")
    assert [p.name for p in _settle(w)] == ["grow.zip"]


def test_a_file_replaced_under_the_same_name_with_the_same_size_is_new(tmp_path):
    w = _watcher(tmp_path)
    f = tmp_path / "tool.sh"
    f.write_bytes(b"aaaa")
    assert len(_settle(w)) == 1
    f.unlink()
    f.write_bytes(b"bbbb")  # new inode, same size
    assert [p.name for p in _settle(w)] == ["tool.sh"]


def test_adding_a_file_inside_a_handed_on_folder_does_not_report_the_folder_again(tmp_path):
    w = _watcher(tmp_path)
    d = tmp_path / "cloned-repo"
    d.mkdir()
    (d / "one.txt").write_text("1")
    assert [p.name for p in _settle(w)] == ["cloned-repo"]
    (d / "two.txt").write_text("2")  # the folder's own mtime changes
    assert _settle(w) == []


def test_a_new_folder_still_waits_until_it_stops_changing(tmp_path):
    w = _watcher(tmp_path)
    d = tmp_path / "App.app"
    d.mkdir()
    (d / "a").write_text("1")
    assert w.poll_once() == []  # first sight: pending
    (d / "b").write_text("2")  # still being extracted
    assert w.poll_once() == []  # changed since the last poll, so still pending
    assert [p.name for p in w.poll_once()] == ["App.app"]


def test_a_same_size_rewrite_in_place_of_a_handed_on_file_is_still_caught_by_content(tmp_path):
    w = _watcher(tmp_path)
    f = tmp_path / "a.bin"
    f.write_bytes(b"1234")
    assert len(_settle(w)) == 1
    st = f.stat()
    f.write_bytes(b"abcd")  # same inode, same size, different bytes
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert [p.name for p in _settle(w)] == ["a.bin"]
    assert _settle(w) == []
