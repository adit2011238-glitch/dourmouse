"""dourmouse/goals.py — the persistent Goal/Task store backing the
autonomous agent runtime. Hermetic: every test uses an in-memory store
(``GoalStore(None)``), matching ``state_store.StateStore``'s own test
convention, except the restart tests, which deliberately use a real
temp file since surviving a restart is the entire point of this module.
"""

from __future__ import annotations

import pytest

from dourmouse.goals import GoalStore


@pytest.fixture()
def store():
    return GoalStore(None)


class TestGoalCreation:
    def test_create_goal_returns_a_real_row(self, store):
        goal = store.create_goal("Research competitors and draft a report")
        assert goal["status"] == "CREATED"
        assert goal["objective"] == "Research competitors and draft a report"
        assert goal["priority"] == "normal"
        assert goal["owner"] == "*"

    def test_empty_objective_is_refused(self, store):
        with pytest.raises(ValueError):
            store.create_goal("   ")

    def test_unknown_priority_is_refused(self, store):
        with pytest.raises(ValueError):
            store.create_goal("do something", priority="urgent-ish")

    def test_get_goal_round_trips(self, store):
        created = store.create_goal("Monitor a folder")
        fetched = store.get_goal(created["id"])
        assert fetched == created

    def test_get_missing_goal_is_none(self, store):
        assert store.get_goal("goal_doesnotexist") is None

    def test_creating_a_goal_logs_an_event(self, store):
        goal = store.create_goal("Ship the report")
        events = store.goal_events(goal["id"])
        assert len(events) == 1
        assert events[0]["type"] == "goal_created"


class TestGoalLifecycle:
    def test_status_transition_is_persisted_and_logged(self, store):
        goal = store.create_goal("Do a thing")
        assert store.update_goal_status(goal["id"], "EXECUTING") is True
        assert store.get_goal(goal["id"])["status"] == "EXECUTING"
        types = [e["type"] for e in store.goal_events(goal["id"])]
        assert types == ["goal_created", "goal_status_changed"]

    def test_unknown_status_is_refused(self, store):
        goal = store.create_goal("Do a thing")
        with pytest.raises(ValueError):
            store.update_goal_status(goal["id"], "FROBNICATING")

    def test_blocked_reason_is_stored_honestly(self, store):
        goal = store.create_goal("Do a thing")
        store.update_goal_status(goal["id"], "BLOCKED", blocked_reason="needs a password")
        assert store.get_goal(goal["id"])["blocked_reason"] == "needs a password"

    def test_active_goals_only_returns_states_the_worker_should_advance(self, store):
        running = store.create_goal("Running")
        store.update_goal_status(running["id"], "EXECUTING")
        done = store.create_goal("Done")
        store.update_goal_status(done["id"], "COMPLETED")
        blocked = store.create_goal("Blocked")
        store.update_goal_status(blocked["id"], "BLOCKED")
        active_ids = {g["id"] for g in store.active_goals()}
        assert active_ids == {running["id"]}

    def test_active_goals_ordered_by_priority_then_age(self, store):
        low = store.create_goal("Low", priority="low")
        store.update_goal_status(low["id"], "EXECUTING")
        critical = store.create_goal("Critical", priority="critical")
        store.update_goal_status(critical["id"], "EXECUTING")
        ordered = [g["id"] for g in store.active_goals()]
        assert ordered[0] == critical["id"]
        assert ordered[1] == low["id"]

    def test_cancel_goal_marks_terminal_and_cancels_open_tasks(self, store):
        goal = store.create_goal("Cancel me")
        store.update_goal_status(goal["id"], "EXECUTING")
        task = store.create_task(goal["id"], "step one")
        assert store.cancel_goal(goal["id"]) is True
        assert store.get_goal(goal["id"])["status"] == "CANCELLED"
        assert store.get_task(task["id"])["status"] == "CANCELLED"

    def test_cancel_goal_leaves_a_completed_task_alone(self, store):
        goal = store.create_goal("Cancel me")
        task = store.create_task(goal["id"], "already done")
        store.update_task_status(task["id"], "COMPLETED")
        store.cancel_goal(goal["id"])
        assert store.get_task(task["id"])["status"] == "COMPLETED"

    def test_cancel_is_a_no_op_on_an_already_terminal_goal(self, store):
        goal = store.create_goal("Finish")
        store.update_goal_status(goal["id"], "COMPLETED")
        assert store.cancel_goal(goal["id"]) is False
        assert store.get_goal(goal["id"])["status"] == "COMPLETED"


class TestResolveTaskApproval:
    """Acceptance test 7, the resumable per-task approval ticket -- see
    docs/ENGINEERING_AUDIT.md finding #029. Before this, the only way
    past a WAITING_FOR_APPROVAL task was the global DOURMOUSE_AUTO_APPROVE
    toggle."""

    def _waiting_task(self, store):
        goal = store.create_goal("Do the gated thing")
        task = store.create_task(goal["id"], "delete an important file")
        store.update_task_status(task["id"], "WAITING_FOR_APPROVAL", error="a gated action needs approval")
        store.update_goal_status(goal["id"], "WAITING_FOR_APPROVAL", blocked_reason="needs approval")
        return goal, task

    def test_approving_resumes_the_task_and_the_goal(self, store):
        goal, task = self._waiting_task(store)
        assert store.resolve_task_approval(task["id"], True) is True
        assert store.get_task(task["id"])["status"] == "READY"
        assert store.get_goal(goal["id"])["status"] == "EXECUTING"

    def test_approving_writes_a_one_time_ticket_into_the_task_result(self, store):
        _, task = self._waiting_task(store)
        store.resolve_task_approval(task["id"], True)
        assert store.get_task(task["id"])["result"] == {"approved_for_next_run": True, "approved_prompts": []}

    def test_the_ticket_names_the_exact_actions_the_human_was_shown(self, store):
        """Finding #137: not a blanket approval for whatever the model does next."""
        _, task = self._waiting_task(store)
        store.update_task_status(
            task["id"], "WAITING_FOR_APPROVAL", result={"pending_prompts": ["Delete a.txt?", "Send the summary?"]},
        )
        store.resolve_task_approval(task["id"], True)
        assert store.get_task(task["id"])["result"] == {
            "approved_for_next_run": True, "approved_prompts": ["Delete a.txt?", "Send the summary?"],
        }

    def test_declining_fails_the_task_and_blocks_the_goal_with_the_real_reason(self, store):
        goal, task = self._waiting_task(store)
        assert store.resolve_task_approval(task["id"], False, reason="too risky") is True
        updated_task = store.get_task(task["id"])
        assert updated_task["status"] == "FAILED"
        assert updated_task["last_error"] == "too risky"
        updated_goal = store.get_goal(goal["id"])
        assert updated_goal["status"] == "BLOCKED"
        assert "too risky" in updated_goal["blocked_reason"]

    def test_declining_with_no_reason_is_still_honest_not_a_blank(self, store):
        _, task = self._waiting_task(store)
        store.resolve_task_approval(task["id"], False)
        assert store.get_task(task["id"])["last_error"] == "declined by human reviewer"

    def test_a_task_not_actually_waiting_cannot_be_resolved(self, store):
        goal = store.create_goal("Normal goal")
        task = store.create_task(goal["id"], "ordinary task")  # status READY, never waited
        assert store.resolve_task_approval(task["id"], True) is False
        assert store.get_task(task["id"])["status"] == "READY"

    def test_unknown_task_id_returns_false(self, store):
        assert store.resolve_task_approval("no-such-task", True) is False

    def test_a_real_event_is_logged_to_the_audit_trail(self, store):
        goal, task = self._waiting_task(store)
        store.resolve_task_approval(task["id"], True, reason="looks safe")
        events = store.goal_events(goal["id"])
        resolved = [e for e in events if e["type"] == "approval_resolved"]
        assert len(resolved) == 1
        assert resolved[0]["task_id"] == task["id"]
        assert resolved[0]["detail"] == {"approved": True, "reason": "looks safe"}


class TestTaskDependencyGraph:
    def test_task_with_no_dependencies_starts_ready(self, store):
        goal = store.create_goal("A goal")
        task = store.create_task(goal["id"], "first step")
        assert task["status"] == "READY"

    def test_task_with_dependencies_starts_pending(self, store):
        goal = store.create_goal("A goal")
        first = store.create_task(goal["id"], "first")
        second = store.create_task(goal["id"], "second", depends_on=[first["id"]])
        assert second["status"] == "PENDING"

    def test_creating_a_task_under_a_missing_goal_is_refused(self, store):
        with pytest.raises(ValueError):
            store.create_task("goal_doesnotexist", "step")

    def test_ready_tasks_excludes_a_task_whose_dependency_is_incomplete(self, store):
        goal = store.create_goal("A goal")
        first = store.create_task(goal["id"], "first")
        store.create_task(goal["id"], "second", depends_on=[first["id"]])
        ready_ids = {t["id"] for t in store.ready_tasks(goal["id"])}
        assert ready_ids == {first["id"]}

    def test_ready_tasks_promotes_pending_to_ready_once_dependency_completes(self, store):
        goal = store.create_goal("A goal")
        first = store.create_task(goal["id"], "first")
        second = store.create_task(goal["id"], "second", depends_on=[first["id"]])
        store.update_task_status(first["id"], "COMPLETED")
        ready = store.ready_tasks(goal["id"])
        assert [t["id"] for t in ready] == [second["id"]]
        assert store.get_task(second["id"])["status"] == "READY"

    def test_independent_tasks_are_both_ready_at_once(self, store):
        goal = store.create_goal("A goal")
        a = store.create_task(goal["id"], "branch a")
        b = store.create_task(goal["id"], "branch b")
        ready_ids = {t["id"] for t in store.ready_tasks(goal["id"])}
        assert ready_ids == {a["id"], b["id"]}

    def test_task_status_transition_increments_attempt_count_only_when_asked(self, store):
        goal = store.create_goal("A goal")
        task = store.create_task(goal["id"], "flaky step")
        store.update_task_status(task["id"], "RUNNING")
        assert store.get_task(task["id"])["attempt_count"] == 0
        store.update_task_status(task["id"], "RETRYING", error="timed out", increment_attempt=True)
        updated = store.get_task(task["id"])
        assert updated["attempt_count"] == 1
        assert updated["last_error"] == "timed out"

    def test_unknown_task_status_is_refused(self, store):
        goal = store.create_goal("A goal")
        task = store.create_task(goal["id"], "step")
        with pytest.raises(ValueError):
            store.update_task_status(task["id"], "VIBING")

    def test_updating_a_missing_task_returns_false_not_a_crash(self, store):
        assert store.update_task_status("task_doesnotexist", "COMPLETED") is False


class TestEventsAndSnapshot:
    def test_log_event_rejects_unknown_type(self, store):
        goal = store.create_goal("A goal")
        with pytest.raises(ValueError):
            store.log_event(goal["id"], "made_up_event_type")

    def test_goal_snapshot_bundles_tasks_and_events(self, store):
        goal = store.create_goal("A goal")
        store.create_task(goal["id"], "only step")
        snap = store.goal_snapshot(goal["id"])
        assert len(snap["tasks"]) == 1
        assert any(e["type"] == "task_created" for e in snap["events"])

    def test_goal_snapshot_of_missing_goal_is_none(self, store):
        assert store.goal_snapshot("goal_doesnotexist") is None

    def test_events_are_ordered_oldest_first_for_a_readable_timeline(self, store):
        goal = store.create_goal("A goal")
        store.update_goal_status(goal["id"], "PLANNING")
        store.update_goal_status(goal["id"], "EXECUTING")
        events = store.goal_events(goal["id"])
        assert [e["type"] for e in events] == ["goal_created", "goal_status_changed", "goal_status_changed"]


class TestCrossGoalAuditTrail:
    """2026-09-18: goal_events() only ever answers "what happened on ONE
    goal" -- the founding spec's own audit-trail requirement ("the user
    should be able to inspect what the assistant actually did") is a
    global question. all_events()/export_events_markdown() are the same
    real data, a second query shape over it."""

    def test_all_events_spans_every_goal_newest_first(self, store):
        first = store.create_goal("First goal")
        second = store.create_goal("Second goal")
        events = store.all_events()
        goal_ids = [e["goal_id"] for e in events]
        assert first["id"] in goal_ids
        assert second["id"] in goal_ids
        # newest first: the second goal's creation event comes before the first's
        assert goal_ids.index(second["id"]) < goal_ids.index(first["id"])

    def test_all_events_since_excludes_earlier_entries(self, store):
        from datetime import datetime, timezone
        from time import sleep

        store.create_goal("Old goal")
        sleep(0.02)
        cutoff = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        sleep(0.02)
        recent = store.create_goal("Recent goal")
        events = store.all_events(since=cutoff)
        assert all(e["goal_id"] == recent["id"] for e in events)
        assert len(events) >= 1

    def test_all_events_limit_is_bounded(self, store):
        goal = store.create_goal("A goal")
        for i in range(10):
            store.create_task(goal["id"], f"step {i}")
        events = store.all_events(limit=3)
        assert len(events) == 3

    def test_export_markdown_is_real_readable_text_not_json_dump(self, store):
        goal = store.create_goal("Research competitors")
        store.create_task(goal["id"], "gather pricing")
        report = store.export_events_markdown(goal_id=goal["id"])
        assert "# Dourmouse Audit Trail" in report
        assert goal["id"] in report
        assert "goal_created" in report
        assert "task_created" in report
        # no em dash or decorative separator in generated product text
        assert "—" not in report
        assert " // " not in report

    def test_export_markdown_global_scope_covers_every_goal(self, store):
        store.create_goal("First goal")
        store.create_goal("Second goal")
        report = store.export_events_markdown()
        assert "Scope: All goals" in report
        assert report.count("goal_created") == 2

    def test_export_markdown_empty_scope_is_honest_not_fabricated(self, store):
        report = store.export_events_markdown(goal_id="goal_doesnotexist")
        assert "Entries: 0" in report


class TestRestartSurvival:
    """The entire reason this module exists: a goal's state must be
    readable by a FRESH GoalStore instance pointed at the same file,
    exactly as a real process restart would do it."""

    def test_a_goal_survives_reopening_the_same_file(self, tmp_path):
        db_path = tmp_path / "goals.db"
        store1 = GoalStore(db_path)
        goal = store1.create_goal("Survive a restart", priority="high")
        task = store1.create_task(goal["id"], "step one")
        store1.update_goal_status(goal["id"], "EXECUTING")
        store1.update_task_status(task["id"], "RUNNING")
        store1.close()

        store2 = GoalStore(db_path)
        reloaded = store2.get_goal(goal["id"])
        assert reloaded["status"] == "EXECUTING"
        assert reloaded["priority"] == "high"
        reloaded_task = store2.get_task(task["id"])
        assert reloaded_task["status"] == "RUNNING"
        store2.close()

    def test_the_dependency_graph_survives_a_restart(self, tmp_path):
        db_path = tmp_path / "goals.db"
        store1 = GoalStore(db_path)
        goal = store1.create_goal("Multi-step")
        first = store1.create_task(goal["id"], "first")
        store1.create_task(goal["id"], "second", depends_on=[first["id"]])
        store1.update_task_status(first["id"], "COMPLETED")
        store1.close()

        store2 = GoalStore(db_path)
        ready = store2.ready_tasks(goal["id"])
        assert len(ready) == 1
        assert ready[0]["description"] == "second"
        store2.close()

    def test_closed_store_refuses_further_operations(self, tmp_path):
        store = GoalStore(tmp_path / "goals.db")
        store.close()
        with pytest.raises(RuntimeError):
            store.create_goal("too late")
