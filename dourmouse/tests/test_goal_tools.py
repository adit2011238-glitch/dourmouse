"""The ``goals`` subagent's tools (dourmouse/goal_tools.py). Hermetic:
the process-wide goal-store singleton is swapped for a fresh in-memory
one before every test and reset after, same isolation pattern this
codebase already uses for message_bus.set_message_bus(None).
"""

from __future__ import annotations

import pytest

from dourmouse import goal_tools
from dourmouse.goals import GoalStore, get_goal_store, set_goal_store


@pytest.fixture(autouse=True)
def _isolated_goal_store():
    set_goal_store(GoalStore(None))
    yield
    set_goal_store(None)


def _tool(name: str):
    subagent = goal_tools.build_goals_subagent()
    for tool in subagent.tools:
        if tool.name == name:
            return tool
    raise AssertionError(f"no tool named {name!r} on the goals subagent")


class TestBuildGoalsSubagent:
    def test_registers_all_five_tools(self):
        subagent = goal_tools.build_goals_subagent()
        names = {t.name for t in subagent.tools}
        assert names == {"create_goal", "add_tasks", "get_goal_status", "list_goals", "cancel_goal"}

    def test_none_of_the_goal_tools_require_confirmation(self):
        """Creating/inspecting/cancelling a goal is bookkeeping — the
        RISK lives in whatever gated tool a task calls once it actually
        runs, not in creating the goal record itself."""
        from dourmouse.dispatch import Permission

        subagent = goal_tools.build_goals_subagent()
        assert all(t.permission == Permission.REGULAR for t in subagent.tools)


class TestCreateGoal:
    def test_creates_a_goal_and_starts_it_executing(self):
        result = _tool("create_goal").handler({
            "objective": "Research competitors",
            "tasks": [{"description": "find five competitors"}],
        })
        assert "GOAL CREATED" in result
        goal_id = result.split()[2]
        goal = get_goal_store().get_goal(goal_id)
        assert goal["status"] == "EXECUTING"
        assert len(get_goal_store().list_tasks(goal_id)) == 1

    def test_empty_objective_is_a_clean_error_not_a_crash(self):
        result = _tool("create_goal").handler({"objective": "  ", "tasks": [{"description": "x"}]})
        assert result.startswith("ERROR:")

    def test_empty_tasks_list_is_refused(self):
        result = _tool("create_goal").handler({"objective": "Do a thing", "tasks": []})
        assert result.startswith("ERROR:")

    def test_a_task_missing_a_description_is_refused_and_names_how_many_survived(self):
        result = _tool("create_goal").handler({
            "objective": "Do a thing",
            "tasks": [{"description": "first"}, {"description": "  "}],
        })
        assert result.startswith("ERROR:")
        assert "1 task(s) already created" in result

    def test_local_reference_resolves_to_a_real_dependency(self):
        result = _tool("create_goal").handler({
            "objective": "Two steps",
            "tasks": [
                {"description": "first step"},
                {"description": "second step", "depends_on": ["#0"]},
            ],
        })
        goal_id = result.split()[2]
        tasks = get_goal_store().list_tasks(goal_id)
        first, second = tasks[0], tasks[1]
        assert second["depends_on"] == [first["id"]]
        assert first["status"] == "READY"
        assert second["status"] == "PENDING"

    def test_a_forward_reference_is_refused(self):
        result = _tool("create_goal").handler({
            "objective": "Bad order",
            "tasks": [
                {"description": "first step", "depends_on": ["#1"]},
                {"description": "second step"},
            ],
        })
        assert result.startswith("ERROR:")
        assert "EARLIER task" in result

    def test_a_self_reference_is_refused(self):
        result = _tool("create_goal").handler({
            "objective": "Bad order",
            "tasks": [{"description": "first step", "depends_on": ["#0"]}],
        })
        assert result.startswith("ERROR:")

    def test_assigned_agent_is_persisted_on_the_task(self):
        result = _tool("create_goal").handler({
            "objective": "Assigned",
            "tasks": [{"description": "code review", "assigned_agent": "dev_coding"}],
        })
        goal_id = result.split()[2]
        task = get_goal_store().list_tasks(goal_id)[0]
        assert task["assigned_agent"] == "dev_coding"

    def test_unknown_priority_is_a_clean_error(self):
        result = _tool("create_goal").handler({
            "objective": "Bad priority",
            "tasks": [{"description": "x"}],
            "priority": "super-urgent",
        })
        assert result.startswith("ERROR:")


class TestAddTasks:
    def test_add_tasks_appends_to_an_existing_goal(self):
        created = _tool("create_goal").handler({"objective": "Base", "tasks": [{"description": "first"}]})
        goal_id = created.split()[2]
        result = _tool("add_tasks").handler({"goal_id": goal_id, "tasks": [{"description": "second"}]})
        assert "ADDED 1 task(s)" in result
        assert len(get_goal_store().list_tasks(goal_id)) == 2

    def test_add_tasks_to_a_missing_goal_is_a_clean_error(self):
        result = _tool("add_tasks").handler({"goal_id": "goal_doesnotexist", "tasks": [{"description": "x"}]})
        assert result.startswith("ERROR:")

    def test_add_tasks_can_depend_on_a_real_existing_task_id(self):
        created = _tool("create_goal").handler({"objective": "Base", "tasks": [{"description": "first"}]})
        goal_id = created.split()[2]
        first_id = get_goal_store().list_tasks(goal_id)[0]["id"]
        _tool("add_tasks").handler({"goal_id": goal_id, "tasks": [{"description": "second", "depends_on": [first_id]}]})
        second = get_goal_store().list_tasks(goal_id)[1]
        assert second["depends_on"] == [first_id]

    def test_add_tasks_reopens_a_blocked_goal(self):
        created = _tool("create_goal").handler({"objective": "Base", "tasks": [{"description": "first"}]})
        goal_id = created.split()[2]
        get_goal_store().update_goal_status(goal_id, "BLOCKED", blocked_reason="stuck")
        _tool("add_tasks").handler({"goal_id": goal_id, "tasks": [{"description": "unblock it"}]})
        assert get_goal_store().get_goal(goal_id)["status"] == "EXECUTING"


class TestInspection:
    def test_get_goal_status_reports_shape_and_recent_activity(self):
        created = _tool("create_goal").handler({"objective": "Inspect me", "tasks": [{"description": "one step"}]})
        goal_id = created.split()[2]
        result = _tool("get_goal_status").handler({"goal_id": goal_id})
        assert "Inspect me" in result
        assert "one step" in result
        assert "task_created" in result

    def test_get_goal_status_of_a_missing_goal_is_a_clean_error(self):
        result = _tool("get_goal_status").handler({"goal_id": "goal_doesnotexist"})
        assert result.startswith("ERROR:")

    def test_list_goals_shows_every_goal(self):
        _tool("create_goal").handler({"objective": "One", "tasks": [{"description": "x"}]})
        _tool("create_goal").handler({"objective": "Two", "tasks": [{"description": "y"}]})
        result = _tool("list_goals").handler({})
        assert "One" in result and "Two" in result

    def test_list_goals_filters_by_status(self):
        created = _tool("create_goal").handler({"objective": "Cancel me", "tasks": [{"description": "x"}]})
        goal_id = created.split()[2]
        _tool("cancel_goal").handler({"goal_id": goal_id})
        _tool("create_goal").handler({"objective": "Still going", "tasks": [{"description": "y"}]})
        result = _tool("list_goals").handler({"status": "cancelled"})
        assert "Cancel me" in result
        assert "Still going" not in result

    def test_list_goals_rejects_an_unknown_status(self):
        result = _tool("list_goals").handler({"status": "vibing"})
        assert result.startswith("ERROR:")

    def test_list_goals_with_none_is_an_honest_empty_message(self):
        result = _tool("list_goals").handler({"status": "failed"})
        assert result == "No goals with status FAILED."


class TestCancelGoal:
    def test_cancel_goal_stops_it(self):
        created = _tool("create_goal").handler({"objective": "Stop me", "tasks": [{"description": "x"}]})
        goal_id = created.split()[2]
        result = _tool("cancel_goal").handler({"goal_id": goal_id})
        assert result == f"CANCELLED: {goal_id}"
        assert get_goal_store().get_goal(goal_id)["status"] == "CANCELLED"

    def test_cancel_a_missing_goal_is_a_clean_error(self):
        result = _tool("cancel_goal").handler({"goal_id": "goal_doesnotexist"})
        assert result.startswith("ERROR:")

    def test_cancel_an_already_terminal_goal_says_so_honestly(self):
        created = _tool("create_goal").handler({"objective": "Finish", "tasks": [{"description": "x"}]})
        goal_id = created.split()[2]
        get_goal_store().update_goal_status(goal_id, "COMPLETED")
        result = _tool("cancel_goal").handler({"goal_id": goal_id})
        assert "NOT CANCELLED" in result
        assert "COMPLETED" in result
