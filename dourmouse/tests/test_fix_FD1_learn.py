"""Fix agent FD1: P5-25, one corrupt line in a session ledger does not hide the intact turns."""

from __future__ import annotations

import json

from dourmouse.learn import _last_session_record


def test_a_truncated_final_line_keeps_the_last_intact_turn(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(json.dumps({"turn": 1, "user": "a", "final_text": "b"}) + "\n"
                 + json.dumps({"turn": 2, "user": "c", "final_text": "d"}) + "\n"
                 + '{"turn": 3, "user": "e", "final_te')
    assert _last_session_record(p)["turn"] == 2


def test_a_corrupt_line_in_the_middle_is_skipped(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(json.dumps({"turn": 1, "user": "a", "final_text": "b"}) + "\nnot json\n"
                 + json.dumps({"turn": 3, "user": "e", "final_text": "f"}) + "\n")
    assert _last_session_record(p)["turn"] == 3


def test_non_dict_lines_are_ignored(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(json.dumps({"turn": 1, "user": "a", "final_text": "b"}) + "\n[1, 2]\n\"str\"\n42\n")
    assert _last_session_record(p)["turn"] == 1


def test_an_empty_or_missing_ledger_is_none(tmp_path):
    assert _last_session_record(tmp_path / "nope.jsonl") is None
    (tmp_path / "e.jsonl").write_text("")
    assert _last_session_record(tmp_path / "e.jsonl") is None
