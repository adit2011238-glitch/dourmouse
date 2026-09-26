"""PAUSE and RESUME on the goal store (2026-09-26, OS shell GOALS screen).

The risk a naive pause has: ``update_goal_status`` is an unconditional
overwrite, so a task already in flight would finish and write EXECUTING or
COMPLETED over PAUSED. These tests pin that a pause survives that write, that
resume restores the exact previous status, and that pausing survives a restart.
"""

from __future__ import annotations

import pytest

from dourmouse.goal_runtime import GoalRuntime  # noqa: F401  (import must keep working)
from dourmouse.goals import GoalStore


@pytest.fixture()
def store():
    return GoalStore(None)


def _goal(store, status="EXECUTING"):
    g = store.create_goal("pause me")
    store.create_task(g["id"], "one")
    store.update_goal_status(g["id"], status)
    return g["id"]


@pytest.mark.parametrize("status", ["READY", "EXECUTING", "VERIFYING", "BLOCKED", "WAITING_FOR_APPROVAL", "RECOVERING"])
def test_resume_restores_the_exact_previous_status(store, status):
    gid = _goal(store, status)
    if status in ("BLOCKED", "WAITING_FOR_APPROVAL"):
        store.update_goal_status(gid, status, blocked_reason="needs a person")
    assert store.pause_goal(gid) is True
    paused = store.get_goal(gid)
    assert paused["status"] == "PAUSED" and paused["paused_from"] == status
    assert store.resume_goal(gid) is True
    back = store.get_goal(gid)
    assert back["status"] == status and back["paused_from"] is None
    if status in ("BLOCKED", "WAITING_FOR_APPROVAL"):
        assert back["blocked_reason"] == "needs a person"


def test_a_paused_goal_is_not_advanced_by_the_worker(store):
    gid = _goal(store)
    store.pause_goal(gid)
    assert gid not in [g["id"] for g in store.active_goals()]
    store.resume_goal(gid)
    assert gid in [g["id"] for g in store.active_goals()]


def test_a_write_from_a_task_in_flight_does_not_undo_the_pause(store):
    gid = _goal(store)
    store.pause_goal(gid)
    store.update_goal_status(gid, "EXECUTING")  # the worker finishing a task
    store.update_goal_status(gid, "WAITING_FOR_APPROVAL", blocked_reason="gated")
    now = store.get_goal(gid)
    assert now["status"] == "PAUSED"
    assert store.resume_goal(gid) is True
    after = store.get_goal(gid)
    assert after["status"] == "WAITING_FOR_APPROVAL" and after["blocked_reason"] == "gated"


def test_a_terminal_write_while_paused_is_kept(store):
    gid = _goal(store)
    store.pause_goal(gid)
    store.update_goal_status(gid, "COMPLETED", result={"summary": "done"})
    done = store.get_goal(gid)
    assert done["status"] == "COMPLETED" and done["paused_from"] is None
    assert store.resume_goal(gid) is False


def test_pause_refuses_terminal_paused_and_unknown_goals(store):
    gid = _goal(store)
    assert store.pause_goal(gid) is True
    assert store.pause_goal(gid) is False  # already paused
    assert store.pause_goal("goal_nope") is False
    store.cancel_goal(gid)
    assert store.get_goal(gid)["status"] == "CANCELLED"
    assert store.get_goal(gid)["paused_from"] is None
    assert store.pause_goal(gid) is False


def test_resume_refuses_a_goal_that_is_not_paused(store):
    gid = _goal(store)
    assert store.resume_goal(gid) is False
    assert store.resume_goal("goal_nope") is False
    assert store.get_goal(gid)["status"] == "EXECUTING"


def test_pause_and_resume_are_in_the_audit_trail(store):
    gid = _goal(store)
    store.pause_goal(gid)
    store.resume_goal(gid)
    details = [e["detail"] for e in store.goal_events(gid) if e["type"] == "goal_status_changed"]
    assert any(d.get("status") == "PAUSED" and d.get("paused_from") == "EXECUTING" for d in details)
    assert any(d.get("resumed") and d.get("status") == "EXECUTING" for d in details)


def test_pause_survives_a_restart_and_resume_still_restores(tmp_path):
    path = tmp_path / "goals.db"
    first = GoalStore(path)
    gid = _goal(first, "EXECUTING")
    first.pause_goal(gid)
    first.close()
    second = GoalStore(path)
    assert second.get_goal(gid)["status"] == "PAUSED"
    assert second.resume_goal(gid) is True
    assert second.get_goal(gid)["status"] == "EXECUTING"


def test_an_old_database_without_the_column_is_migrated(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE goals (id TEXT PRIMARY KEY, owner TEXT NOT NULL DEFAULT '*', objective TEXT NOT NULL,"
        " status TEXT NOT NULL, priority TEXT NOT NULL DEFAULT 'normal', success_criteria TEXT NOT NULL DEFAULT '[]',"
        " resource_limits TEXT NOT NULL DEFAULT '{}', deadline TEXT, current_task_id TEXT, blocked_reason TEXT,"
        " result TEXT, session_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    con.execute("INSERT INTO goals (id, objective, status, created_at, updated_at) VALUES ('g1','old','EXECUTING','t','t')")
    con.commit()
    con.close()
    store = GoalStore(path)
    assert store.get_goal("g1")["paused_from"] is None
    assert store.pause_goal("g1") and store.resume_goal("g1")
    assert store.get_goal("g1")["status"] == "EXECUTING"


def test_a_worker_tick_stops_after_the_task_in_flight_when_paused(store):
    """_advance_goal re-checks the status before each task: paused means no further task starts."""
    from dourmouse.goal_runtime import GoalRuntime

    g = store.create_goal("two steps")
    store.create_task(g["id"], "a")
    store.create_task(g["id"], "b")
    store.update_goal_status(g["id"], "EXECUTING")
    rt = GoalRuntime.__new__(GoalRuntime)
    rt._store = store
    ran = []

    def fake_run(goal, task):
        ran.append(task["description"])
        store.update_task_status(task["id"], "COMPLETED", result={"final_text": "x", "verified": True})
        store.pause_goal(goal["id"])  # the owner presses PAUSE while step a is running
        store.update_goal_status(goal["id"], "EXECUTING")  # ...and the worker writes on

    rt._run_task = fake_run
    rt._advance_goal(store.get_goal(g["id"]))
    assert ran == ["a"]
    assert store.get_goal(g["id"])["status"] == "PAUSED"


def test_create_goal_with_steps_chains_the_steps_and_starts_executing(store):
    g = store.create_goal_with_steps("ship it", ["draft", "  ", "review"], priority="high")
    tasks = store.list_tasks(g["id"])
    assert g["status"] == "EXECUTING" and g["priority"] == "high"
    assert [t["description"] for t in tasks] == ["draft", "review"]
    assert tasks[0]["depends_on"] == [] and tasks[1]["depends_on"] == [tasks[0]["id"]]
    assert tasks[0]["status"] == "READY" and tasks[1]["status"] == "PENDING"


def test_create_goal_with_no_steps_uses_the_objective_as_the_one_task(store):
    g = store.create_goal_with_steps("just do this")
    assert [t["description"] for t in store.list_tasks(g["id"])] == ["just do this"]


def test_create_goal_with_steps_refuses_an_empty_objective(store):
    with pytest.raises(ValueError):
        store.create_goal_with_steps("  ", ["a"])
