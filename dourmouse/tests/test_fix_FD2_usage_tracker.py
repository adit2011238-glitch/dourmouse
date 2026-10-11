"""FD2 fix P4-65: usage.json is written atomically, a corrupt one is kept aside, and a null field
no longer drops the whole record."""

from __future__ import annotations

import json
import os
import threading

import pytest

from dourmouse import usage_tracker as ut


@pytest.fixture(autouse=True)
def usage_path(tmp_path, monkeypatch):
    path = tmp_path / "usage.json"
    monkeypatch.setattr(ut, "_usage_path", lambda: path)
    return path


def test_null_fields_do_not_drop_the_record():
    ut.record_claude_usage({
        "input_tokens": 100, "output_tokens": None, "cache_read_input_tokens": None,
        "cache_creation_input_tokens": None, "cost_usd": None,
    })
    c = ut.get_totals()["claude"]
    assert c["requests"] == 1 and c["input_tokens"] == 100 and c["cost_usd"] == 0.0


def test_garbage_field_values_count_as_zero_not_a_dropped_record():
    ut.record_claude_usage({"input_tokens": "abc", "output_tokens": 7, "cost_usd": "n/a"})
    c = ut.get_totals()["claude"]
    assert c["requests"] == 1 and c["output_tokens"] == 7 and c["input_tokens"] == 0
    ut.record_ollama_usage({"prompt_tokens": None, "completion_tokens": 3})
    o = ut.get_totals()["ollama"]
    assert o["requests"] == 1 and o["completion_tokens"] == 3


def test_corrupt_file_is_kept_aside_not_zeroed_by_the_next_record(usage_path):
    usage_path.write_text('{"claude": {"requests": 41, "cost_usd": 9.5', encoding="utf-8")  # truncated
    ut.record_claude_usage({"input_tokens": 1})
    asides = list(usage_path.parent.glob("usage.json.corrupt-*"))
    assert len(asides) == 1
    assert asides[0].read_text(encoding="utf-8").startswith('{"claude": {"requests": 41')
    assert ut.get_totals()["claude"]["requests"] == 1


def test_reading_totals_never_moves_a_corrupt_file(usage_path):
    usage_path.write_text("garbage{", encoding="utf-8")
    assert ut.get_totals()["claude"]["requests"] == 0
    assert usage_path.read_text(encoding="utf-8") == "garbage{"
    assert not list(usage_path.parent.glob("usage.json.corrupt-*"))


def test_save_is_atomic_a_failed_replace_keeps_the_old_totals(usage_path, monkeypatch):
    ut.record_claude_usage({"input_tokens": 5})
    before = usage_path.read_text(encoding="utf-8")

    def boom(src, dst):
        raise OSError("disk full")

    with monkeypatch.context() as m:
        m.setattr(os, "replace", boom)
        ut.record_claude_usage({"input_tokens": 99})  # swallowed by contract, but must not truncate
    assert usage_path.read_text(encoding="utf-8") == before
    assert json.loads(before)["claude"]["input_tokens"] == 5


def test_concurrent_records_all_count(usage_path):
    ts = [threading.Thread(target=ut.record_claude_usage, args=({"input_tokens": 1},)) for _ in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    c = ut.get_totals()["claude"]
    assert c["requests"] == 20 and c["input_tokens"] == 20
