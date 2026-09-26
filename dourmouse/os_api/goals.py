"""Backend for the OS shell's GOALS screen (finding #147).

* ``GET  /api/os/goals/board``: every goal with its task counts and its tasks
  in one read, so the screen does not make one request per goal and the
  progress figure is counted from real task rows (completed over not
  cancelled), never guessed.
* ``POST /api/os/goals/pause {id}`` and ``/resume {id}``: the owner's click.
  Pause keeps the status the goal had in its own column and resume restores
  it exactly (``GoalStore.pause_goal`` and ``resume_goal``). Pausing lets the
  task already running finish, then no further task starts.
* ``POST /api/os/goals/create {objective, steps, priority, success_criteria}``:
  a goal the owner wrote, with the steps they wrote. There is no planner, so
  with no steps the objective itself is the one task. It runs unattended, so
  the screen says so before it sends this.

Cancel, audit and task approval stay on the existing ``/api/goals/cancel``,
``/api/audit`` and ``/api/goals/tasks/approve`` routes.
"""

from __future__ import annotations

import re
from typing import Any

from . import ApiError, Request, route

_ID = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
MAX_GOALS = 60
MAX_TASKS_PER_GOAL = 80
MAX_OBJECTIVE = 600
MAX_STEP = 500
MAX_STEPS = 12
MAX_CRITERIA = 6
_PRIORITIES = ("critical", "high", "normal", "low", "background")


def _store():
    from dourmouse.goals import get_goal_store

    return get_goal_store()


def _pending(task: dict[str, Any]) -> list[str]:
    """The exact actions a WAITING_FOR_APPROVAL task is parked on, so the
    screen can show them before the owner approves."""
    if task["status"] != "WAITING_FOR_APPROVAL":
        return []
    prompts = (task.get("result") or {}).get("pending_prompts") or []
    return [str(p)[:600] for p in list(prompts)[:5]]


def _goal_id(req: Request) -> str:
    gid = str(req.body.get("id") or "").strip()
    if not gid:
        raise ApiError(400, "id is required")
    if not _ID.match(gid):
        raise ApiError(400, "id is not a valid goal id")
    return gid


def _text_list(value: Any, name: str, limit: int, each: int) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        value = value.splitlines()
    if not isinstance(value, list):
        raise ApiError(400, f"{name} must be a list of lines")
    out = []
    for item in value:
        text = str(item).strip()
        if not text:
            continue
        if len(text) > each:
            raise ApiError(400, f"a line in {name} is longer than {each} characters")
        out.append(text)
    if len(out) > limit:
        raise ApiError(400, f"{name} allows at most {limit} lines")
    return out


@route("GET", "/api/os/goals/board")
def board(req: Request) -> tuple[int, dict[str, Any]]:
    store = _store()
    goals = store.list_goals()
    total_goals = len(goals)
    out = []
    for goal in goals[:MAX_GOALS]:
        tasks = store.list_tasks(goal["id"])
        counted = [t for t in tasks if t["status"] != "CANCELLED"]
        done = sum(1 for t in counted if t["status"] == "COMPLETED")
        goal["tasks_total"] = len(counted)
        goal["tasks_done"] = done
        goal["tasks_verified"] = sum(
            1 for t in counted if t["status"] == "COMPLETED" and (t.get("result") or {}).get("verified") is True
        )
        goal["tasks"] = [
            {
                "id": t["id"], "description": t["description"], "status": t["status"],
                "assigned_agent": t["assigned_agent"], "attempt_count": t["attempt_count"],
                "max_attempts": t["max_attempts"], "last_error": t["last_error"],
                "verified": (t.get("result") or {}).get("verified"),
                "pending_prompts": _pending(t),
            }
            for t in tasks[:MAX_TASKS_PER_GOAL]
        ]
        goal.pop("result", None)
        out.append(goal)
    runtime = getattr(req.server, "goal_runtime", None)
    return 200, {
        "ok": True, "goals": out, "total_goals": total_goals, "capped": total_goals > MAX_GOALS,
        "runtime": {"running": runtime is not None},
    }


@route("POST", "/api/os/goals/pause")
def pause(req: Request) -> tuple[int, dict[str, Any]]:
    gid = _goal_id(req)
    store = _store()
    if store.get_goal(gid) is None:
        raise ApiError(404, "no such goal")
    if not store.pause_goal(gid):
        raise ApiError(409, "this goal cannot be paused: it is already paused or already finished")
    return 200, {"ok": True, "goal": store.get_goal(gid)}


@route("POST", "/api/os/goals/resume")
def resume(req: Request) -> tuple[int, dict[str, Any]]:
    gid = _goal_id(req)
    store = _store()
    if store.get_goal(gid) is None:
        raise ApiError(404, "no such goal")
    if not store.resume_goal(gid):
        raise ApiError(409, "this goal is not paused")
    return 200, {"ok": True, "goal": store.get_goal(gid)}


@route("POST", "/api/os/goals/create")
def create(req: Request) -> tuple[int, dict[str, Any]]:
    objective = str(req.body.get("objective") or "").strip()
    if not objective:
        raise ApiError(400, "objective is required")
    if len(objective) > MAX_OBJECTIVE:
        raise ApiError(400, f"objective is longer than {MAX_OBJECTIVE} characters")
    steps = _text_list(req.body.get("steps"), "steps", MAX_STEPS, MAX_STEP)
    criteria = _text_list(req.body.get("success_criteria"), "success_criteria", MAX_CRITERIA, MAX_STEP)
    priority = str(req.body.get("priority") or "normal").strip().lower()
    if priority not in _PRIORITIES:
        raise ApiError(400, "priority must be one of " + ", ".join(_PRIORITIES))
    goal = _store().create_goal_with_steps(objective, steps, priority=priority, success_criteria=criteria)
    return 200, {"ok": True, "goal": goal, "steps": len(steps) or 1}
