"""Stress-test fixes of 2026-10-27: STRESS1-10 (full disk keeps the answer, torn ledger line is
repaired), STRESS1-12 (token deltas are coalesced in the office log), STRESS1-14 (a research run
checkpoints and survives any error)."""

from __future__ import annotations

import errno
import json
import time

import pytest

from dourmouse.office_logger import OfficeLogger
from dourmouse.research_pipeline.core import ResearchRecord
from dourmouse.research_pipeline.stages import run_full_pipeline


# ----------------------------------------------------------------------------- office log
def _count(path, kind):
    import sqlite3

    c = sqlite3.connect(str(path))
    try:
        return c.execute("SELECT COUNT(*) FROM agent_events WHERE type=?", (kind,)).fetchone()[0]
    finally:
        c.close()


def test_thousands_of_tokens_become_a_handful_of_rows(tmp_path):
    log = OfficeLogger(tmp_path / "o.db")
    for _ in range(3000):
        log.on_event({"type": "thinking_delta", "text": "tok ", "agent": "a", "call_id": "c1"})
    log.on_event({"type": "assistant_text", "text": "done", "agent": "a", "call_id": "c1"})
    rows = log.transcript(call_id="c1", limit=2000)
    thinking = [r for r in rows if r["type"] == "thinking_delta"]
    assert len(thinking) < 40  # was 3000
    assert "".join(r["text"] for r in thinking) == "tok " * 3000  # nothing lost, order kept


def test_a_tool_event_flushes_earlier_tokens_first_so_order_is_kept(tmp_path):
    log = OfficeLogger(tmp_path / "o.db")
    log.on_event({"type": "thinking_delta", "text": "I will search. ", "agent": "a", "call_id": "c1"})
    log.on_event({"type": "tool_use", "name": "web_search", "agent": "a", "call_id": "c1"})
    log.on_event({"type": "assistant_delta", "text": "Found it.", "agent": "a", "call_id": "c1"})
    kinds = [r["type"] for r in log.transcript(call_id="c1")]
    assert kinds == ["thinking_delta", "tool_use", "assistant_delta"]


def test_reading_flushes_tokens_still_in_the_buffer(tmp_path):
    log = OfficeLogger(tmp_path / "o.db")
    log.on_event({"type": "assistant_delta", "text": "hello", "agent": "a", "call_id": "c9"})
    assert [r["text"] for r in log.transcript(agent="a")] == ["hello"]


def test_a_buffer_older_than_the_flush_time_is_written(tmp_path, monkeypatch):
    log = OfficeLogger(tmp_path / "o.db")
    log.on_event({"type": "assistant_delta", "text": "a", "agent": "a", "call_id": "c2"})
    time.sleep(0.6)
    log.on_event({"type": "assistant_delta", "text": "b", "agent": "a", "call_id": "c2"})
    assert _count(tmp_path / "o.db", "assistant_delta") == 1  # written without anyone reading


# ----------------------------------------------------------------------------- chat persist
def test_a_full_disk_returns_the_answer_with_a_warning(tmp_path, monkeypatch):
    from dourmouse import chat as chat_module
    from dourmouse.chat import ChatSession
    from dourmouse.dispatch import DispatchRegistry

    session = ChatSession(DispatchRegistry(), session_file=tmp_path / "s.jsonl")

    def _boom(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(session, "_persist", _boom)
    monkeypatch.setattr(chat_module, "run_dispatch_messages",
                        lambda *a, **k: {"final_text": "the answer", "transcript": []}, raising=False)
    report = session.ask("hello", force_plain_dispatch=True)
    assert report.get("final_text") == "the answer"
    assert "not saved" in report.get("persist_warning", "")


def test_a_torn_last_ledger_line_does_not_swallow_the_next_record(tmp_path):
    from dourmouse.chat import ChatSession
    from dourmouse.dispatch import DispatchRegistry

    path = tmp_path / "s.jsonl"
    path.write_text('{"half": "a record cut off mid-wri', encoding="utf-8")  # no newline at the end
    session = ChatSession(DispatchRegistry(), session_file=path)
    session._persist("hi", {"final_text": "x", "transcript": []}, 1.0, display_text="x", screen="HOME")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    json.loads(lines[1])  # the new record is on its own, parseable line


# ----------------------------------------------------------------------------- research checkpoint
def _record_with_plan(n=3):
    r = ResearchRecord(question="Q")
    r.set_plan([f"sub {i}" for i in range(n)])
    return r


def test_a_crash_on_the_third_sub_question_keeps_the_first_two(monkeypatch):
    import dourmouse.research_pipeline.stages as st

    calls = {"n": 0}

    def _discover(record, registry, *, sub_question_index=0, **kw):
        calls["n"] += 1
        if sub_question_index == 2:
            raise RuntimeError("RATE LIMITED")
        record.add_sources([f"https://x.example/{sub_question_index}"])
        return record

    def _extract(record, registry, *, source_index=0, sub_question_index=0, **kw):
        from dourmouse.research_pipeline.core import Claim

        record.add_claim(Claim(claim="c", source_id="s", url=record.sources[source_index], document_hash="h",
                               location="l", passage="a real passage here", retrieved_at=1.0, agent="research_info",
                               sub_question=record.plan[sub_question_index]))
        return record

    monkeypatch.setattr(st, "discover_sources", _discover)
    monkeypatch.setattr(st, "extract_evidence", _extract)
    saved = []
    skipped: list[str] = []
    rec = _record_with_plan(3)
    run_full_pipeline(rec, object(), skipped=skipped, checkpoint=lambda: saved.append(len(rec.claims)))
    assert len(rec.claims) == 2  # sub-questions 1 and 2 survived
    assert any("sub-question 3" in s and "RATE LIMITED" in s for s in skipped)
    assert saved and saved[-1] == 2  # and were checkpointed before the failure


def test_any_error_on_one_source_skips_only_that_source(monkeypatch):
    import dourmouse.research_pipeline.stages as st

    def _discover(record, registry, *, sub_question_index=0, **kw):
        record.add_sources(["https://a.example/1", "https://a.example/2"])
        return record

    def _extract(record, registry, *, source_index=0, **kw):
        if source_index == 0:
            raise TimeoutError("slow site")  # not a ValueError
        from dourmouse.research_pipeline.core import Claim

        record.add_claim(Claim(claim="c", source_id="s", url="https://a.example/2", document_hash="h", location="l",
                               passage="a real passage here", retrieved_at=1.0, agent="research_info", sub_question="sub 0"))
        return record

    monkeypatch.setattr(st, "discover_sources", _discover)
    monkeypatch.setattr(st, "extract_evidence", _extract)
    skipped: list[str] = []
    rec = _record_with_plan(1)
    run_full_pipeline(rec, object(), skipped=skipped)
    assert len(rec.claims) == 1 and skipped == ["slow site"]
