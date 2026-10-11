"""FD2 fixes for patch_apply: P4-43, P4-44, P4-45."""

from __future__ import annotations

from dourmouse import patch_apply as pa


# --- P4-43: pure-addition hunk goes AFTER line N --------------------------- #
def test_pure_addition_hunk_inserts_after_stated_line(tmp_path):
    f = tmp_path / "t.txt"
    f.write_text("a\nb\nc\n")
    diff = "@@ -2,0 +3 @@\n+X\n"
    res = pa.apply_unified_diff(f, diff)
    assert res.ok, res.message
    assert f.read_text() == "a\nb\nX\nc\n"


def test_pure_addition_at_top_with_zero_start(tmp_path):
    f = tmp_path / "t.txt"
    f.write_text("a\nb\n")
    res = pa.apply_unified_diff(f, "@@ -0,0 +1 @@\n+TOP\n")
    assert res.ok, res.message
    assert f.read_text() == "TOP\na\nb\n"


def test_pure_addition_after_last_line(tmp_path):
    f = tmp_path / "t.txt"
    f.write_text("a\nb\n")
    res = pa.apply_unified_diff(f, "@@ -2,0 +3 @@\n+END\n")
    assert res.ok, res.message
    assert f.read_text() == "a\nb\nEND\n"


# --- P4-44: exact bytes preserved ------------------------------------------ #
def test_crlf_file_keeps_crlf_unified(tmp_path):
    f = tmp_path / "t.txt"
    f.write_bytes(b"a\r\nb\r\nc\r\n")
    res = pa.apply_unified_diff(f, "@@ -2 +2 @@\n-b\n+B\n")
    assert res.ok, res.message
    assert f.read_bytes() == b"a\r\nB\r\nc\r\n"


def test_crlf_file_keeps_crlf_search_replace(tmp_path):
    f = tmp_path / "t.txt"
    f.write_bytes(b"a\r\nb\r\nc\r\n")
    res = pa.apply_search_replace(f, "<<<<<<< SEARCH\nb\n=======\nB\n>>>>>>> REPLACE")
    assert res.ok, res.message
    assert f.read_bytes() == b"a\r\nB\r\nc\r\n"


def test_invalid_utf8_is_refused_not_replaced(tmp_path):
    f = tmp_path / "t.txt"
    raw = b"caf\xe9\nb\nc\n"
    f.write_bytes(raw)
    res = pa.apply_unified_diff(f, "@@ -2 +2 @@\n-b\n+B\n")
    assert not res.ok
    assert "UTF-8" in res.message
    assert f.read_bytes() == raw
    res = pa.apply_search_replace(f, "<<<<<<< SEARCH\nb\n=======\nB\n>>>>>>> REPLACE")
    assert not res.ok
    assert f.read_bytes() == raw


def test_no_trailing_newline_is_preserved(tmp_path):
    f = tmp_path / "t.txt"
    f.write_bytes(b"a\nb\nc")
    res = pa.apply_unified_diff(f, "@@ -2 +2 @@\n-b\n+B\n")
    assert res.ok, res.message
    assert f.read_bytes() == b"a\nB\nc"


def test_form_feed_is_not_treated_as_a_line_break(tmp_path):
    f = tmp_path / "t.txt"
    f.write_bytes(b"a\x0cb\nc\nd\n")
    res = pa.apply_unified_diff(f, "@@ -3 +3 @@\n-d\n+D\n")
    assert res.ok, res.message
    assert f.read_bytes() == b"a\x0cb\nc\nD\n"


# --- P4-45: ambiguity and overlap ------------------------------------------ #
def test_ambiguous_context_with_wrong_line_number_is_refused(tmp_path):
    f = tmp_path / "t.txt"
    original = "x\nreturn None\ny\nreturn None\nz\n"
    f.write_text(original)
    res = pa.apply_unified_diff(f, "@@ -9 +9 @@\n-return None\n+return 1\n")
    assert not res.ok
    assert "matches" in res.message or "ambiguous" in res.message
    assert f.read_text() == original


def test_unique_context_with_wrong_line_number_still_applies(tmp_path):
    f = tmp_path / "t.txt"
    f.write_text("x\nreturn None\ny\nz\n")
    res = pa.apply_unified_diff(f, "@@ -9 +9 @@\n-return None\n+return 1\n")
    assert res.ok, res.message
    assert f.read_text() == "x\nreturn 1\ny\nz\n"


def test_overlapping_hunks_are_refused(tmp_path):
    f = tmp_path / "t.txt"
    original = "a\nb\nc\nd\n"
    f.write_text(original)
    diff = "@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n@@ -2,3 +2,3 @@\n b\n-c\n+C\n d\n"
    res = pa.apply_unified_diff(f, diff)
    assert not res.ok
    assert "overlap" in res.message
    assert f.read_text() == original
