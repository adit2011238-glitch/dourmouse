"""The ``goals`` subagent — tools that create and control persistent,
background-executing Goal/Task graphs (docs/GODSPEED_ROADMAP.md Phase 2).

Mirrors ``system_access.build_system_subagent()``'s shape: one dedicated
module, one ``build_goals_subagent()`` factory returning a real
``Subagent``, imported and registered by ``general_roster.py`` rather
than adding more bulk to that already-oversized file (a concrete
"god file" the engineering audit flagged — see docs/UI_SOURCE_MAP.md
and docs/ARCHITECTURE.md for the pattern this deliberately avoids
repeating).

Planning happens the same way ``delegate_parallel``'s branches already
do: the calling model decomposes the objective into a task list itself,
as part of this ONE tool call, using its own full reasoning — there is
no separate hidden "planner LLM" call. A task's ``depends_on`` entries
are either a real task id already in the goal, or a local reference
``"#N"`` meaning "the Nth task in THIS SAME call's task list" (0-indexed,
must point to an earlier index — no forward or self references), so a
single call can describe an entire dependency graph before any task id
exists yet.
"""

from __future__ import annotations

from typing import Any

from dourmouse.dispatch import Subagent, ToolSpec
from dourmouse.goals import GOAL_STATES, get_goal_store

_TASK_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string", "description": "What this task must accomplish, in enough detail to run standalone."},
        "depends_on": {
            "type": "array",
            "items": {"type": "string"},
            "default": [],
            "description": "Real task ids already in this goal, or '#N' for the Nth task in THIS list (0-indexed, must be an earlier index).",
        },
        "assigned_agent": {"type": "string", "default": "", "description": "Optional: force this task to run against one specific registered agent."},
    },
    "required": ["description"],
}


def _resolve_depends_on(raw: list[str], created_ids: list[str], index: int) -> list[str] | str:
    """Returns the resolved real-id list, or an error string."""
    resolved: list[str] = []
    for dep in raw or []:
        dep = str(dep)
        if dep.startswith("#"):
            try:
                local_idx = int(dep[1:])
            except ValueError:
                return f"ERROR: {dep!r} is not a valid local reference (expected '#N')."
            if local_idx < 0 or local_idx >= index:
                return f"ERROR: task {index} depends_on {dep!r}, which must reference an EARLIER task in this same list."
            resolved.append(created_ids[local_idx])
        else:
            resolved.append(dep)
    return resolved


def _create_goal(arguments: dict[str, Any]) -> str:
    objective = str(arguments.get("objective") or "").strip()
    if not objective:
        return "ERROR: create_goal requires a non-empty 'objective'."
    tasks_arg = arguments.get("tasks")
    if not isinstance(tasks_arg, list) or not tasks_arg:
        return "ERROR: create_goal requires a non-empty 'tasks' list of {description, depends_on} objects."
    store = get_goal_store()
    try:
        goal = store.create_goal(
            objective,
            priority=str(arguments.get("priority") or "normal"),
            success_criteria=arguments.get("success_criteria") or None,
        )
    except ValueError as exc:
        return f"ERROR: {exc}"

    created_ids: list[str] = []
    for i, item in enumerate(tasks_arg):
        if not isinstance(item, dict):
            return f"ERROR: task {i} must be an object with at least 'description' (goal {goal['id']} left with {i} task(s) already created)."
        description = str(item.get("description") or "").strip()
        if not description:
            return f"ERROR: task {i} is missing a non-empty 'description' (goal {goal['id']} left with {i} task(s) already created)."
        depends_on = _resolve_depends_on(item.get("depends_on") or [], created_ids, i)
        if isinstance(depends_on, str):  # an error message
            return f"{depends_on} (goal {goal['id']} left with {i} task(s) already created)."
        agent = str(item.get("assigned_agent") or "").strip() or None
        task = store.create_task(goal["id"], description, depends_on=depends_on, assigned_agent=agent)
        created_ids.append(task["id"])

    store.update_goal_status(goal["id"], "EXECUTING")
    return (
        f"GOAL CREATED: {goal['id']} ({len(created_ids)} task(s), priority={goal['priority']}) — "
        "now executing in the background. Use get_goal_status to check progress; the user does "
        "not need to keep this conversation open for it to continue."
    )


def _add_tasks(arguments: dict[str, Any]) -> str:
    goal_id = str(arguments.get("goal_id") or "").strip()
    if not goal_id:
        return "ERROR: add_tasks requires a non-empty 'goal_id'."
    store = get_goal_store()
    goal = store.get_goal(goal_id)
    if goal is None:
        return f"ERROR: no goal {goal_id!r}."
    tasks_arg = arguments.get("tasks")
    if not isinstance(tasks_arg, list) or not tasks_arg:
        return "ERROR: add_tasks requires a non-empty 'tasks' list of {description, depends_on} objects."
    created_ids: list[str] = []
    for i, item in enumerate(tasks_arg):
        if not isinstance(item, dict):
            return f"ERROR: task {i} must be an object with at least 'description' ({i} task(s) already added)."
        description = str(item.get("description") or "").strip()
        if not description:
            return f"ERROR: task {i} is missing a non-empty 'description' ({i} task(s) already added)."
        depends_on = _resolve_depends_on(item.get("depends_on") or [], created_ids, i)
        if isinstance(depends_on, str):
            return f"{depends_on} ({i} task(s) already added)."
        agent = str(item.get("assigned_agent") or "").strip() or None
        task = store.create_task(goal_id, description, depends_on=depends_on, assigned_agent=agent)
        created_ids.append(task["id"])
    if goal["status"] in ("BLOCKED", "COMPLETED", "FAILED"):
        store.update_goal_status(goal_id, "EXECUTING")
    return f"ADDED {len(created_ids)} task(s) to goal {goal_id}."


def _format_goal_snapshot(snap: dict[str, Any]) -> str:
    lines = [
        f"GOAL {snap['id']} [{snap['status']}] priority={snap['priority']}",
        f"Objective: {snap['objective']}",
    ]
    if snap.get("blocked_reason"):
        lines.append(f"Blocked reason: {snap['blocked_reason']}")
    lines.append(f"Tasks ({len(snap['tasks'])}):")
    for t in snap["tasks"]:
        marker = "*" if t["id"] == snap.get("current_task_id") else "-"
        lines.append(f"  {marker} [{t['status']}] {t['description']} (attempts {t['attempt_count']}/{t['max_attempts']})")
        if t.get("last_error"):
            lines.append(f"      last error: {t['last_error']}")
    recent = snap["events"][-10:]
    if recent:
        lines.append("Recent activity:")
        for e in recent:
            lines.append(f"  {e['at']} {e['type']}: {e['detail']}")
    return "\n".join(lines)


def _get_goal_status(arguments: dict[str, Any]) -> str:
    goal_id = str(arguments.get("goal_id") or "").strip()
    if not goal_id:
        return "ERROR: get_goal_status requires a non-empty 'goal_id'."
    snap = get_goal_store().goal_snapshot(goal_id)
    if snap is None:
        return f"ERROR: no goal {goal_id!r}."
    return _format_goal_snapshot(snap)


def _list_goals(arguments: dict[str, Any]) -> str:
    status = str(arguments.get("status") or "").strip().upper() or None
    if status is not None and status not in GOAL_STATES:
        return f"ERROR: unknown status {status!r} (allowed: {sorted(GOAL_STATES)})."
    goals = get_goal_store().list_goals(status=status)
    if not goals:
        return "No goals" + (f" with status {status}" if status else "") + "."
    lines = [f"{len(goals)} goal(s):"]
    for g in goals:
        lines.append(f"  {g['id']} [{g['status']}] {g['objective']}")
    return "\n".join(lines)


def _cancel_goal(arguments: dict[str, Any]) -> str:
    goal_id = str(arguments.get("goal_id") or "").strip()
    if not goal_id:
        return "ERROR: cancel_goal requires a non-empty 'goal_id'."
    if get_goal_store().cancel_goal(goal_id):
        return f"CANCELLED: {goal_id}"
    goal = get_goal_store().get_goal(goal_id)
    if goal is None:
        return f"ERROR: no goal {goal_id!r}."
    return f"NOT CANCELLED: {goal_id} is already {goal['status']} (terminal)."


def build_goals_subagent() -> Subagent:
    return Subagent(
        name="goals",
        domain="Both",
        description=(
            "Create and control persistent, autonomous background goals — a multi-step "
            "task graph that keeps executing after this conversation ends, independent of "
            "the chat connection. Use create_goal when the user asks for multi-step "
            "background work (\"research X and report back\", \"monitor this and notify me\") "
            "rather than trying to do everything inline in one turn."
        ),
        tools=(
            ToolSpec(
                name="create_goal",
                description=(
                    "Create a persistent background goal with its own task graph, then start "
                    "executing it immediately — it keeps running even after this turn ends. "
                    "Decompose the objective into 'tasks' yourself: a list of "
                    "{description, depends_on, assigned_agent} objects. depends_on entries are "
                    "either a real task id, or '#N' meaning the Nth task in this same list "
                    "(0-indexed, must reference an earlier index). Independent tasks (no "
                    "shared depends_on) run concurrently. Use this for real multi-step or "
                    "long-running work the user should not have to babysit turn by turn — not "
                    "for something answerable in this one turn."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "objective": {"type": "string"},
                        "tasks": {"type": "array", "items": _TASK_ITEM_SCHEMA},
                        "priority": {"type": "string", "enum": ["critical", "high", "normal", "low", "background"], "default": "normal"},
                        "success_criteria": {"type": "array", "items": {"type": "string"}, "default": []},
                    },
                    "required": ["objective", "tasks"],
                },
                handler=_create_goal,
            ),
            ToolSpec(
                name="add_tasks",
                description=(
                    "Add more tasks to an existing goal — the real mechanism for replanning "
                    "when a goal turns out to need more work than originally planned (e.g. "
                    "after inspecting get_goal_status and finding a gap). Same task-object "
                    "shape as create_goal."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "goal_id": {"type": "string"},
                        "tasks": {"type": "array", "items": _TASK_ITEM_SCHEMA},
                    },
                    "required": ["goal_id", "tasks"],
                },
                handler=_add_tasks,
            ),
            ToolSpec(
                name="get_goal_status",
                description="Inspect one goal: its status, every task's state, and its recent real activity log.",
                parameters={"type": "object", "properties": {"goal_id": {"type": "string"}}, "required": ["goal_id"]},
                handler=_get_goal_status,
            ),
            ToolSpec(
                name="list_goals",
                description="List goals, optionally filtered by status.",
                parameters={"type": "object", "properties": {"status": {"type": "string", "default": ""}}},
                handler=_list_goals,
            ),
            ToolSpec(
                name="cancel_goal",
                description="Stop a goal and cancel its remaining tasks. Does not undo work already completed.",
                parameters={"type": "object", "properties": {"goal_id": {"type": "string"}}, "required": ["goal_id"]},
                handler=_cancel_goal,
            ),
        ),
    )
