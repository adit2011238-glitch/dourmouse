"""User-defined recurring workflow tests (v5.x) — schedules.py + roster tools.

Deterministic parser, store CRUD, and the SchedulerRunner (injected
fetcher + injected clock — no real waiting, no network).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from dourmouse import schedules
from dourmouse.dispatch import DispatchRegistry, Permission, Subagent, ToolSpec
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

    def test_a_paused_entry_never_fires_even_when_due(self, tmp_path):
        """set_enabled(False) (the TIMETABLE UI's PAUSE button) must
        actually stop the real runner, not just flip a cosmetic flag --
        _tick_once's own enabled check is what this proves end to end."""
        store = schedules.Schedules(tmp_path / "schedules.jsonl")
        entry = store.add("list_tasks", {}, {
            "kind": "interval", "interval_seconds": 60,
        }, "every 60 minutes")
        store.set_enabled(entry["id"], False)
        calls = []
        tracker = _FakeTracker()
        runner = schedules.SchedulerRunner(
            self._registry(), tracker, store=store,
            fetcher=lambda tool, args: calls.append(tool) or "ok",
            now_fn=lambda: datetime(2026, 8, 12, 9, 0), tick=1.0,
        )
        runner._tick_once()
        assert calls == []
        assert store.list()[0]["last_run"] is None

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

    def test_gated_tool_fails_closed_never_calls_handler(self, tmp_path):
        """v8.15: the runner has no confirmation channel — nobody is present
        when a schedule fires. A gated tool reaching the store any other way
        (a legacy entry, a future caller of Schedules.add() that skips the
        schedule_recurring roster tool) must fail closed, not execute
        unconfirmed. A synthetic registry + tool (rather than a real gated
        tool like gmail_send, whose handler is a private closure with no
        module-level name to intercept) proves the RUNNER itself is guarded
        — defense in depth, not just schedule_recurring's creation-time
        refusal.
        """
        called = []
        gated = ToolSpec(
            name="gated_thing",
            description="a tool that requires confirmation",
            parameters={"type": "object", "properties": {}},
            handler=lambda a: called.append(a) or "DID THE THING",
            permission=Permission.REQUIRES_CONFIRMATION,
            confirm_prompt=lambda a: "confirm?",
        )
        registry = DispatchRegistry()
        registry.register_subagent(
            Subagent(name="g", domain="Test", description="x", tools=(gated,))
        )
        store = schedules.Schedules(tmp_path / "schedules.jsonl")
        store.add("gated_thing", {}, {"kind": "interval", "interval_seconds": 60}, "every 60 minutes")
        tracker = _FakeTracker()
        runner = schedules.SchedulerRunner(
            registry, tracker, store=store,
            now_fn=lambda: datetime(2026, 8, 12, 9, 0), tick=1.0,
        )
        runner._tick_once()
        assert not called, "gated tool's handler ran on an unattended schedule"
        assert tracker.events
        text = tracker.events[0]["text"]
        assert "CONFIRMATION REQUIRED" in text and "NOT executed" in text
        # The entry is left enabled (not silently disabled) — an honest,
        # visible "not executed" every tick beats a schedule that looks
        # live but quietly never fires.
        assert store.list()[0]["enabled"] is True

    def test_regular_tool_from_real_roster_still_runs_unattended(self, tmp_path):
        """Companion to the gated-tool test above: confirm the fail-safe
        check doesn't over-match and block ordinary scheduled tools too."""
        store = schedules.Schedules(tmp_path / "schedules.jsonl")
        store.add("list_tasks", {}, {"kind": "interval", "interval_seconds": 60}, "every 60 minutes")
        tracker = _FakeTracker()
        runner = schedules.SchedulerRunner(
            self._registry(), tracker, store=store,
            now_fn=lambda: datetime(2026, 8, 12, 9, 0), tick=1.0,
        )
        runner._tick_once()
        assert tracker.events
        assert "CONFIRMATION REQUIRED" not in tracker.events[0]["text"]

    def _registry_with(self, handler):
        tool = ToolSpec(
            name="probe", description="a regular probe", parameters={"type": "object", "properties": {}},
            handler=handler,
        )
        registry = DispatchRegistry()
        registry.register_subagent(Subagent(name="p", domain="Test", description="x", tools=(tool,)))
        return registry

    def test_a_scheduled_result_is_scrubbed_of_credentials(self, tmp_path):
        """Finding #137: a scheduled run's output is redacted like any tool result."""
        registry = self._registry_with(lambda a: "GEMINI_API_KEY=AIzaSyFAKEFAKEFAKEFAKEFAKEFAKE12345")
        store = schedules.Schedules(tmp_path / "schedules.jsonl")
        store.add("probe", {}, {"kind": "interval", "interval_seconds": 60}, "every 60 minutes")
        tracker = _FakeTracker()
        runner = schedules.SchedulerRunner(
            registry, tracker, store=store, now_fn=lambda: datetime(2026, 8, 12, 9, 0), tick=1.0,
        )
        runner._tick_once()
        assert "AIzaSyFAKE" not in tracker.events[0]["text"]

    def test_a_scheduled_run_lands_in_the_action_ledger_under_its_own_name(self, tmp_path):
        from dourmouse import execution_policy

        seen = []
        execution_policy.set_action_sink(lambda kind, source, subject, actor, data: seen.append((kind, subject, actor)))
        try:
            registry = self._registry_with(lambda a: "ok")
            store = schedules.Schedules(tmp_path / "schedules.jsonl")
            store.add("probe", {}, {"kind": "interval", "interval_seconds": 60}, "every 60 minutes")
            runner = schedules.SchedulerRunner(
                registry, _FakeTracker(), store=store, now_fn=lambda: datetime(2026, 8, 12, 9, 0), tick=1.0,
            )
            runner._tick_once()
        finally:
            execution_policy.set_action_sink(None)
        assert ("action.proposed", "probe", "scheduler") in seen
        assert ("action.executed", "probe", "scheduler") in seen


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

    def test_set_enabled_pauses_and_resumes_without_losing_the_entry(self, tmp_path):
        store = schedules.Schedules(tmp_path / "schedules.jsonl")
        e = store.add("gmail_search", {"query": "receipt"}, {
            "kind": "weekday", "time": "09:00", "weekday": 0,
        }, "every Monday at 9:00")
        assert store.list()[0]["enabled"] is True
        assert store.set_enabled(e["id"], False) is True
        assert store.list()[0]["enabled"] is False
        assert store.list()[0]["id"] == e["id"]  # paused, not recreated
        assert store.set_enabled(e["id"], True) is True
        assert store.list()[0]["enabled"] is True

    def test_set_enabled_on_an_unknown_id_reports_false(self, tmp_path):
        store = schedules.Schedules(tmp_path / "schedules.jsonl")
        assert store.set_enabled("no-such-schedule", False) is False

    def test_update_spec_reschedules_the_real_underlying_job(self, tmp_path):
        store = schedules.Schedules(tmp_path / "schedules.jsonl")
        e = store.add("gmail_search", {"query": "receipt"}, {
            "kind": "weekday", "time": "09:00", "weekday": 0,
        }, "every Monday at 9:00")
        updated = store.update_spec(e["id"], "every Friday at 17:00")
        assert updated["schedule_text"] == "every Friday at 17:00"
        assert updated["spec"]["weekday"] == 4
        assert updated["spec"]["time"] == "17:00"
        reloaded = store.list()[0]
        assert reloaded["schedule_text"] == "every Friday at 17:00"

    def test_update_spec_never_touches_the_tool_or_its_arguments(self, tmp_path):
        store = schedules.Schedules(tmp_path / "schedules.jsonl")
        e = store.add("gmail_search", {"query": "receipt"}, {
            "kind": "weekday", "time": "09:00", "weekday": 0,
        }, "every Monday at 9:00")
        updated = store.update_spec(e["id"], "daily at 8:00")
        assert updated["tool"] == "gmail_search"
        assert updated["arguments"] == {"query": "receipt"}
        assert updated["id"] == e["id"]

    def test_update_spec_preserves_enabled_and_last_run_history(self, tmp_path):
        store = schedules.Schedules(tmp_path / "schedules.jsonl")
        e = store.add("list_tasks", {}, {"kind": "interval", "interval_seconds": 60}, "every 60 minutes")
        store.mark_run(e["id"])
        store.set_enabled(e["id"], False)
        updated = store.update_spec(e["id"], "daily at 8:00")
        assert updated["enabled"] is False
        assert updated["last_run"] is not None

    def test_update_spec_rejects_an_unparseable_schedule_with_an_honest_reason(self, tmp_path):
        store = schedules.Schedules(tmp_path / "schedules.jsonl")
        e = store.add("list_tasks", {}, {"kind": "interval", "interval_seconds": 60}, "every 60 minutes")
        with pytest.raises(ValueError):
            store.update_spec(e["id"], "sometimes, whenever")
        # the original schedule survives a rejected edit untouched
        assert store.list()[0]["schedule_text"] == "every 60 minutes"

    def test_update_spec_on_an_unknown_id_raises(self, tmp_path):
        store = schedules.Schedules(tmp_path / "schedules.jsonl")
        with pytest.raises(ValueError):
            store.update_spec("no-such-schedule", "daily at 9:00")


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

    def test_schedule_recurring_refuses_gated_tool(self, tmp_path, monkeypatch):
        """v8.15: a tool that REQUIRES_CONFIRMATION can't be scheduled at
        all — the runner has no confirmation channel when a schedule fires
        unattended, so refuse at creation time with an honest reason rather
        than silently creating a schedule that can never do anything."""
        monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path))
        registry = build_general_registry()
        add = next(t for t in registry.get_subagent("tasks").tools if t.name == "schedule_recurring")
        lst = next(t for t in registry.get_subagent("tasks").tools if t.name == "list_schedules")
        out = add.handler({
            "tool": "gmail_send",
            "arguments": {"to": "x@example.com", "subject": "hi", "body": "hi"},
            "schedule_text": "daily at 9:00",
        })
        assert "cannot schedule 'gmail_send'" in out
        assert "requires_confirmation" in out
        assert "Nothing was scheduled" in out
        assert "SCHEDULES: none" in lst.handler({})
