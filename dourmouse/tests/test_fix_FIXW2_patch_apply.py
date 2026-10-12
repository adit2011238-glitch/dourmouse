"""FIX-W2 W2R-8: patch_apply keeps CRLF files CRLF even when the patch text itself has CRLF, and does
not refuse an insertion hunk that sits directly before a hunk replacing the next line."""
from __future__ import annotations

from dourmouse.patch_apply import apply_search_replace, apply_unified_diff, parse_search_replace_blocks


def _blocks(search: str, replace: str) -> str:
    return f"<<<<<<< SEARCH\n{search}\n=======\n{replace}\n>>>>>>> REPLACE\n"


def test_crlf_in_the_replacement_does_not_double_the_carriage_return(tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"a\r\nb\r\nc\r\n")
    res = apply_search_replace(f, _blocks("b", "B\r\nB2"))
    assert res.ok, res.message
    assert f.read_bytes() == b"a\r\nB\r\nB2\r\nc\r\n"


def test_crlf_patch_text_finds_a_crlf_file_search(tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"a\r\nb\r\nc\r\n")
    res = apply_search_replace(f, "<<<<<<< SEARCH\r\na\r\nb\r\n=======\r\nX\r\nY\r\n>>>>>>> REPLACE\r\n")
    assert res.ok, res.message
    assert f.read_bytes() == b"X\r\nY\r\nc\r\n"


def test_crlf_replacement_into_an_lf_file_stays_lf(tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"a\nb\nc\n")
    assert apply_search_replace(f, _blocks("b", "B\r\nB2")).ok
    assert f.read_bytes() == b"a\nB\nB2\nc\n"


def test_parse_normalises_line_endings():
    (blk,) = parse_search_replace_blocks("<<<<<<< SEARCH\r\nx\r\n=======\r\ny\r\nz\r\n>>>>>>> REPLACE")
    assert blk.search == "x" and blk.replace == "y\nz"


def test_crlf_unified_diff_applies_to_a_crlf_file(tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"a\r\nb\r\nc\r\n")
    diff = "--- a\r\n+++ a\r\n@@ -2,1 +2,2 @@\r\n-b\r\n+B\r\n+B2\r\n"
    assert apply_unified_diff(f, diff).ok
    assert f.read_bytes() == b"a\r\nB\r\nB2\r\nc\r\n"


def test_insertion_hunk_followed_by_a_hunk_replacing_the_next_line_is_not_an_overlap(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("one\ntwo\nthree\n", encoding="utf-8")
    diff = "@@ -1,0 +2,1 @@\n+inserted\n@@ -2,1 +3,1 @@\n-two\n+TWO\n"
    res = apply_unified_diff(f, diff)
    assert res.ok, res.message
    assert f.read_text(encoding="utf-8") == "one\ninserted\nTWO\nthree\n"


def test_the_same_pair_in_the_other_order_gives_the_same_result(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("one\ntwo\nthree\n", encoding="utf-8")
    diff = "@@ -2,1 +3,1 @@\n-two\n+TWO\n@@ -1,0 +2,1 @@\n+inserted\n"
    res = apply_unified_diff(f, diff)
    assert res.ok, res.message
    assert f.read_text(encoding="utf-8") == "one\ninserted\nTWO\nthree\n"


def test_two_insertions_at_the_same_spot_are_still_refused(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("one\ntwo\n", encoding="utf-8")
    res = apply_unified_diff(f, "@@ -1,0 +2,1 @@\n+x\n@@ -1,0 +2,1 @@\n+y\n")
    assert not res.ok and "overlap" in res.message
    assert f.read_text(encoding="utf-8") == "one\ntwo\n"


def test_genuinely_overlapping_hunks_are_still_refused(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("one\ntwo\nthree\n", encoding="utf-8")
    res = apply_unified_diff(f, "@@ -1,2 +1,2 @@\n-one\n-two\n+1\n+2\n@@ -2,2 +2,2 @@\n-two\n-three\n+2\n+3\n")
    assert not res.ok and "overlap" in res.message
