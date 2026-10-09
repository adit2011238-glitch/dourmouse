"""FS1 P2-1: the spaced-secret joiner must be "one or more white space or
invisible characters", not "one white space then literal ']'"."""

from __future__ import annotations

from dourmouse.governance import _spaced_pattern


def test_multi_word_secret_split_by_newline_or_zero_width_is_caught():
    pat = _spaced_pattern("correct horse battery")
    assert pat.search("xx correct\nhorse   battery yy")
    assert pat.search("correct​horse​​battery")
    assert pat.search("correct horse battery")


def test_nonsense_brackets_are_not_what_matches():
    pat = _spaced_pattern("correct horse battery")
    assert not pat.search("correct ]horse ]battery")
    assert not pat.search("correcthorsebattery")
