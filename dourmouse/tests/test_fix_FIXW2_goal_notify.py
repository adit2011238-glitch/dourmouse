"""FIX-W2 P5-22: the goal runtime does not announce a status it did not write. When update_goal_status
refuses (a cancelled goal, a stale snapshot), no "Done" or "Blocked" notification goes out."""
from __future__ import annotations

from dourmouse.goal_runtime import GoalRuntime
from dourmouse.goals import GoalStore


class Bus:
    def __init__(self):
        self.posts = []

    def post(self, **kw):
        self.posts.append(kw)


def _setup(max_attempts=1):
    store = GoalStore(None)
    bus = Bus()
    runtime = GoalRuntime(store, registry=object(), tick_seconds=1000.0, bus=bus)
    goal = store.create_goal("ship it")
    task = store.create_task(goal["id"], "only step", max_attempts=max_attempts)
    store.update_goal_status(goal["id"], "EXECUTING")
    return store, bus, runtime, goal, task


def test_a_cancelled_goal_is_not_announced_done_by_a_stale_tick():
    store, bus, runtime, goal, task = _setup()
    store.update_task_status(task["id"], "COMPLETED", result={"final_text": "ok"})
    stale_goal = store.get_goal(goal["id"])
    stale_tasks = store.list_tasks(goal["id"])
    store.update_goal_status(goal["id"], "CANCELLED")  # the owner cancels while the tick is mid-flight
    runtime._complete_goal(stale_goal, stale_tasks)
    assert store.get_goal(goal["id"])["status"] == "CANCELLED"
    assert bus.posts == []


def test_a_cancelled_goal_is_not_announced_blocked_by_a_stale_tick():
    store, bus, runtime, goal, task = _setup()
    store.update_task_status(task["id"], "RUNNING", increment_attempt=True)
    store.update_task_status(task["id"], "FAILED", error="boom")
    stale_goal = store.get_goal(goal["id"])
    stale_tasks = store.list_tasks(goal["id"])
    store.update_goal_status(goal["id"], "CANCELLED")
    assert runtime._resolve_if_terminal(stale_goal, stale_tasks) is True
    assert store.get_goal(goal["id"])["status"] == "CANCELLED"
    assert bus.posts == []


def test_a_real_completion_is_still_announced_once():
    store, bus, runtime, goal, task = _setup()
    store.update_task_status(task["id"], "COMPLETED", result={"final_text": "ok"})
    runtime._complete_goal(store.get_goal(goal["id"]), store.list_tasks(goal["id"]))
    assert store.get_goal(goal["id"])["status"] == "COMPLETED"
    assert len(bus.posts) == 1 and "Done" in bus.posts[0]["body"]


def test_a_real_permanent_failure_is_still_announced_blocked():
    store, bus, runtime, goal, task = _setup()
    store.update_task_status(task["id"], "RUNNING", increment_attempt=True)
    store.update_task_status(task["id"], "FAILED", error="boom")
    runtime._resolve_if_terminal(store.get_goal(goal["id"]), store.list_tasks(goal["id"]))
    assert store.get_goal(goal["id"])["status"] == "BLOCKED"
    assert len(bus.posts) == 1 and "Blocked" in bus.posts[0]["body"]


def test_a_cancelled_goal_is_not_announced_as_needing_approval(monkeypatch):
    """Same class, found next to P5-22: the WAITING_FOR_APPROVAL branch of _run_task."""
    store, bus, runtime, goal, task = _setup()

    def fake_dispatch(g, t, approved):
        store.update_goal_status(g["id"], "CANCELLED")  # the owner cancels during the long dispatch call
        return {"blocked_reason": "needs a yes", "pending_prompts": []}

    monkeypatch.setattr(runtime, "_execute_via_dispatch", fake_dispatch)
    runtime._run_task(store.get_goal(goal["id"]), store.get_task(task["id"]))
    assert store.get_goal(goal["id"])["status"] == "CANCELLED"
    assert bus.posts == []
