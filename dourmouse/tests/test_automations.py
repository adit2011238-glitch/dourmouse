"""Automations (plan 2.2, finding 207): store v2, triggers, caps, parking, runner, API. No model is called anywhere."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from dourmouse import attention, automation_runs as runs, automation_triggers as triggers, schedules
from dourmouse.attention import AttentionStore
from dourmouse.dispatch import DispatchRegistry, Permission, Subagent, ToolSpec
from dourmouse.os_api import ApiError, Request, automations as api

NOW = datetime(2026, 11, 17, 10, 0)


def tool(name, permission=Permission.REGULAR, result="OK DONE", calls=None):
    return ToolSpec(
        name=name, description=name, parameters={"type": "object", "properties": {}},
        handler=lambda a: (calls.append(a) if calls is not None else None) or result,
        permission=permission, confirm_prompt=(lambda a: "confirm?") if permission != Permission.REGULAR else None,
    )


@pytest.fixture
def feed(tmp_path):
    store = AttentionStore(tmp_path / "attn")
    attention.set_attention_store(store)
    yield store
    attention.set_attention_store(None)


@pytest.fixture
def calls():
    return []


@pytest.fixture
def registry(calls):
    reg = DispatchRegistry()
    reg.register_subagent(Subagent(name="t", domain="Test", description="x", tools=(
        tool("ok_tool", calls=calls), tool("bad_tool", result="ERROR: it broke"),
        tool("gated_tool", Permission.REQUIRES_CONFIRMATION, "SENT", calls=calls),
        tool("never_tool", Permission.PROHIBITED),
    )))
    return reg


@pytest.fixture
def clock():
    box = [NOW]
    fn = lambda: box[0]  # noqa: E731
    fn.box = box
    return fn


@pytest.fixture
def store(tmp_path):
    return schedules.Schedules(tmp_path / "schedules.jsonl")


@pytest.fixture
def started():
    return []


@pytest.fixture
def engine(store, registry, tmp_path, feed, clock, started):
    def starter(prompt, owner, name, max_steps):
        started.append({"prompt": prompt, "owner": owner, "name": name, "max_steps": max_steps})
        return f"goal-{len(started)}"

    return runs.Engine(store, registry, runs.RunHistory(tmp_path / "runs.jsonl"), job_starter=starter,
                       job_status=lambda jid: ("EXECUTING", ""), now_fn=clock)


def add(store, action, trigger=None, spec=None, caps=None, name="Test", delivery=None):
    return store.add_v2(name, trigger or {"type": "time"}, action, spec=spec or {"kind": "daily", "time": "08:00"},
                        caps=caps, delivery=delivery)


# ---- the clock ----------------------------------------------------------------------------------------------


def test_weekdays_skip_the_weekend():
    spec = schedules.parse_schedule("every weekday at 8:00")
    friday_noon = datetime(2026, 11, 20, 12, 0)
    assert schedules.next_run(spec, friday_noon) == datetime(2026, 11, 23, 8, 0)
    assert schedules.describe_spec(spec) == "every weekday at 08:00"


def test_one_time_reminders_fire_once_then_never_again():
    spec = schedules.parse_schedule("tomorrow at 5pm", now=NOW)
    assert spec["kind"] == "once" and spec["when"] == "2026-11-18T17:00"
    assert schedules.next_run(spec, NOW) == datetime(2026, 11, 18, 17, 0)
    assert schedules.next_run(spec, datetime(2026, 11, 18, 17, 1)).year == 9999
    assert schedules.parse_schedule("in 20 minutes", now=NOW)["when"] == "2026-11-17T10:20"
    with pytest.raises(ValueError):
        schedules.parse_schedule("in 5000 hours")


def test_a_bare_hour_is_the_24_hour_clock():
    assert schedules.parse_schedule("every weekday at 8")["time"] == "08:00"
    with pytest.raises(ValueError):
        schedules.parse_schedule("daily at 25")


def test_a_sentence_splits_into_the_clock_and_what_to_do():
    r = runs.parse_sentence("every weekday at 8 check my security", NOW)
    assert r["spec"]["kind"] == "weekdays" and r["rest"] == "check my security" and not r["reminder"]
    r = runs.parse_sentence("remind me tomorrow at 5pm to call the bank", NOW)
    assert r["reminder"] and r["rest"] == "call the bank"
    r = runs.parse_sentence("remind me to call the bank tomorrow at 5pm", NOW)
    assert r["reminder"] and r["rest"] == "call the bank" and r["spec"]["kind"] == "once"
    with pytest.raises(ValueError, match="Accepted shapes"):
        runs.parse_sentence("whenever you feel like it", NOW)


# ---- the store ----------------------------------------------------------------------------------------------


def test_v1_rows_still_read_and_v2_rows_keep_the_v1_fields(store):
    store.add("ok_tool", {}, {"kind": "interval", "interval_seconds": 60}, "every 1 minutes")
    v2 = add(store, {"type": "prompt", "prompt": "do it", "owner": "orchestrator"})
    assert v2["version"] == 2 and v2["tool"] == "automation_prompt" and v2["arguments"] == {"prompt": "do it"}
    assert [e["id"] for e in store.list()] == ["sched-001", "sched-002"]


def test_three_failures_in_a_row_switch_it_off_and_resume_clears_the_streak(store):
    e = add(store, {"type": "tool", "tool": "bad_tool", "arguments": {}})
    for _ in range(2):
        store.record_outcome(e["id"], False, "failed")
    assert store.get(e["id"])["enabled"] is True
    store.record_outcome(e["id"], True, "ok")  # a success clears the count
    for _ in range(3):
        store.record_outcome(e["id"], False, "failed")
    got = store.get(e["id"])
    assert got["enabled"] is False and got["state"]["auto_disabled"] is True
    store.set_enabled(e["id"], True)
    got = store.get(e["id"])
    assert got["enabled"] is True and got["state"]["fail_count"] == 0 and not got["state"]["auto_disabled"]


def test_the_day_count_rolls_over(store):
    e = add(store, {"type": "reminder", "text": "x"})
    assert store.count_run(e["id"], NOW) == 1 and store.count_run(e["id"], NOW) == 2
    assert store.runs_today(store.get(e["id"]), NOW) == 2
    assert store.runs_today(store.get(e["id"]), NOW + timedelta(days=1)) == 0
    assert store.count_run(e["id"], NOW + timedelta(days=1)) == 1


# ---- the engine ---------------------------------------------------------------------------------------------


def test_a_reminder_needs_no_model_and_tells_the_owner(store, engine, feed):
    e = add(store, {"type": "reminder", "text": "Call the bank"}, name="Bank")
    row = engine.run(e, "Once")
    assert row["status"] == "ok" and row["result"] == "Call the bank"
    item = feed.items()[0]
    assert item["kind"] == "finished" and item["source"] == "automations" and item["screen"] == "timetable"
    assert item["params"] == {"id": e["id"]}
    assert engine.history.last(e["id"])["id"] == row["id"]


def test_a_regular_tool_runs_and_a_failing_one_is_recorded_as_failed(store, engine, calls):
    good = engine.run(add(store, {"type": "tool", "tool": "ok_tool", "arguments": {"a": 1}}), "x")
    assert good["status"] == "ok" and calls == [{"a": 1}] and good["tools"] == ["ok_tool"]
    bad = engine.run(add(store, {"type": "tool", "tool": "bad_tool", "arguments": {}}), "x")
    assert bad["status"] == "failed" and "broke" in bad["result"]
    gone = engine.run(add(store, {"type": "tool", "tool": "nope", "arguments": {}}), "x")
    assert gone["status"] == "failed" and "no tool called nope" in gone["result"]


def test_a_gated_tool_parks_and_runs_only_after_the_owner_approves(store, engine, feed, calls):
    e = add(store, {"type": "tool", "tool": "gated_tool", "arguments": {"to": "x"}}, name="Send")
    row = engine.run(e, "x")
    assert row["status"] == "parked" and calls == [] and row["parked"]["tool"] == "gated_tool"
    item = [i for i in feed.items() if i["kind"] == "needs_you"][0]
    assert item["action"] == {"label": "Review"} and item["params"] == {"id": e["id"], "run": row["id"]}
    assert store.get(e["id"])["state"]["fail_count"] == 0  # parked is not a failure
    done = engine.approve_parked(row["id"])
    assert done["status"] == "ok" and calls == [{"to": "x"}] and done["approved"] is True
    assert not [i for i in feed.items() if i["kind"] == "needs_you"]  # the item settled
    with pytest.raises(LookupError):
        engine.approve_parked(row["id"])  # only once


def test_declining_a_parked_step_never_runs_it(store, engine, calls):
    row = engine.run(add(store, {"type": "tool", "tool": "gated_tool", "arguments": {}}), "x")
    out = engine.decline_parked(row["id"])
    assert out["status"] == "skipped" and calls == []


def test_a_prohibited_tool_can_never_run_or_be_approved(store, engine):
    row = engine.run(add(store, {"type": "tool", "tool": "never_tool", "arguments": {}}), "x")
    assert row["status"] == "failed" and "never allowed" in row["result"]
    with pytest.raises(LookupError):
        engine.approve_parked(row["id"])


def test_the_daily_run_cap_stops_automatic_runs_but_not_run_now(store, engine, feed):
    e = add(store, {"type": "reminder", "text": "x"}, caps={"max_runs_per_day": 2, "max_steps": 8})
    assert [engine.run(store.get(e["id"]), "x")["status"] for _ in range(3)] == ["ok", "ok", "capped"]
    assert store.runs_today(store.get(e["id"]), NOW) == 2  # the capped run was not counted
    assert [i for i in feed.items() if "daily limit" in i["title"]]
    assert engine.run(store.get(e["id"]), "Run now", manual=True)["status"] == "ok"
    engine.run(store.get(e["id"]), "x")  # still capped, still one notice a day
    assert len([i for i in feed.items() if "daily limit" in i["title"]]) == 1


def test_a_prompt_run_starts_one_job_with_the_step_cap_as_its_budget(store, engine, started):
    e = add(store, {"type": "prompt", "prompt": "Summarise", "owner": "researcher"}, caps={"max_runs_per_day": 6, "max_steps": 5})
    row = engine.run(e, "x")
    assert row["status"] == "running" and row["job_id"] == "goal-1"
    assert started == [{"prompt": "Summarise", "owner": "researcher", "name": "Test", "max_steps": 5}]


def test_a_prompt_run_finishes_later_and_the_owner_is_told_once(store, engine, feed, started):
    e = add(store, {"type": "prompt", "prompt": "p", "owner": "orchestrator"})
    row = engine.run(e, "x")
    states = {"v": ("EXECUTING", "")}
    engine._job_status = lambda jid: states["v"]
    assert engine.refresh_running() == 0
    states["v"] = ("COMPLETED", "")
    assert engine.refresh_running() == 1 and engine.refresh_running() == 0
    assert engine.history.get(row["id"])["status"] == "ok"
    assert len([i for i in feed.items() if i["kind"] == "finished"]) == 1


def test_a_job_that_needs_approval_or_hits_its_budget_is_said_plainly(store, engine, feed):
    e = add(store, {"type": "prompt", "prompt": "p", "owner": "orchestrator"})
    row = engine.run(e, "x")
    engine._job_status = lambda jid: ("WAITING_FOR_APPROVAL", "")
    engine.refresh_running()
    assert engine.history.get(row["id"])["status"] == "parked"
    assert [i for i in feed.items() if i["kind"] == "needs_you" and i["screen"] == "goals"]
    engine._job_status = lambda jid: ("BLOCKED", "the budget is used up: 8 of 8 step runs. Raise it to carry on.")
    engine.refresh_running()
    got = engine.history.get(row["id"])
    assert got["status"] == "capped" and "8 of 8" in got["result"]
    assert store.get(e["id"])["state"]["fail_count"] == 0  # stopping at the step limit is not a failure


def test_three_failed_runs_switch_it_off_and_say_so(store, engine, feed):
    e = add(store, {"type": "tool", "tool": "bad_tool", "arguments": {}}, name="Flaky")
    for _ in range(3):
        engine.run(store.get(e["id"]), "x")
    assert store.get(e["id"])["enabled"] is False
    assert [i for i in feed.items() if i["kind"] == "needs_you" and "switched off after 3 failures" in i["title"]]


def test_delivery_off_is_quiet_but_never_hides_a_needs_you_item(store, engine, feed):
    e = add(store, {"type": "reminder", "text": "x"}, delivery={"notify": False})
    engine.run(e, "x")
    assert feed.items() == []
    g = add(store, {"type": "tool", "tool": "gated_tool", "arguments": {}}, delivery={"notify": False})
    engine.run(g, "x")
    assert [i for i in feed.items() if i["kind"] == "needs_you"]


def test_the_history_keeps_ninety_days_and_survives_a_damaged_line(tmp_path):
    now = [time.time()]
    hist = runs.RunHistory(tmp_path / "r.jsonl", clock=lambda: now[0])
    hist.add({"id": "run-old", "automation_id": "sched-001", "ts": now[0] - 91 * 86400})
    now[0] += 7200  # past the hourly trim gate
    hist.add({"id": "run-new", "automation_id": "sched-001", "ts": now[0]})
    assert [r["id"] for r in hist.for_automation("sched-001")] == ["run-new"]
    with open(tmp_path / "r.jsonl", "a") as f:
        f.write("{not json\n")
    assert hist.get("run-new") is not None


# ---- triggers -----------------------------------------------------------------------------------------------


def test_a_new_file_fires_once_and_old_files_never_do(tmp_path):
    watch = tmp_path / "in"
    watch.mkdir()
    (watch / "old.pdf").write_text("x")
    trig = triggers.clean_trigger({"type": "file_appears", "folder": str(watch), "pattern": "*.pdf"})
    entry = {"trigger": trig, "state": {}}
    events, change = triggers.poll(entry)
    assert events == [] and change["seen"] == ["old.pdf"]  # the first look is only a baseline
    entry["state"] = change
    (watch / "a.pdf").write_text("x")
    (watch / "b.pdf.crdownload").write_text("x")  # half-downloaded
    (watch / ".hidden.pdf").write_text("x")
    (watch / "c.txt").write_text("x")  # not the pattern
    events, change = triggers.poll(entry)
    assert len(events) == 1 and events[0]["names"] == ["a.pdf"]
    entry["state"] = {**entry["state"], **change}
    assert triggers.poll(entry)[0] == []  # not again


def test_a_vanished_folder_is_reported_not_hidden(tmp_path):
    watch = tmp_path / "gone"
    watch.mkdir()
    entry = {"trigger": {"type": "file_appears", "folder": str(watch), "pattern": "*"}, "state": {"seen": []}}
    watch.rmdir()
    events, change = triggers.poll(entry)
    assert events == [] and "cannot read the folder" in change["watch_error"]


def test_folders_that_hold_keys_or_system_files_cannot_be_watched(tmp_path):
    for bad in ("/etc", "/System/Library", str(tmp_path / ".ssh"), "relative/path", ""):
        with pytest.raises(ValueError):
            triggers.clean_folder(bad)
    (tmp_path / ".ssh").mkdir()
    with pytest.raises(ValueError, match="keys or system"):
        triggers.clean_folder(str(tmp_path / ".ssh"))


def test_the_mail_trigger_is_off_until_3_1():
    assert triggers.MAIL_TRIGGER_ENABLED is False
    with pytest.raises(ValueError, match="3.1"):
        triggers.clean_trigger({"type": "mail_match"})
    assert triggers.poll({"trigger": {"type": "mail_match"}, "state": {}}) == ([], {})


def test_a_security_finding_fires_for_new_items_only(feed):
    feed.add("heads_up", "Old finding", source="security")
    entry = {"trigger": {"type": "security_finding"}, "state": {}}
    events, change = triggers.poll(entry, attention_store=feed)
    assert events == [] and "after_id" in change
    entry["state"] = change
    feed.add("finished", "Not a finding", source="security")
    feed.add("heads_up", "Other source", source="mail")
    assert triggers.poll(entry, attention_store=feed)[0] == []
    feed.add("needs_you", "New finding", source="security")
    events, change = triggers.poll(entry, attention_store=feed)
    assert len(events) == 1 and events[0]["titles"] == ["New finding"]
    entry["state"] = change
    assert triggers.poll(entry, attention_store=feed)[0] == []


# ---- the runner ---------------------------------------------------------------------------------------------


class Tracker:
    def __init__(self):
        self.events = []

    def on_event(self, e):
        self.events.append(e)


def runner_for(store, registry, engine, clock):
    return schedules.SchedulerRunner(registry, Tracker(), store=store, now_fn=clock, tick=1.0, engine=engine)


def test_the_runner_fires_a_due_v2_automation_once_and_a_reminder_then_switches_off(store, registry, engine, clock):
    daily = add(store, {"type": "reminder", "text": "Stand up"}, spec={"kind": "daily", "time": "09:00"})
    once = store.add_v2("Bank", {"type": "time"}, {"type": "reminder", "text": "Call"},
                        spec={"kind": "once", "when": "2026-11-17T09:30"})
    runner = runner_for(store, registry, engine, clock)
    store_created = NOW - timedelta(hours=2)
    entries = store._load()  # created earlier today, so the 09:00 and 09:30 moments have passed
    for e in entries:
        e["created_at"] = store_created.isoformat(timespec="seconds")
    store._save(entries)
    runner._tick_once()
    runner._tick_once()  # a second tick must not fire them again
    assert len(engine.history.for_automation(daily["id"])) == 1
    assert len(engine.history.for_automation(once["id"])) == 1
    assert store.get(once["id"])["enabled"] is False and store.get(daily["id"])["enabled"] is True


def test_the_runner_polls_event_triggers_and_a_paused_automation_does_nothing(store, registry, engine, clock, tmp_path):
    watch = tmp_path / "w"
    watch.mkdir()
    trig = triggers.clean_trigger({"type": "file_appears", "folder": str(watch), "pattern": "*"})
    e = store.add_v2("Watcher", trig, {"type": "reminder", "text": "new file"})
    runner = runner_for(store, registry, engine, clock)
    runner._tick_once()  # baseline
    (watch / "new.txt").write_text("x")
    runner._tick_once()
    runs_ = engine.history.for_automation(e["id"])
    assert len(runs_) == 1 and runs_[0]["trigger"] == "When a file appears in w"
    store.set_enabled(e["id"], False)
    (watch / "again.txt").write_text("x")
    runner._tick_once()
    assert len(engine.history.for_automation(e["id"])) == 1


def test_one_broken_automation_does_not_stop_the_others(store, registry, engine, clock):
    add(store, {"type": "tool", "tool": "nope", "arguments": {}}, spec={"kind": "interval", "interval_seconds": 60}, name="Broken")
    fine = add(store, {"type": "reminder", "text": "x"}, spec={"kind": "interval", "interval_seconds": 60}, name="Fine")
    runner_for(store, registry, engine, clock)._tick_once()
    assert engine.history.for_automation(fine["id"])[0]["status"] == "ok"


def test_v1_schedules_run_exactly_as_before(store, registry, engine, clock, calls):
    store.add("ok_tool", {"v": 1}, {"kind": "interval", "interval_seconds": 60}, "every 1 minutes")
    runner_for(store, registry, engine, clock)._tick_once()
    assert calls == [{"v": 1}] and engine.history.for_automation("sched-001") == []


# ---- the API ------------------------------------------------------------------------------------------------


@pytest.fixture
def server(store, engine, registry):
    runner = SimpleNamespace(_engine=lambda: engine)
    return SimpleNamespace(scheduler_runner=runner, registry=registry)


def post(server, handler, **body):
    return handler(Request(server=server, body=body))


def test_preview_writes_nothing_and_create_needs_an_explicit_approve(server, store):
    status, out = post(server, api.preview, sentence="every weekday at 8 check my security")
    card = out["card"]
    assert card["trigger"] == "Every weekday at 08:00" and card["action_type"] == "prompt"
    assert "Anything that needs confirmation waits for you" in card["tools"]
    assert card["caps"] == runs.DEFAULT_CAPS and store.list() == []
    with pytest.raises(ApiError, match="approve"):
        post(server, api.create, sentence="every weekday at 8 check my security")
    assert store.list() == []
    status, out = post(server, api.create, sentence="every weekday at 8 check my security", approve=True)
    assert out["automation"]["id"] == "sched-001" and out["automation"]["trigger"] == "Every weekday at 08:00"
    assert store.get("sched-001")["action"]["owner"] == "orchestrator"


def test_templates_create_the_five_starting_points(server, store, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Downloads").mkdir()
    status, out = api.listing(Request(server=server))
    assert [t["id"] for t in out["templates"]] == ["morning_brief", "daily_security", "downloads_tidy", "weekly_research", "bills_deadlines"]
    assert out["mail_trigger"] is False
    for t in runs.TEMPLATES:
        if t["id"] == "morning_brief":
            continue  # its tool lives in the real roster, not this stub registry
        status, made = post(server, api.create, template=t["id"], approve=True)
        assert made["automation"]["template"] == t["id"]
    assert len(store.list()) == 4
    with pytest.raises(ApiError, match="no tool called 'daily_digest'"):
        post(server, api.create, template="morning_brief", approve=True)


def test_a_reminder_sentence_and_bad_input_are_handled_in_plain_words(server, store):
    status, out = post(server, api.create, sentence="remind me in 20 minutes to stretch", approve=True)
    assert out["automation"]["action"] == "Remind: stretch" and out["automation"]["trigger"].startswith("Once on")
    for body, msg in [
        ({"sentence": "whenever"}, "Accepted shapes"),
        ({"sentence": "daily at 8"}, "say what to do"),
        ({"trigger": {"type": "mail_match"}, "sentence": "daily at 8 x"}, "3.1"),
        ({"sentence": "daily at 8 x", "caps": {"max_runs_per_day": 0}}, "at least 1"),
        ({"sentence": "daily at 8", "action": {"type": "tool", "tool": "never_tool"}}, "never allowed"),
        ({"sentence": "daily at 8", "action": {"type": "prompt", "prompt": "x", "owner": "nobody"}}, "no agent"),
        ({"template": "zzz"}, "no such template"),
    ]:
        with pytest.raises(ApiError, match=msg):
            post(server, api.preview, **body)


def test_run_now_pause_resume_remove_and_history(server, store, engine):
    post(server, api.create, sentence="daily at 8 x", action={"type": "reminder", "text": "Hi"}, approve=True)
    status, out = post(server, api.run_now, id="sched-001")
    assert out["run"]["word"] == "Done"
    status, out = api.detail(Request(server=server, query={"id": ["sched-001"]}))
    assert [h["status"] for h in out["history"]] == ["ok"] and out["automation"]["result"] == "Done"
    post(server, api.pause, id="sched-001")
    assert store.get("sched-001")["enabled"] is False
    post(server, api.resume, id="sched-001")
    assert store.get("sched-001")["enabled"] is True
    post(server, api.remove, id="sched-001")
    assert store.list() == [] and engine.history.for_automation("sched-001") == []
    with pytest.raises(ApiError) as e:
        post(server, api.run_now, id="sched-001")
    assert e.value.status == 404
    with pytest.raises(ApiError):
        post(server, api.run_now, id="../etc")


def test_parked_steps_are_approved_or_declined_through_the_api(server, store, calls):
    post(server, api.create, sentence="daily at 8 x", action={"type": "tool", "tool": "gated_tool", "arguments": {"a": 1}}, approve=True)
    run_id = post(server, api.run_now, id="sched-001")[1]["run"]["id"]
    status, out = api.detail(Request(server=server, query={"id": ["sched-001"]}))
    assert [p["id"] for p in out["parked"]] == [run_id] and calls == []
    status, out = post(server, api.approve, run=run_id)
    assert out["run"]["status"] == "ok" and calls == [{"a": 1}]
    with pytest.raises(ApiError) as e:
        post(server, api.approve, run=run_id)
    assert e.value.status == 404


def test_the_list_gives_the_screen_what_it_shows(server, store):
    post(server, api.create, sentence="daily at 8 x", action={"type": "reminder", "text": "Hi"}, name="Hello", approve=True)
    old = store.add("ok_tool", {}, {"kind": "interval", "interval_seconds": 3600}, "every 1 hours")
    status, out = api.listing(Request(server=server))
    by_id = {r["id"]: r for r in out["automations"]}
    assert by_id["sched-001"]["name"] == "Hello" and by_id["sched-001"]["trigger"] == "Daily at 08:00" and by_id["sched-001"]["next_run"]
    assert by_id[old["id"]]["version"] == 1 and by_id[old["id"]]["name"] == "Ok tool"


def test_automation_routes_are_owner_only():
    from dourmouse import request_guard

    for path in ("/api/os/automations/create", "/api/os/automations/run", "/api/os/automations/approve"):
        assert request_guard.is_owner_route("POST", path)
