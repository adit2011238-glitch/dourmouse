"""dourmouse/goal_runtime.py — the persistent background worker that
executes a Goal's task graph. Hermetic: ``chat.ChatSession`` is replaced
with a scripted fake keyed by task description, so no real model/tool
call ever happens. The store is always an in-memory ``GoalStore(None)``.
"""

from __future__ import annotations

import dourmouse.chat as chat_module
from dourmouse.goal_runtime import GoalRuntime, goal_runtime_enabled
from dourmouse.goals import GoalStore


class _FakeSession:
    """Records every ``ask()`` call and returns a scripted response keyed
    by the exact prompt text (== the task description). A response is
    either a plain dict (returned as-is) or a callable producing one, so
    a test can simulate raising on the first attempt and succeeding on a
    retry."""

    calls: list[tuple[str, str | None]] = []
    responses: dict[str, object] = {}

    def __init__(self, registry, session_file=None, confirmation_gate=None):
        self.registry = registry
        self.confirmation_gate = confirmation_gate

    def ask(self, prompt, event_sink=None, forced_agent=None, force_plain_dispatch=False):
        type(self).calls.append((prompt, forced_agent))
        scripted = type(self).responses.get(prompt, {"final_text": "done"})
        if callable(scripted):
            scripted = scripted()
        if event_sink is not None:
            for event in scripted.get("events", []):
                event_sink(event)
        if scripted.get("raises"):
            raise RuntimeError(scripted["raises"])
        return {"final_text": scripted.get("final_text", ""), "transcript": [], "messages": []}


def _install_fake(monkeypatch, responses: dict[str, object]):
    _FakeSession.calls = []
    _FakeSession.responses = responses
    monkeypatch.setattr(chat_module, "ChatSession", _FakeSession)


def _runtime(store: GoalStore) -> GoalRuntime:
    return GoalRuntime(store, registry=object(), tick_seconds=1000.0)


class TestSingleTaskGoal:
    def test_a_successful_task_completes_its_goal(self, monkeypatch):
        _install_fake(monkeypatch, {"do the one thing": {"final_text": "it is done"}})
        store = GoalStore(None)
        goal = store.create_goal("A simple goal")
        store.create_task(goal["id"], "do the one thing")
        store.update_goal_status(goal["id"], "EXECUTING")

        _runtime(store).tick()

        updated = store.get_goal(goal["id"])
        assert updated["status"] == "COMPLETED"
        assert "it is done" in updated["result"]["summary"]

    def test_an_empty_final_response_is_treated_as_a_failure_not_a_success(self, monkeypatch):
        _install_fake(monkeypatch, {"say nothing": {"final_text": "   "}})
        store = GoalStore(None)
        goal = store.create_goal("A goal")
        task = store.create_task(goal["id"], "say nothing", max_attempts=1)
        store.update_goal_status(goal["id"], "EXECUTING")

        _runtime(store).tick()

        assert store.get_task(task["id"])["status"] == "FAILED"
        assert store.get_goal(goal["id"])["status"] == "BLOCKED"


class TestDependencyOrdering:
    def test_a_dependent_task_only_runs_after_its_dependency_completes(self, monkeypatch):
        _install_fake(monkeypatch, {
            "first step": {"final_text": "first done"},
            "second step": {"final_text": "second done"},
        })
        store = GoalStore(None)
        goal = store.create_goal("Two steps")
        first = store.create_task(goal["id"], "first step")
        store.create_task(goal["id"], "second step", depends_on=[first["id"]])
        store.update_goal_status(goal["id"], "EXECUTING")
        runtime = _runtime(store)

        runtime.tick()
        assert [c[0] for c in _FakeSession.calls] == ["first step"]
        assert store.get_goal(goal["id"])["status"] == "EXECUTING"

        runtime.tick()
        assert [c[0] for c in _FakeSession.calls] == ["first step", "second step"]
        assert store.get_goal(goal["id"])["status"] == "COMPLETED"

    def test_independent_branches_both_run_in_the_same_tick(self, monkeypatch):
        _install_fake(monkeypatch, {"branch a": {"final_text": "a"}, "branch b": {"final_text": "b"}})
        store = GoalStore(None)
        goal = store.create_goal("Two branches")
        store.create_task(goal["id"], "branch a")
        store.create_task(goal["id"], "branch b")
        store.update_goal_status(goal["id"], "EXECUTING")

        _runtime(store).tick()

        assert {c[0] for c in _FakeSession.calls} == {"branch a", "branch b"}
        assert store.get_goal(goal["id"])["status"] == "COMPLETED"

    def test_forced_agent_is_threaded_through_to_the_session(self, monkeypatch):
        _install_fake(monkeypatch, {"pick an agent": {"final_text": "ok"}})
        store = GoalStore(None)
        goal = store.create_goal("Assigned")
        store.create_task(goal["id"], "pick an agent", assigned_agent="dev_coding")
        store.update_goal_status(goal["id"], "EXECUTING")

        _runtime(store).tick()

        assert _FakeSession.calls == [("pick an agent", "dev_coding")]


class TestFailureAndRecovery:
    def test_a_failing_task_retries_before_giving_up(self, monkeypatch):
        attempts = {"n": 0}

        def flaky():
            attempts["n"] += 1
            if attempts["n"] < 2:
                return {"raises": "temporary failure"}
            return {"final_text": "recovered"}

        _install_fake(monkeypatch, {"flaky step": flaky})
        store = GoalStore(None)
        goal = store.create_goal("Retry me")
        task = store.create_task(goal["id"], "flaky step", max_attempts=3)
        store.update_goal_status(goal["id"], "EXECUTING")
        runtime = _runtime(store)

        runtime.tick()
        assert store.get_task(task["id"])["status"] == "RETRYING"
        assert store.get_goal(goal["id"])["status"] == "EXECUTING"

        runtime.tick()
        assert store.get_task(task["id"])["status"] == "COMPLETED"
        assert store.get_goal(goal["id"])["status"] == "COMPLETED"

    def test_exhausting_attempts_blocks_the_goal_with_an_honest_reason(self, monkeypatch):
        _install_fake(monkeypatch, {"always fails": {"raises": "boom"}})
        store = GoalStore(None)
        goal = store.create_goal("Doomed")
        task = store.create_task(goal["id"], "always fails", max_attempts=2)
        store.update_goal_status(goal["id"], "EXECUTING")
        runtime = _runtime(store)

        runtime.tick()
        assert store.get_task(task["id"])["status"] == "RETRYING"
        runtime.tick()
        assert store.get_task(task["id"])["status"] == "FAILED"
        runtime.tick()  # the tick that notices the permanent failure and blocks the goal

        updated = store.get_goal(goal["id"])
        assert updated["status"] == "BLOCKED"
        assert "boom" in updated["blocked_reason"]

    def test_a_task_found_running_at_startup_is_never_assumed_still_running(self, monkeypatch):
        _install_fake(monkeypatch, {})
        store = GoalStore(None)
        goal = store.create_goal("Interrupted")
        task = store.create_task(goal["id"], "was mid-flight", max_attempts=3)
        store.update_goal_status(goal["id"], "EXECUTING")
        store.update_task_status(task["id"], "RUNNING")

        runtime = _runtime(store)
        runtime._recover_orphaned_tasks()

        updated = store.get_task(task["id"])
        assert updated["status"] == "RETRYING"
        events = [e["type"] for e in store.goal_events(goal["id"])]
        assert "recovery_attempted" in events

    def test_orphan_recovery_fails_the_task_when_no_attempts_remain(self, monkeypatch):
        _install_fake(monkeypatch, {})
        store = GoalStore(None)
        goal = store.create_goal("Interrupted, out of retries")
        task = store.create_task(goal["id"], "was mid-flight", max_attempts=1)
        store.update_goal_status(goal["id"], "EXECUTING")
        store.update_task_status(task["id"], "RUNNING", increment_attempt=True)

        _runtime(store)._recover_orphaned_tasks()

        assert store.get_task(task["id"])["status"] == "FAILED"


class TestApprovalGating:
    def test_a_declined_gated_tool_call_waits_for_approval_instead_of_completing(self, monkeypatch):
        _install_fake(monkeypatch, {
            "delete everything": {
                "final_text": "I tried but was declined",
                "events": [{"type": "tool_result", "name": "delete_path", "text": "DECLINED BY USER: delete /important"}],
            }
        })
        store = GoalStore(None)
        goal = store.create_goal("Risky goal")
        task = store.create_task(goal["id"], "delete everything")
        store.update_goal_status(goal["id"], "EXECUTING")

        _runtime(store).tick()

        assert store.get_task(task["id"])["status"] == "WAITING_FOR_APPROVAL"
        assert store.get_goal(goal["id"])["status"] == "WAITING_FOR_APPROVAL"


class TestGoalControl:
    def test_a_cancelled_goal_is_never_advanced(self, monkeypatch):
        _install_fake(monkeypatch, {"should not run": {"final_text": "ran anyway"}})
        store = GoalStore(None)
        goal = store.create_goal("Cancel before it starts")
        store.create_task(goal["id"], "should not run")
        store.cancel_goal(goal["id"])

        _runtime(store).tick()

        assert _FakeSession.calls == []
        assert store.get_goal(goal["id"])["status"] == "CANCELLED"

    def test_cancelling_mid_flight_is_never_clobbered_back_to_completed(self, monkeypatch):
        """Real, narrow race: cancel_goal() can run on another thread
        while a task's own dispatch call is still in flight (a real chat
        turn can take many seconds). Once that call finally returns, the
        task must stay CANCELLED, not get silently overwritten back to
        COMPLETED just because its now-moot result showed up late."""
        store = GoalStore(None)
        goal = store.create_goal("Cancel while running")
        task = store.create_task(goal["id"], "slow task")
        store.update_goal_status(goal["id"], "EXECUTING")

        def slow_response():
            # Simulates another thread calling cancel_goal() while this
            # task's own "dispatch call" is still in flight.
            store.cancel_goal(goal["id"])
            return {"final_text": "finished after being cancelled"}

        _install_fake(monkeypatch, {"slow task": slow_response})

        _runtime(store).tick()

        assert store.get_task(task["id"])["status"] == "CANCELLED"
        assert store.get_goal(goal["id"])["status"] == "CANCELLED"

    def test_a_goal_with_no_tasks_yet_is_left_alone(self, monkeypatch):
        _install_fake(monkeypatch, {})
        store = GoalStore(None)
        goal = store.create_goal("Not planned yet")
        store.update_goal_status(goal["id"], "EXECUTING")

        _runtime(store).tick()

        assert store.get_goal(goal["id"])["status"] == "EXECUTING"

    def test_a_goal_blocked_on_an_unreachable_dependency_is_marked_blocked(self, monkeypatch):
        _install_fake(monkeypatch, {})
        store = GoalStore(None)
        goal = store.create_goal("Bad graph")
        stuck = store.create_task(goal["id"], "never satisfied", depends_on=["task_doesnotexist"])
        store.update_goal_status(goal["id"], "EXECUTING")

        _runtime(store).tick()

        assert store.get_task(stuck["id"])["status"] == "PENDING"
        assert store.get_goal(goal["id"])["status"] == "BLOCKED"


class TestNotifications:
    def test_completion_posts_to_the_bus_and_raises_a_real_alert(self, monkeypatch):
        _install_fake(monkeypatch, {"one step": {"final_text": "done"}})
        store = GoalStore(None)
        goal = store.create_goal("Notify me")
        store.create_task(goal["id"], "one step")
        store.update_goal_status(goal["id"], "EXECUTING")

        bus_calls = []
        alert_calls = []
        broadcast_calls = []

        class FakeBus:
            def post(self, **kwargs):
                bus_calls.append(kwargs)

        class FakeStateStore:
            def add_alert(self, **kwargs):
                alert_calls.append(kwargs)

        class FakeBroadcast:
            def broadcast(self, payload):
                broadcast_calls.append(payload)

        runtime = GoalRuntime(
            store, registry=object(), tick_seconds=1000.0,
            bus=FakeBus(), state_store=FakeStateStore(), events_broadcast=FakeBroadcast(),
        )
        runtime.tick()

        assert len(bus_calls) == 1
        assert "Notify me" in bus_calls[0]["body"]
        assert len(alert_calls) == 1
        assert broadcast_calls == [{"type": "state_change", "section": "alerts", "owner": "*"}]

    def test_a_broken_notifier_never_breaks_the_runtime(self, monkeypatch):
        _install_fake(monkeypatch, {"one step": {"final_text": "done"}})
        store = GoalStore(None)
        goal = store.create_goal("Notify me")
        store.create_task(goal["id"], "one step")
        store.update_goal_status(goal["id"], "EXECUTING")

        class BrokenBus:
            def post(self, **kwargs):
                raise RuntimeError("bus is down")

        runtime = GoalRuntime(store, registry=object(), tick_seconds=1000.0, bus=BrokenBus())
        runtime.tick()  # must not raise

        assert store.get_goal(goal["id"])["status"] == "COMPLETED"


class TestEnvGate:
    def test_enabled_by_default(self, monkeypatch):
        """2026-09-18: flipped from opt-in to opt-out -- see
        goal_runtime_enabled's own docstring for the real bug this
        closes (create_goal was unconditionally registered and callable
        with the old off-by-default flag, so a "successful" tool call
        could silently do nothing forever)."""
        monkeypatch.delenv("DOURMOUSE_GOAL_RUNTIME", raising=False)
        assert goal_runtime_enabled() is True

    def test_only_the_literal_value_zero_opts_out(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_GOAL_RUNTIME", "0")
        assert goal_runtime_enabled() is False
        monkeypatch.setenv("DOURMOUSE_GOAL_RUNTIME", "1")
        assert goal_runtime_enabled() is True
        monkeypatch.setenv("DOURMOUSE_GOAL_RUNTIME", "false")
        assert goal_runtime_enabled() is True  # only "0" opts out, not any falsy-looking string
