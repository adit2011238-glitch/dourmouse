"""Tests for dourmouse/office_logger.py (finding #066) -- the persistent,
append-only log of real message_bus traffic and delegate_parallel fan-out
lifecycle events. Hermetic: every test gets its own on-disk SQLite file
under tmp_path, never default_db().
"""

from __future__ import annotations

from dourmouse.office_logger import OfficeLogger


class TestOfficeLoggerMessages:
    def test_log_message_persists_a_real_row(self, tmp_path):
        log = OfficeLogger(tmp_path / "office.db")
        log.log_message({"id": "msg-1", "from": "research_info", "to": "markets",
                          "subject": "catalyst", "body": "NVDA spiked"})
        rows = log.recent_messages()
        assert len(rows) == 1
        assert rows[0]["from"] == "research_info"
        assert rows[0]["to"] == "markets"
        assert rows[0]["body"] == "NVDA spiked"

    def test_recent_messages_newest_first_and_capped(self, tmp_path):
        log = OfficeLogger(tmp_path / "office.db")
        for i in range(5):
            log.log_message({"id": f"msg-{i}", "from": "news", "to": "*", "subject": "", "body": str(i)})
        rows = log.recent_messages(limit=3)
        assert len(rows) == 3
        assert [r["body"] for r in rows] == ["4", "3", "2"]

    def test_log_message_swallows_bad_input(self, tmp_path):
        log = OfficeLogger(tmp_path / "office.db")
        log.log_message(None)  # type: ignore[arg-type]
        assert log.recent_messages() == []

    def test_survives_reopen_same_path(self, tmp_path):
        db = tmp_path / "office.db"
        OfficeLogger(db).log_message({"id": "m", "from": "a", "to": "b", "subject": "s", "body": "hello"})
        reopened = OfficeLogger(db)
        rows = reopened.recent_messages()
        assert len(rows) == 1 and rows[0]["body"] == "hello"


class TestOfficeLoggerAgentEvents:
    """Finding #067: real per-agent/per-call_id dispatch events (tool_use,
    tool_result, thinking_delta, assistant_delta, assistant_text, brain),
    additively tagged by dispatch.py's _emit_event."""

    def test_tagged_tool_use_is_persisted(self, tmp_path):
        log = OfficeLogger(tmp_path / "office.db")
        log.on_event({"type": "tool_use", "name": "review_read_file", "agent": "reviewer", "call_id": "c1", "depth": 1})
        rows = log.transcript(call_id="c1")
        assert len(rows) == 1
        assert rows[0]["agent"] == "reviewer" and rows[0]["type"] == "tool_use"
        assert rows[0]["name"] == "review_read_file"

    def test_untagged_event_is_skipped_not_fabricated(self, tmp_path):
        # No call_id at all (e.g. an emitter that never passed ctx) -- must
        # never be logged under a made-up identity.
        log = OfficeLogger(tmp_path / "office.db")
        log.on_event({"type": "tool_use", "name": "review_read_file"})
        assert log.transcript() == []

    def test_unrelated_event_types_are_ignored(self, tmp_path):
        log = OfficeLogger(tmp_path / "office.db")
        log.on_event({"type": "budget_exhausted", "agent": "reviewer", "call_id": "c1"})
        log.on_event({"type": "stop", "agent": "reviewer", "call_id": "c1"})
        assert log.transcript() == []

    def test_transcript_scoped_by_agent_and_call_id_oldest_first(self, tmp_path):
        log = OfficeLogger(tmp_path / "office.db")
        log.on_event({"type": "thinking_delta", "text": "step 1", "agent": "reviewer", "call_id": "c1"})
        log.on_event({"type": "thinking_delta", "text": "step 2", "agent": "reviewer", "call_id": "c1"})
        log.on_event({"type": "tool_use", "name": "x", "agent": "reviewer", "call_id": "c2"})
        log.on_event({"type": "tool_use", "name": "y", "agent": "security", "call_id": "c3"})
        by_call = log.transcript(call_id="c1")
        assert [r["text"] for r in by_call] == ["step 1", "step 2"]
        by_agent = log.transcript(agent="reviewer")
        assert len(by_agent) == 3  # both c1 events + the c2 tool_use
        security_rows = log.transcript(agent="security")
        assert len(security_rows) == 1
        assert security_rows[0]["call_id"] == "c3" and security_rows[0]["name"] == "y"

    def test_two_concurrent_calls_to_the_same_agent_stay_separate(self, tmp_path):
        # The exact real gap flaw #4 named: two independent runs against
        # the SAME agent name. call_id (not agent name alone) is what lets
        # this store tell them apart.
        log = OfficeLogger(tmp_path / "office.db")
        log.on_event({"type": "tool_use", "name": "review_read_file", "agent": "reviewer", "call_id": "call-A"})
        log.on_event({"type": "tool_use", "name": "review_diff", "agent": "reviewer", "call_id": "call-B"})
        assert [r["name"] for r in log.transcript(call_id="call-A")] == ["review_read_file"]
        assert [r["name"] for r in log.transcript(call_id="call-B")] == ["review_diff"]
        assert len(log.transcript(agent="reviewer")) == 2


class TestOfficeLoggerFanoutEvents:
    def test_on_event_persists_delegate_parallel_branch(self, tmp_path):
        log = OfficeLogger(tmp_path / "office.db")
        log.on_event({
            "type": "delegate_parallel_branch", "phase": "start", "run_id": "run-1",
            "index": 0, "total": 2, "agent": "reviewer",
        })
        log.on_event({
            "type": "delegate_parallel_branch", "phase": "result", "run_id": "run-1",
            "index": 0, "total": 2, "agent": "reviewer", "ok": True, "error": "", "elapsed_s": 1.23,
        })
        events = log.recent_fanout_events(run_id="run-1")
        assert len(events) == 2
        assert events[0]["phase"] == "result"  # newest first
        assert events[0]["ok"] is True
        assert events[0]["elapsed_s"] == 1.23
        assert events[1]["phase"] == "start"

    def test_on_event_ignores_unrelated_event_types(self, tmp_path):
        log = OfficeLogger(tmp_path / "office.db")
        log.on_event({"type": "tool_use", "name": "review_read_file"})
        log.on_event({"type": "assistant_delta", "text": "hi"})
        assert log.recent_fanout_events() == []

    def test_recent_fanout_events_scoped_by_run_id(self, tmp_path):
        log = OfficeLogger(tmp_path / "office.db")
        log.on_event({"type": "delegate_parallel_branch", "phase": "start", "run_id": "run-a", "agent": "x"})
        log.on_event({"type": "delegate_parallel_branch", "phase": "start", "run_id": "run-b", "agent": "y"})
        assert len(log.recent_fanout_events(run_id="run-a")) == 1
        assert len(log.recent_fanout_events()) == 2
