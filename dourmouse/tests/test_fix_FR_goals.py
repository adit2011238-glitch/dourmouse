"""FR fixes P3-24, P3-25, P3-26, P3-27 (goal_runtime.py, goal_tools.py)."""

from __future__ import annotations

import pytest

from dourmouse import goal_tools
from dourmouse.goal_runtime import GoalRuntime
from dourmouse.goals import GoalStore, get_goal_store, set_goal_store


def _runtime(store):
    return GoalRuntime(store, registry=object(), tick_seconds=1000.0)


def _tool(name):
    return next(t for t in goal_tools.build_goals_subagent().tools if t.name == name)


@pytest.fixture()
def tools_store():
    set_goal_store(GoalStore(None))
    yield get_goal_store()
    set_goal_store(None)


# ---- P3-24 / P3-25 ---------------------------------------------------------

def test_a_task_left_verifying_by_a_crash_is_recovered():
    store = GoalStore(None)
    goal = store.create_goal("g")
    task = store.create_task(goal["id"], "last step", max_attempts=3)
    store.update_goal_status(goal["id"], "EXECUTING")
    store.update_task_status(task["id"], "RUNNING", increment_attempt=True)
    store.update_task_status(task["id"], "VERIFYING")
    _runtime(store)._recover_orphaned_tasks()
    assert store.get_task(task["id"])["status"] == "RETRYING"


def test_a_verifying_task_is_in_flight_not_a_reason_to_block_the_goal():
    store = GoalStore(None)
    goal = store.create_goal("g")
    a = store.create_task(goal["id"], "a")
    store.create_task(goal["id"], "b", depends_on=[a["id"]])
    store.update_goal_status(goal["id"], "EXECUTING")
    store.update_task_status(a["id"], "VERIFYING")
    _runtime(store)._advance_goal(store.get_goal(goal["id"]))
    assert store.get_goal(goal["id"])["status"] != "BLOCKED"


def test_recovery_does_not_spend_a_second_attempt():
    store = GoalStore(None)
    goal = store.create_goal("g")
    task = store.create_task(goal["id"], "x", max_attempts=3)
    store.update_goal_status(goal["id"], "EXECUTING")
    store.update_task_status(task["id"], "RUNNING", increment_attempt=True)  # what _run_task does
    assert store.get_task(task["id"])["attempt_count"] == 1
    _runtime(store)._recover_orphaned_tasks()
    assert store.get_task(task["id"])["attempt_count"] == 1  # the interrupted run is the one counted


# ---- P3-26 -----------------------------------------------------------------

def test_create_goal_does_not_promise_concurrency():
    text = _tool("create_goal").description
    assert "run concurrently" not in text and "one after another" in text


# ---- P3-27 -----------------------------------------------------------------

def test_unknown_depends_on_id_is_refused(tools_store):
    result = _tool("create_goal").handler({
        "objective": "o", "tasks": [{"description": "a", "depends_on": ["task-typo"]}],
    })
    assert result.startswith("ERROR") and "not a task of this goal" in result


def test_add_tasks_accepts_this_goals_ids_but_not_another_goals(tools_store):
    first = _tool("create_goal").handler({"objective": "o1", "tasks": [{"description": "a"}]})
    other = _tool("create_goal").handler({"objective": "o2", "tasks": [{"description": "b"}]})
    g1, g2 = first.split()[2], other.split()[2]
    t1 = tools_store.list_tasks(g1)[0]["id"]
    t2 = tools_store.list_tasks(g2)[0]["id"]
    assert _tool("add_tasks").handler({"goal_id": g1, "tasks": [{"description": "c", "depends_on": [t1]}]}).startswith("ADDED")
    assert _tool("add_tasks").handler({"goal_id": g1, "tasks": [{"description": "d", "depends_on": [t2]}]}).startswith("ERROR")


def test_success_criteria_must_be_a_list_of_strings(tools_store):
    for bad in ("the email was sent", [1, 2], ["ok", " "]):
        result = _tool("create_goal").handler({"objective": "o", "tasks": [{"description": "a"}], "success_criteria": bad})
        assert result.startswith("ERROR") and "list of non-empty strings" in result, bad
    assert tools_store.list_goals() == [] if hasattr(tools_store, "list_goals") else True
    ok = _tool("create_goal").handler({"objective": "o", "tasks": [{"description": "a"}], "success_criteria": ["email sent"]})
    assert ok.startswith("GOAL CREATED")
