"""dourmouse/usage_tracker.py — real, persisted Claude + Ollama usage
accounting. See that module's own docstring for what's real (the
Claude Code CLI's own real result-event fields, live-verified this
session before this module was written) vs. honestly not attempted
(a fabricated Ollama dollar cost).
"""

from __future__ import annotations

import json
import threading

import pytest

from dourmouse import usage_tracker


@pytest.fixture(autouse=True)
def _isolated_usage_path(tmp_path, monkeypatch):
    """Real file I/O against a real tmp_path file — never the developer's
    actual ~/Library/Application Support/Dourmouse/usage.json."""
    path = tmp_path / "usage.json"
    monkeypatch.setattr(usage_tracker, "_usage_path", lambda: path)
    return path


class TestGetTotals:
    def test_missing_file_is_honest_all_zero(self):
        totals = usage_tracker.get_totals()
        assert totals["claude"]["requests"] == 0
        assert totals["claude"]["cost_usd"] == 0.0
        assert totals["ollama"]["requests"] == 0

    def test_corrupt_file_is_honest_not_a_crash(self, _isolated_usage_path):
        _isolated_usage_path.write_text("not valid json{{{", encoding="utf-8")
        totals = usage_tracker.get_totals()
        assert totals["claude"]["requests"] == 0

    def test_partial_file_fills_honest_defaults_for_missing_fields(self, _isolated_usage_path):
        _isolated_usage_path.write_text(
            json.dumps({"claude": {"requests": 3}}), encoding="utf-8"
        )
        totals = usage_tracker.get_totals()
        assert totals["claude"]["requests"] == 3
        assert totals["claude"]["cost_usd"] == 0.0  # honest default, not fabricated
        assert totals["ollama"]["requests"] == 0


class TestRecordClaudeUsage:
    def test_real_usage_accumulates(self):
        usage_tracker.record_claude_usage({
            "cost_usd": 0.0446764, "input_tokens": 2, "output_tokens": 4,
            "cache_creation_input_tokens": 10010, "cache_read_input_tokens": 22962,
        })
        usage_tracker.record_claude_usage({
            "cost_usd": 0.01, "input_tokens": 10, "output_tokens": 20,
        })
        totals = usage_tracker.get_totals()
        c = totals["claude"]
        assert c["requests"] == 2
        assert c["cost_usd"] == pytest.approx(0.0546764, abs=1e-6)
        assert c["input_tokens"] == 12
        assert c["output_tokens"] == 24
        assert c["cache_creation_input_tokens"] == 10010  # second call didn't report this field
        assert c["cache_read_input_tokens"] == 22962

    def test_missing_fields_in_one_call_do_not_zero_out_the_running_total(self):
        usage_tracker.record_claude_usage({"cost_usd": 1.0, "input_tokens": 100})
        usage_tracker.record_claude_usage({})  # a real call that reported nothing usable
        totals = usage_tracker.get_totals()
        assert totals["claude"]["requests"] == 2
        assert totals["claude"]["cost_usd"] == 1.0
        assert totals["claude"]["input_tokens"] == 100

    def test_a_broken_persistence_path_never_raises(self, monkeypatch):
        """The real safety property: usage tracking must never break the
        chat turn it's observing."""
        monkeypatch.setattr(usage_tracker, "_usage_path", lambda: object())  # not a real Path
        usage_tracker.record_claude_usage({"cost_usd": 1.0})  # must not raise

    def test_persists_across_a_fresh_read(self, _isolated_usage_path):
        usage_tracker.record_claude_usage({"cost_usd": 0.5, "input_tokens": 50})
        raw = json.loads(_isolated_usage_path.read_text(encoding="utf-8"))
        assert raw["claude"]["cost_usd"] == 0.5
        assert raw["claude"]["input_tokens"] == 50


class TestRecordOllamaUsage:
    def test_real_usage_accumulates(self):
        usage_tracker.record_ollama_usage({"prompt_tokens": 30, "completion_tokens": 12})
        usage_tracker.record_ollama_usage({"prompt_tokens": 8, "completion_tokens": 4})
        totals = usage_tracker.get_totals()
        o = totals["ollama"]
        assert o["requests"] == 2
        assert o["prompt_tokens"] == 38
        assert o["completion_tokens"] == 16

    def test_never_carries_a_fabricated_cost_field(self):
        usage_tracker.record_ollama_usage({"prompt_tokens": 10, "completion_tokens": 5})
        totals = usage_tracker.get_totals()
        assert "cost_usd" not in totals["ollama"]

    def test_a_broken_persistence_path_never_raises(self, monkeypatch):
        monkeypatch.setattr(usage_tracker, "_usage_path", lambda: object())
        usage_tracker.record_ollama_usage({"prompt_tokens": 1})  # must not raise


class TestConcurrentWrites:
    def test_concurrent_record_calls_never_lose_a_count(self):
        """Real thread-safety property: 20 threads each recording one
        real Claude call must sum to exactly 20 requests, never fewer
        (a lost update from an unsynchronized read-modify-write)."""
        threads = [
            threading.Thread(target=usage_tracker.record_claude_usage, args=({"cost_usd": 0.01, "input_tokens": 1},))
            for _ in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        totals = usage_tracker.get_totals()
        assert totals["claude"]["requests"] == 20
        assert totals["claude"]["input_tokens"] == 20
