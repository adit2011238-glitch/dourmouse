"""FR fixes P5-46 .. P5-49 in schedules.py."""

from __future__ import annotations

import threading
from datetime import datetime

import pytest

from dourmouse import schedules as sch
from dourmouse.schedules import SchedulerRunner, Schedules, parse_schedule

SPEC = {"kind": "daily", "time": "09:00", "weekday": None, "interval_seconds": None}


def _store(tmp_path):
    return Schedules(tmp_path / "schedules.jsonl")


def test_ids_stay_unique_after_a_removal(tmp_path):
    s = _store(tmp_path)
    for _ in range(3):
        s.add("t", {}, SPEC, "daily")
    assert s.remove("sched-002")
    s.add("t", {}, SPEC, "daily")
    ids = [e["id"] for e in s.list()]
    assert ids == ["sched-001", "sched-003", "sched-004"]
    assert len(set(ids)) == len(ids)


def test_mark_run_and_remove_touch_only_their_own_entry(tmp_path):
    s = _store(tmp_path)
    for _ in range(3):
        s.add("t", {}, SPEC, "daily")
    s.remove("sched-002")
    new = s.add("t", {}, SPEC, "daily")
    s.mark_run(new["id"], at=datetime(2026, 1, 1, 9, 0))
    runs = {e["id"]: e["last_run"] for e in s.list()}
    assert runs["sched-001"] is None and runs[new["id"]] is not None


def test_concurrent_adds_do_not_lose_entries_and_no_temp_file_is_left(tmp_path):
    s = _store(tmp_path)
    errors = []

    def worker():
        try:
            for _ in range(15):
                s.add("t", {}, SPEC, "daily")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    ids = [e["id"] for e in s.list()]
    assert len(ids) == 90 and len(set(ids)) == 90
    assert [p.name for p in tmp_path.iterdir()] == ["schedules.jsonl"]


def test_save_is_atomic_a_failed_write_keeps_the_old_file(tmp_path, monkeypatch):
    s = _store(tmp_path)
    s.add("t", {}, SPEC, "daily")
    before = (tmp_path / "schedules.jsonl").read_text()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(sch.os, "replace", boom)
    with pytest.raises(OSError):
        s.add("t", {}, SPEC, "daily")
    assert (tmp_path / "schedules.jsonl").read_text() == before


def test_a_tick_that_raises_does_not_kill_the_runner_thread(tmp_path):
    calls = {"n": 0}

    class Flaky(Schedules):
        def list(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("transient read error")
            return super().list()

    runner = SchedulerRunner(None, None, store=Flaky(tmp_path / "s.jsonl"), tick=1.0)
    runner._tick = 0.01
    runner.start()
    try:
        deadline = 200
        import time

        while calls["n"] < 3 and deadline:
            time.sleep(0.01)
            deadline -= 1
        assert calls["n"] >= 3
        assert runner.running
    finally:
        runner.stop()


@pytest.mark.parametrize("phrase,weekday,time", [
    ("every mondays", 0, "09:00"), ("every Fridays at 2pm", 4, "14:00"),
    ("every Monday", 0, "09:00"), ("every sat at 10:00", 5, "10:00"),
    ("every tues", 1, "09:00"), ("every sundays at 8:15", 6, "08:15"),
])
def test_every_weekday_with_a_plural_s_is_accepted(phrase, weekday, time):
    spec = parse_schedule(phrase)
    assert spec["kind"] == "weekday" and spec["weekday"] == weekday and spec["time"] == time


def test_a_non_weekday_is_still_rejected():
    with pytest.raises(ValueError):
        parse_schedule("every blursdays")
