"""Jobs: goals that run unattended under a budget (plan 1.3, finding 198).

A job is a goal whose ``resource_limits`` carry a ``kind`` (and, for jobs made
here, a ``budget``). No new table: the goals store, its task rows and its audit
trail already are the job's board and log, and the goals worker already runs it.

Two kinds of step exist today:

* kind ``agent``: each step is a chat task run by the owner agent (an agent id
  or an old agent name, see ``agent_profiles.resolve``), pinned through
  ``assigned_agent`` and checked by the existing independent verifier.
* a registered native kind (``research``): steps are ``job:<kind>`` handlers.
  Those jobs bring their own budget, so only ``agent`` jobs are budget-checked
  here.

The budget counts two things the store can count exactly: ``max_runs`` (task
attempts, retries included) and ``max_tool_calls`` (audit events of type
tool_call). Elapsed time is not budgeted: a job parked overnight waiting for
the owner must not run out of time while nobody works on it.
"""

from __future__ import annotations

from typing import Any

from dourmouse.goals import GOAL_TERMINAL_STATES, GoalStore

KIND_AGENT = "agent"
DEFAULT_BUDGET = {"max_runs": 40, "max_tool_calls": 200}
BUDGET_CEILING = {"max_runs": 500, "max_tool_calls": 5000}
MAX_STEPS = 24
MAX_TITLE = 300
MAX_STEP = 600

#: Board columns, in the order the screen shows them.
COLUMNS = (
    ("needs_you", "Needs you", ("WAITING_FOR_APPROVAL", "WAITING_FOR_AUTHENTICATION", "BLOCKED")),
    ("running", "Running", ("CREATED", "PLANNING", "READY", "EXECUTING", "VERIFYING", "RECOVERING", "REPLANNING")),
    ("paused", "Paused", ("PAUSED",)),
    ("done", "Done", ("COMPLETED",)),
    ("stopped", "Stopped", ("FAILED", "CANCELLED", "EXPIRED")),
)


def clean_budget(raw: dict[str, Any] | None, base: dict[str, int] | None = None) -> dict[str, int]:
    """Whole positive numbers up to the ceiling; anything else is refused, never clamped silently."""
    out = dict(base or DEFAULT_BUDGET)
    for key, value in (raw or {}).items():
        if key not in DEFAULT_BUDGET:
            raise ValueError(f"unknown budget item {key!r} (allowed: {', '.join(DEFAULT_BUDGET)})")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value or value < 1:
            raise ValueError(f"{key} must be a whole number of at least 1")
        if value > BUDGET_CEILING[key]:
            raise ValueError(f"{key} is at most {BUDGET_CEILING[key]}")
        out[key] = int(value)
    return out


def job_kind(goal: dict[str, Any]) -> str | None:
    limits = goal.get("resource_limits")
    kind = limits.get("kind") if isinstance(limits, dict) else None
    return str(kind) if kind else None


def is_job(goal: dict[str, Any]) -> bool:
    return job_kind(goal) is not None


def _budget_of(goal: dict[str, Any]) -> dict[str, int] | None:
    """The generic budget, or None for a job that keeps its own (research)."""
    limits = goal.get("resource_limits")
    budget = limits.get("budget") if isinstance(limits, dict) else None
    if isinstance(budget, dict) and any(k in budget for k in DEFAULT_BUDGET):
        return {k: int(budget[k]) for k in DEFAULT_BUDGET if isinstance(budget.get(k), (int, float))}
    return None


def spend(store: GoalStore, goal: dict[str, Any], tasks: list[dict[str, Any]] | None = None) -> dict[str, int]:
    tasks = store.list_tasks(goal["id"]) if tasks is None else tasks
    return {
        "max_runs": sum(int(t.get("attempt_count") or 0) for t in tasks),
        "max_tool_calls": store.count_events(goal["id"], "tool_call"),
    }


def over_budget(store: GoalStore, goal: dict[str, Any], tasks: list[dict[str, Any]] | None = None) -> str | None:
    """The plain reason a job has used its budget up, or None."""
    if job_kind(goal) != KIND_AGENT:
        return None
    budget = _budget_of(goal)
    if budget is None:
        return None
    used = spend(store, goal, tasks)
    labels = {"max_runs": "step runs", "max_tool_calls": "tool calls"}
    for key, limit in budget.items():
        if used[key] >= limit:
            return f"the budget is used up: {used[key]} of {limit} {labels[key]}. Raise it to carry on."
    return None


def create_job(
    title: str, steps: list[str] | None = None, *, owner: str = "orchestrator",
    budget: dict[str, Any] | None = None, priority: str = "normal", store: GoalStore | None = None,
) -> dict[str, Any]:
    """An unattended multi-step job run by one agent. Steps run in the order written; with none, the
    title is the one step (nothing invents steps the owner did not write)."""
    from dourmouse.agent_profiles import resolve
    from dourmouse.goals import get_goal_store

    title = " ".join(str(title or "").split())
    if not title:
        raise ValueError("a job needs a title")
    if len(title) > MAX_TITLE:
        raise ValueError(f"the title is longer than {MAX_TITLE} characters")
    clean = [str(s).strip() for s in (steps or []) if str(s).strip()]
    if len(clean) > MAX_STEPS:
        raise ValueError(f"a job has at most {MAX_STEPS} steps")
    if any(len(s) > MAX_STEP for s in clean):
        raise ValueError(f"a step is longer than {MAX_STEP} characters")
    resolved = resolve(owner or "orchestrator")
    if resolved is None:
        raise ValueError(f"no agent called {owner!r}")
    limits = {"kind": KIND_AGENT, "owner_agent": resolved.agent_id, "budget": clean_budget(budget)}
    store = store or get_goal_store()
    goal = store.create_goal(title, owner=resolved.agent_id, priority=priority, resource_limits=limits)
    previous: str | None = None
    for description in clean or [title]:
        task = store.create_task(goal["id"], description, depends_on=[previous] if previous else None,
                                 assigned_agent=resolved.agent_id)
        previous = task["id"]
    store.update_goal_status(goal["id"], "EXECUTING")
    return store.get_goal(goal["id"])  # type: ignore[return-value]


def set_budget(store: GoalStore, goal_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Change a job's budget. A job parked only because it ran out of budget carries on at once when the
    new budget has room; a job parked for any other reason stays parked."""
    goal = store.get_goal(goal_id)
    if goal is None:
        raise LookupError("no such job")
    if job_kind(goal) != KIND_AGENT or _budget_of(goal) is None:
        raise ValueError("this job keeps its own budget")
    limits = dict(goal["resource_limits"])
    limits["budget"] = clean_budget(raw, base=_budget_of(goal))
    store.set_resource_limits(goal_id, limits)
    goal = store.get_goal(goal_id)  # type: ignore[assignment]
    if goal["status"] == "BLOCKED" and "budget is used up" in str(goal.get("blocked_reason") or ""):
        if over_budget(store, goal) is None:
            store.update_goal_status(goal_id, "EXECUTING")
            goal = store.get_goal(goal_id)  # type: ignore[assignment]
    return goal


def column_of(status: str) -> str:
    for key, _label, states in COLUMNS:
        if status in states:
            return key
    return "running"


def summary(store: GoalStore, goal: dict[str, Any]) -> dict[str, Any]:
    tasks = store.list_tasks(goal["id"])
    counted = [t for t in tasks if t["status"] != "CANCELLED"]
    budget = _budget_of(goal)
    return {
        "id": goal["id"], "title": goal["objective"], "kind": job_kind(goal),
        "owner": (goal.get("resource_limits") or {}).get("owner_agent") or goal.get("owner"),
        "status": goal["status"], "column": column_of(goal["status"]), "reason": goal.get("blocked_reason"),
        "steps_total": len(counted), "steps_done": sum(1 for t in counted if t["status"] == "COMPLETED"),
        "budget": budget, "spent": spend(store, goal, tasks) if budget else None,
        "created_at": goal["created_at"], "updated_at": goal["updated_at"],
    }


def board(store: GoalStore, limit: int = 100) -> dict[str, Any]:
    jobs = [g for g in store.list_goals() if is_job(g)]
    rows = [summary(store, g) for g in jobs[:limit]]
    return {
        "columns": [{"key": k, "label": label, "jobs": [r for r in rows if r["column"] == k]} for k, label, _ in COLUMNS],
        "total": len(jobs), "capped": len(jobs) > limit,
    }


def detail(store: GoalStore, goal_id: str, log_limit: int = 100) -> dict[str, Any] | None:
    goal = store.get_goal(goal_id)
    if goal is None or not is_job(goal):
        return None
    out = summary(store, goal)
    out["steps"] = [
        {"id": t["id"], "description": t["description"], "status": t["status"], "attempts": t["attempt_count"],
         "max_attempts": t["max_attempts"], "error": t["last_error"],
         "verified": (t.get("result") or {}).get("verified")}
        for t in store.list_tasks(goal_id)
    ]
    out["log"] = [
        {"at": e["at"], "type": e["type"], "detail": e["detail"], "task_id": e.get("task_id")}
        for e in store.goal_events(goal_id, limit=log_limit)
    ]
    out["can_pause"] = goal["status"] not in GOAL_TERMINAL_STATES and goal["status"] != "PAUSED"
    out["can_resume"] = goal["status"] == "PAUSED"
    out["can_cancel"] = goal["status"] not in GOAL_TERMINAL_STATES
    return out
