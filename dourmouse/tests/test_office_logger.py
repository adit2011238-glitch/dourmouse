"""Tests for dourmouse/office_logger.py (finding #066) -- the persistent,
append-only log of real message_bus traffic and delegate_parallel fan-out
lifecycle events. Hermetic: every test gets its own on-disk SQLite file
under tmp_path, never DEFAULT_DB.
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
