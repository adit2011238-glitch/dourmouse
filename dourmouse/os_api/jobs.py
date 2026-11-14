"""Backend for the Jobs view (plan 1.3, finding 198).

* ``GET  /api/os/jobs``: the board, jobs grouped Needs you, Running, Paused, Done, Stopped.
* ``GET  /api/os/jobs/job?id=``: one job with its steps, budget spent and log.
* ``POST /api/os/jobs/start {title, steps, owner, budget, priority}``: an unattended job run by one agent.
* ``POST /api/os/jobs/pause|resume|cancel {id}`` and ``/budget {id, max_runs, max_tool_calls}``.

Approving a parked step stays on ``/api/goals/tasks/approve``.
"""

from __future__ import annotations

import re
from typing import Any

from dourmouse import jobs

from . import ApiError, Request, route

_ID = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


def _store():
    from dourmouse.goals import get_goal_store

    return get_goal_store()


def _job(req: Request) -> tuple[Any, str]:
    jid = str(req.body.get("id") or "").strip()
    if not _ID.match(jid):
        raise ApiError(400, "id is required and must be a job id")
    store = _store()
    goal = store.get_goal(jid)
    if goal is None or not jobs.is_job(goal):
        raise ApiError(404, "no such job")
    return store, jid


@route("GET", "/api/os/jobs")
def board(req: Request) -> tuple[int, dict[str, Any]]:
    return 200, {"ok": True, **jobs.board(_store())}


@route("GET", "/api/os/jobs/job")
def job(req: Request) -> tuple[int, dict[str, Any]]:
    jid = req.need("id")
    if not _ID.match(jid):
        raise ApiError(400, "not a job id")
    out = jobs.detail(_store(), jid)
    if out is None:
        raise ApiError(404, "no such job")
    return 200, {"ok": True, **out}


@route("POST", "/api/os/jobs/start")
def start(req: Request) -> tuple[int, dict[str, Any]]:
    body = req.body
    steps = body.get("steps")
    if isinstance(steps, str):
        steps = steps.splitlines()
    if steps is not None and not isinstance(steps, list):
        raise ApiError(400, "steps must be a list of lines")
    budget = body.get("budget")
    if budget is not None and not isinstance(budget, dict):
        raise ApiError(400, "budget must be an object")
    priority = str(body.get("priority") or "normal").strip().lower()
    try:
        goal = jobs.create_job(str(body.get("title") or ""), steps, owner=str(body.get("owner") or "orchestrator"),
                               budget=budget, priority=priority, store=_store())
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None
    return 200, {"ok": True, "id": goal["id"], "job": jobs.summary(_store(), goal)}


@route("POST", "/api/os/jobs/pause")
def pause(req: Request) -> tuple[int, dict[str, Any]]:
    store, jid = _job(req)
    if not store.pause_goal(jid):
        raise ApiError(409, "this job cannot be paused: it is already paused or finished")
    return 200, {"ok": True, "job": jobs.summary(store, store.get_goal(jid))}


@route("POST", "/api/os/jobs/resume")
def resume(req: Request) -> tuple[int, dict[str, Any]]:
    store, jid = _job(req)
    if not store.resume_goal(jid):
        raise ApiError(409, "this job is not paused")
    return 200, {"ok": True, "job": jobs.summary(store, store.get_goal(jid))}


@route("POST", "/api/os/jobs/cancel")
def cancel(req: Request) -> tuple[int, dict[str, Any]]:
    store, jid = _job(req)
    if not store.cancel_goal(jid):
        raise ApiError(409, "this job is already finished")
    return 200, {"ok": True, "job": jobs.summary(store, store.get_goal(jid))}


@route("POST", "/api/os/jobs/budget")
def budget(req: Request) -> tuple[int, dict[str, Any]]:
    store, jid = _job(req)
    raw = {k: req.body[k] for k in jobs.DEFAULT_BUDGET if k in req.body}
    if not raw:
        raise ApiError(400, "give max_runs, max_tool_calls or both")
    try:
        goal = jobs.set_budget(store, jid, raw)
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None
    return 200, {"ok": True, "job": jobs.summary(store, goal)}
