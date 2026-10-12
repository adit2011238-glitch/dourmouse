"""FIX-W2 H-FD1-6: one damaged JSON cell in the goals database must not make every read of the goal,
its tasks or its audit trail raise. A damaged cell reads as a safe value, never as one that skips a rule."""
from __future__ import annotations

import pytest

from dourmouse.goals import GoalStore


@pytest.fixture()
def store():
    s = GoalStore(None)
    goal = s.create_goal("ship it", success_criteria=["tests pass"])
    a = s.create_task(goal["id"], "first")
    b = s.create_task(goal["id"], "second", depends_on=[a["id"]])
    s.log_event(goal["id"], "note", {"k": "v"})
    return s, goal, a, b


def _poke(s, sql, *params):
    with s._lock, s._connect() as c:
        c.execute(sql, params)


def test_a_damaged_event_detail_does_not_break_the_snapshot_or_the_export(store):
    s, goal, a, b = store
    _poke(s, "UPDATE goal_events SET detail=? WHERE type='note'", "{not json")
    events = s.goal_events(goal["id"])
    bad = [e for e in events if e["type"] == "note"][0]
    assert bad["detail"] == {"unreadable": "{not json"}
    assert any(e["id"] != bad["id"] for e in events)  # the other events are still there
    assert s.all_events()
    assert isinstance(s.export_events_markdown(goal["id"]), str)


def test_a_damaged_depends_on_does_not_unblock_the_task(store):
    s, goal, a, b = store
    _poke(s, "UPDATE tasks SET depends_on=? WHERE id=?", "garbage", b["id"])
    task = s.get_task(b["id"])
    assert task["depends_on"] and task["depends_on"] != []  # not read as "no dependencies"
    s.update_goal_status(goal["id"], "EXECUTING")
    ready = [t["id"] for t in s.ready_tasks(goal["id"])]
    assert b["id"] not in ready


def test_damaged_criteria_do_not_read_as_no_criteria(store):
    s, goal, a, b = store
    _poke(s, "UPDATE goals SET success_criteria=? WHERE id=?", "[oops", goal["id"])
    g = s.get_goal(goal["id"])
    assert g["success_criteria"] and "unreadable" in g["success_criteria"][0].lower()


def test_damaged_result_and_limits_read_as_safe_values(store):
    s, goal, a, b = store
    _poke(s, "UPDATE goals SET result=?, resource_limits=? WHERE id=?", "}{", "}{", goal["id"])
    _poke(s, "UPDATE tasks SET result=? WHERE id=?", "}{", a["id"])
    g = s.get_goal(goal["id"])
    assert g["result"] is None and g["resource_limits"] == {}
    assert s.get_task(a["id"])["result"] is None
    assert s.list_tasks(goal["id"])  # listing still works


def test_good_cells_are_unchanged(store):
    s, goal, a, b = store
    assert s.get_goal(goal["id"])["success_criteria"] == ["tests pass"]
    assert s.get_task(b["id"])["depends_on"] == [a["id"]]
