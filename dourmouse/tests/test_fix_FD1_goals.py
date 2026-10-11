"""Fix agent FD1: P5-21 (goal_events keeps the oldest N) and P5-22 (terminal states can be overwritten)."""

from __future__ import annotations

import pytest

from dourmouse.goals import GoalStore


@pytest.fixture()
def store():
    return GoalStore(None)


# P5-21 ---------------------------------------------------------------------- #

def test_goal_events_returns_the_newest_events_oldest_first(store):
    g = store.create_goal("long goal")
    for i in range(30):
        store._log_event(g["id"], None, "tick", {"n": i})
    events = store.goal_events(g["id"], limit=10)
    ns = [e["detail"].get("n") for e in events if e["type"] == "tick"]
    assert ns == list(range(20, 30)), ns  # the last ten, in chronological order
    assert [e["id"] for e in events] == sorted(e["id"] for e in events)


def test_snapshot_shows_the_current_state_of_a_long_goal(store):
    g = store.create_goal("long goal")
    for i in range(250):
        store._log_event(g["id"], None, "tick", {"n": i})
    store.update_goal_status(g["id"], "BLOCKED", blocked_reason="stuck now")
    snap = store.goal_snapshot(g["id"])
    assert snap["events"][-1]["type"] == "goal_status_changed"
    assert snap["events"][-1]["detail"]["status"] == "BLOCKED"


# P5-22 ---------------------------------------------------------------------- #

@pytest.mark.parametrize("terminal", ["CANCELLED", "EXPIRED"])
@pytest.mark.parametrize("late", ["COMPLETED", "EXECUTING", "BLOCKED", "VERIFYING"])
def test_a_stale_write_cannot_resurrect_a_cancelled_or_expired_goal(store, terminal, late):
    g = store.create_goal("x")
    store.update_goal_status(g["id"], "EXECUTING")
    if terminal == "CANCELLED":
        assert store.cancel_goal(g["id"]) is True
    else:
        store.update_goal_status(g["id"], "EXPIRED")
    assert store.update_goal_status(g["id"], late) is False
    assert store.get_goal(g["id"])["status"] == terminal


def test_a_stale_completed_write_cannot_overwrite_failed(store):
    g = store.create_goal("x")
    store.update_goal_status(g["id"], "FAILED")
    assert store.update_goal_status(g["id"], "COMPLETED") is False
    assert store.get_goal(g["id"])["status"] == "FAILED"


@pytest.mark.parametrize("done", ["COMPLETED", "FAILED"])
def test_add_tasks_can_still_reopen_a_completed_or_failed_goal(store, done):
    g = store.create_goal("x")
    store.update_goal_status(g["id"], done)
    assert store.update_goal_status(g["id"], "EXECUTING") is True
    assert store.get_goal(g["id"])["status"] == "EXECUTING"


def test_ordinary_transitions_are_unchanged(store):
    g = store.create_goal("x")
    for s in ("PLANNING", "EXECUTING", "VERIFYING", "COMPLETED"):
        assert store.update_goal_status(g["id"], s) is True
