"""User-defined recurring workflow tests (v5.x) — schedules.py + roster tools.

Deterministic parser, store CRUD, and the SchedulerRunner (injected
fetcher + injected clock — no real waiting, no network).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from dourmouse import schedules
from dourmouse.general_roster import build_general_registry


class TestParseSchedule:
    def test_every_weekday(self):
        s = schedules.parse_schedule("every Monday at 9:00")
        assert s == {"kind": "weekday", "time": "09:00", "weekday": 0, "interval_seconds": None}

    def test_weekday_abbrev_and_12h(self):
        s = schedules.parse_schedule("every fri at 2pm")
        assert s["kind"] == "weekday" and s["weekday"] == 4 and s["time"] == "14:00"

    def test_daily_shapes(self):
        for text, want in [
            ("daily", "09:00"),
            ("every day at 8:30", "08:30"),
            ("at 7:15", "07:15"),
        ]:
            s = schedules.parse_schedule(text)
            assert s["kind"] == "daily" and s["time"] == want, text

    def test_interval(self):
        s = schedules.parse_schedule("every 30 minutes")
        assert s["kind"] == "interval" and s["interval_seconds"] == 1800
        assert schedules.parse_schedule("every 2 hours")["interval_seconds"] == 7200
        assert schedules.parse_schedule("every 1 day")["interval_seconds"] == 86400

    def test_weekly_defaults_monday_9am(self):
        s = schedules.parse_schedule("weekly")
        assert s["kind"] == "weekday" and s["weekday"] == 0 and s["time"] == "09:00"

    def test_rejects_garbage_honestly(self):
        with pytest.raises(ValueError, match="not understood"):
            schedules.parse_schedule("sometimes")
        with pytest.raises(ValueError, match="must describe when"):
            schedules.parse_schedule("")
        with pytest.raises(ValueError, match="valid"):
            schedules.parse_schedule("every Monday at 25:00")


class TestNextRun:
    def test_daily_rolls_to_tomorrow_after_passed_time(self):
        spec = {"kind": "daily", "time": "09:00", "weekday": None, "interval_seconds": None}
        after = datetime(2026, 8, 12, 10, 0)  # 10am — 9am already passed
        assert schedules.next_run(spec, after) == datetime(2026, 8, 13, 9, 0)

    def test_daily_same_day_before_time(self):
        spec = {"kind": "daily", "time": "09:00", "weekday": None, "interval_seconds": None}
        after = datetime(2026, 8, 12, 8, 0)
        assert schedules.next_run(spec, after) == datetime(2026, 8, 12, 9, 0)

    def test_weekday_finds_next_monday(self):
        spec = {"kind": "weekday", "time": "09:00", "weekday": 0, "interval_seconds": None}
        after = datetime(2026, 8, 12, 8, 0)  # Wednesday
        assert schedules.next_run(spec, after).weekday() == 0
        assert schedules.next_run(spec, after) > after

    def test_interval_adds_offset(self):
        spec = {"kind": "interval", "interval_seconds": 1800}
        after = datetime(2026, 8, 12, 9, 0)
        assert schedules.next_run(spec, after) == datetime(2026, 8, 12, 9, 30)


class _FakeTracker:
    def __init__(self):
        self.events = []

    def on_event(self, event):
        self.events.append(event)


class TestSchedulerRunner:
    def _registry(self):
        return build_general_registry()

    def test_runs_due_interval_task(self, tmp_path, monkeypatch):
        store = schedules.Schedules(tmp_path / "schedules.jsonl")
        store.add("list_tasks", {}, {
            "kind": "interval", "interval_seconds": 60,
        }, "every 60 minutes")
        t0 = datetime(2026, 8, 12, 9, 0)
        now = {"t": t0}
        calls = []

        def fake_now():
            return now["t"]

        def fake_fetcher(tool, args):
            calls.append((tool, args))
            return "ran ok"

        tracker = _FakeTracker()
        runner = schedules.SchedulerRunner(
            self._registry(), tracker, store=store,
            fetcher=fake_fetcher, now_fn=fake_now, tick=1.0,
        )
        runner._tick_once()
        assert calls == [("list_tasks", {})]
        assert tracker.events and tracker.events[0]["type"] == "schedule"
        assert store.list()[0]["last_run"] is not None
        # second tick: not due again (60s interval, clock unchanged)
        runner._tick_once()
        assert len(calls) == 1

    def test_not_due_until_scheduled_time(self, tmp_path):
        store = schedules.Schedules(tmp_path / "schedules.jsonl")
        entry = store.add("list_tasks", {}, {
            "kind": "daily", "time": "09:00", "weekday": None,
        }, "daily at 9:00")
        t0 = datetime(2026, 8, 12, 8, 0)  # 8am, due at 9am
        calls = []

        def fake_now():
            return t0

        tracker = _FakeTracker()
        runner = schedules.SchedulerRunner(
            self._registry(), tracker, store=store,
            fetcher=lambda tool, args: calls.append(tool) or "ok",
            now_fn=fake_now, tick=1.0,
        )
        runner._tick_once()
        assert calls == []  # not due yet

    def test_catch_up_runs_once_after_missed_window(self, tmp_path):
        store = schedules.Schedules(tmp_path / "schedules.jsonl")
        # created yesterday 08:00, never ran; now 09:05 -> due (catch-up)
        store.add("list_tasks", {}, {
            "kind": "daily", "time": "09:00", "weekday": None,
        }, "daily at 9:00", created_at="2026-08-11T08:00:00")
        calls = []

        def fake_now():
            return datetime(2026, 8, 12, 9, 5)

        tracker = _FakeTracker()
        runner = schedules.SchedulerRunner(
            self._registry(), tracker, store=store,
            fetcher=lambda tool, args: calls.append(tool) or "ok",
            now_fn=fake_now, tick=1.0,
        )
        runner._tick_once()
        assert calls == ["list_tasks"]
        assert store.list()[0]["last_run"] is not None

    def test_missing_tool_reports_honestly(self, tmp_path):
        store = schedules.Schedules(tmp_path / "schedules.jsonl")
        store.add("no_such_tool", {}, {
            "kind": "interval", "interval_seconds": 60,
        }, "every 60 minutes")
        tracker = _FakeTracker()
        runner = schedules.SchedulerRunner(
            self._registry(), tracker, store=store, now_fn=lambda: datetime(2026, 8, 12, 9, 0),
            tick=1.0,
        )
        runner._tick_once()
        assert tracker.events
        assert "no such tool" in tracker.events[0]["text"]


class TestStore:
    def test_crud(self, tmp_path):
        store = schedules.Schedules(tmp_path / "schedules.jsonl")
        e = store.add("gmail_search", {"query": "receipt"}, {
            "kind": "weekday", "time": "09:00", "weekday": 0,
        }, "every Monday at 9:00")
        assert store.list()[0]["id"] == e["id"]
        assert store.remove(e["id"]) is True
        assert store.remove(e["id"]) is False
        assert store.list() == []

    def test_persists_across_instances(self, tmp_path):
        path = tmp_path / "schedules.jsonl"
        schedules.Schedules(path).add("list_tasks", {}, {
            "kind": "interval", "interval_seconds": 60,
        }, "every 60 minutes")
        reloaded = schedules.Schedules(path).list()
        assert len(reloaded) == 1 and reloaded[0]["tool"] == "list_tasks"


class TestRosterTools:
    def test_schedule_recurring_validates_tool(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
        registry = build_general_registry()
        tool = next(t for t in registry.get_subagent("tasks").tools if t.name == "schedule_recurring")
        out = tool.handler({"tool": "no_such_tool", "arguments": {}, "schedule_text": "daily at 9:00"})
        assert "no such tool" in out and "Nothing was scheduled" in out

    def test_schedule_list_cancel_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
        registry = build_general_registry()
        add = next(t for t in registry.get_subagent("tasks").tools if t.name == "schedule_recurring")
        lst = next(t for t in registry.get_subagent("tasks").tools if t.name == "list_schedules")
        cancel = next(t for t in registry.get_subagent("tasks").tools if t.name == "cancel_schedule")

        out = add.handler({"tool": "list_tasks", "arguments": {}, "schedule_text": "every Monday at 9:00"})
        assert "SCHEDULED sched-001" in out
        listed = lst.handler({})
        assert "sched-001" in listed and "list_tasks" in listed
        sid = "sched-001"
        assert f"SCHEDULE CANCELLED: {sid}" in cancel.handler({"schedule_id": sid})
        assert "SCHEDULES: none" in lst.handler({})

    def test_bad_schedule_text_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
        registry = build_general_registry()
        add = next(t for t in registry.get_subagent("tasks").tools if t.name == "schedule_recurring")
        out = add.handler({"tool": "list_tasks", "arguments": {}, "schedule_text": "sometimes"})
        assert "SCHEDULE REJECTED" in out
