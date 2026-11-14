"""Routes for the Projects store (plan 1.4, finding 199). Singular ``/api/os/project/...`` on purpose: the older
folder-based ``/api/os/projects/...`` routes in ``projects.py`` keep serving today's screen until the Projects screens
(plan 1.6) replace it.

Reads: ``GET list?archived=&q=``, ``get?id=``, ``templates``, ``search?id=&q=``, ``timeline?id=``,
``thread?id=&thread=``, ``file?id=&file=`` and ``output?id=&output=``.
Writes (POST, JSON): ``create``, ``update``, ``delete``, ``file/add``, ``file/remove``, ``memory/add``,
``memory/forget``, ``work/add``, ``work/update``, ``work/remove``, ``work/run`` (starts a job for the item),
``output/add``, ``thread/new``, ``thread/turn``, ``thread/summary`` and ``thread/archive``.
"""

from __future__ import annotations

import base64
import binascii
import re
from typing import Any

from dourmouse import projects_store as ps

from . import ApiError, Request, route

_ID = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


def _store() -> ps.ProjectStore:
    return ps.get_project_store()


def _id(value: Any, what: str = "id") -> str:
    text = str(value or "").strip()
    if not _ID.match(text):
        raise ApiError(400, f"{what} is required")
    return text


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None
    except LookupError as exc:
        raise ApiError(404, str(exc)) from None


def _sync_jobs(store: ps.ProjectStore, project_id: str) -> None:
    """Work items started as jobs follow their job: finished means done, stopped means back to todo with a note."""
    from dourmouse.goals import get_goal_store

    proj = store.get(project_id)
    linked = [w for w in proj["work_items"] if w["job_id"] and w["status"] != "done"]
    if not linked:
        return
    goals = get_goal_store()
    for item in linked:
        goal = goals.get_goal(item["job_id"])
        if goal is None:
            continue
        if goal["status"] == "COMPLETED":
            store.update_work(project_id, item["id"], status="done")
            store.add_output(project_id, item["title"], kind="job", body=f"Finished by job {item['job_id']}.", job_id=item["job_id"])
        elif goal["status"] in ("FAILED", "CANCELLED", "EXPIRED") and item["status"] == "doing":
            store.update_work(project_id, item["id"], status="todo", notes=(item["notes"] + f"\nThe job stopped: {goal['status'].lower()}.").strip())
            store.note_event(project_id, "work", f"Job for \"{item['title']}\" stopped ({goal['status'].lower()})", item["id"])


@route("GET", "/api/os/project/templates")
def templates(req: Request) -> tuple[int, dict[str, Any]]:
    return 200, {"ok": True, "templates": [
        {"key": k, "label": v["label"], "goal": v["goal"], "agents": v["agents"], "work_items": v["work_items"]}
        for k, v in ps.TEMPLATES.items()]}


@route("GET", "/api/os/project/list")
def project_list(req: Request) -> tuple[int, dict[str, Any]]:
    show = req.arg("archived").lower() in ("1", "true", "yes")
    return 200, {"ok": True, "projects": _store().list(include_archived=show, query=req.arg("q"))}


@route("GET", "/api/os/project/get")
def project_get(req: Request) -> tuple[int, dict[str, Any]]:
    pid = _id(req.arg("id"))
    store = _store()
    _call(store.get, pid)
    _sync_jobs(store, pid)
    return 200, {"ok": True, "project": _call(store.get, pid)}


@route("POST", "/api/os/project/create")
def project_create(req: Request) -> tuple[int, dict[str, Any]]:
    b = req.body
    kwargs = {k: b[k] for k in ("goal", "instructions", "agents") if k in b}
    return 200, {"ok": True, "project": _call(_store().create, b.get("name"), template=str(b.get("template") or "blank"), **kwargs)}


@route("POST", "/api/os/project/update")
def project_update(req: Request) -> tuple[int, dict[str, Any]]:
    pid = _id(req.body.get("id"))
    fields = {k: v for k, v in req.body.items() if k != "id"}
    return 200, {"ok": True, "project": _call(_store().update, pid, **fields)}


@route("POST", "/api/os/project/delete")
def project_delete(req: Request) -> tuple[int, dict[str, Any]]:
    if req.body.get("confirm") is not True:
        raise ApiError(400, "deleting a project removes its files, memory and threads for good: send confirm: true")
    _call(_store().delete, _id(req.body.get("id")))
    return 200, {"ok": True}


@route("GET", "/api/os/project/search")
def project_search(req: Request) -> tuple[int, dict[str, Any]]:
    pid = _id(req.arg("id"))
    try:
        limit = int(req.arg("limit") or 8)
    except ValueError:
        raise ApiError(400, "limit must be a number") from None
    return 200, {"ok": True, "results": _call(_store().search, pid, req.arg("q"), limit)}


@route("GET", "/api/os/project/timeline")
def project_timeline(req: Request) -> tuple[int, dict[str, Any]]:
    return 200, {"ok": True, "timeline": _call(_store().timeline, _id(req.arg("id")))}


# ---- files and memory -----------------------------------------------------------------------------------------------

@route("POST", "/api/os/project/file/add")
def file_add(req: Request) -> tuple[int, dict[str, Any]]:
    b = req.body
    pid = _id(b.get("id"))
    text, b64 = b.get("text"), b.get("data_base64")
    if text is not None and not isinstance(text, str):
        raise ApiError(400, "text must be text")
    data = None
    if b64 is not None:
        try:
            data = base64.b64decode(str(b64), validate=True)
        except (binascii.Error, ValueError):
            raise ApiError(400, "data_base64 is not valid base64") from None
    return 200, {"ok": True, "file": _call(_store().add_file, pid, b.get("name"), text=text, data=data)}


@route("GET", "/api/os/project/file")
def file_get(req: Request) -> tuple[int, dict[str, Any]]:
    name, raw = _call(_store().read_file, _id(req.arg("id")), _id(req.arg("file"), "file"))
    try:
        return 200, {"ok": True, "name": name, "size": len(raw), "text": raw.decode("utf-8")}
    except UnicodeDecodeError:
        return 200, {"ok": True, "name": name, "size": len(raw), "data_base64": base64.b64encode(raw).decode("ascii")}


@route("POST", "/api/os/project/file/remove")
def file_remove(req: Request) -> tuple[int, dict[str, Any]]:
    _call(_store().remove_file, _id(req.body.get("id")), _id(req.body.get("file"), "file"))
    return 200, {"ok": True}


@route("POST", "/api/os/project/memory/add")
def memory_add(req: Request) -> tuple[int, dict[str, Any]]:
    return 200, {"ok": True, "memory": _call(_store().remember, _id(req.body.get("id")), req.body.get("text"))}


@route("POST", "/api/os/project/memory/forget")
def memory_forget(req: Request) -> tuple[int, dict[str, Any]]:
    _call(_store().forget, _id(req.body.get("id")), _id(req.body.get("memory"), "memory"))
    return 200, {"ok": True}


# ---- work items and jobs --------------------------------------------------------------------------------------------

@route("POST", "/api/os/project/work/add")
def work_add(req: Request) -> tuple[int, dict[str, Any]]:
    b = req.body
    return 200, {"ok": True, "work": _call(_store().add_work, _id(b.get("id")), b.get("title"),
                                           assignee=str(b.get("assignee") or ""), notes=b.get("notes") or "")}


@route("POST", "/api/os/project/work/update")
def work_update(req: Request) -> tuple[int, dict[str, Any]]:
    b = req.body
    fields = {k: v for k, v in b.items() if k not in ("id", "work")}
    if "job_id" in fields:
        raise ApiError(400, "a work item is tied to a job with work/run, not by hand")
    return 200, {"ok": True, "work": _call(_store().update_work, _id(b.get("id")), _id(b.get("work"), "work"), **fields)}


@route("POST", "/api/os/project/work/remove")
def work_remove(req: Request) -> tuple[int, dict[str, Any]]:
    _call(_store().remove_work, _id(req.body.get("id")), _id(req.body.get("work"), "work"))
    return 200, {"ok": True}


@route("POST", "/api/os/project/work/run")
def work_run(req: Request) -> tuple[int, dict[str, Any]]:
    """Start an unattended job for one work item, run by its assigned agent (else the orchestrator) under the project's
    instructions. The job belongs to the project; the item moves to doing and follows the job."""
    from dourmouse import jobs

    b = req.body
    pid, wid = _id(b.get("id")), _id(b.get("work"), "work")
    store = _store()
    proj = _call(store.get, pid)
    item = _call(store.work_item, pid, wid)
    if item["job_id"] and item["status"] == "doing":
        raise ApiError(409, "this item already has a job running")
    if item["status"] == "done":
        raise ApiError(409, "this item is already done")
    title = item["title"]
    steps = [f"{title}\n\nProject: {proj['name']}. Goal: {proj['goal']}\nInstructions: {proj['instructions']}".strip()] if proj["instructions"] else [title]
    budget = b.get("budget")
    if budget is not None and not isinstance(budget, dict):
        raise ApiError(400, "budget must be an object")
    try:
        goal = jobs.create_job(title, steps, owner=item["assignee"] or "orchestrator", budget=budget, project_id=pid)
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None
    _call(store.update_work, pid, wid, status="doing", job_id=goal["id"])
    store.note_event(pid, "job", f"Job started for \"{title}\"", goal["id"])
    return 200, {"ok": True, "job_id": goal["id"], "work": _call(store.work_item, pid, wid)}


# ---- outputs --------------------------------------------------------------------------------------------------------

@route("POST", "/api/os/project/output/add")
def output_add(req: Request) -> tuple[int, dict[str, Any]]:
    b = req.body
    return 200, {"ok": True, "output": _call(_store().add_output, _id(b.get("id")), b.get("title"), kind=str(b.get("kind") or "note"),
                                             body=b.get("body") or "", file_id=str(b.get("file") or ""))}


@route("GET", "/api/os/project/output")
def output_get(req: Request) -> tuple[int, dict[str, Any]]:
    return 200, {"ok": True, "output": _call(_store().get_output, _id(req.arg("id")), _id(req.arg("output"), "output"))}


# ---- threads --------------------------------------------------------------------------------------------------------

@route("POST", "/api/os/project/thread/new")
def thread_new(req: Request) -> tuple[int, dict[str, Any]]:
    return 200, {"ok": True, "thread": _call(_store().new_thread, _id(req.body.get("id")), str(req.body.get("title") or "New thread"))}


@route("GET", "/api/os/project/thread")
def thread_get(req: Request) -> tuple[int, dict[str, Any]]:
    return 200, {"ok": True, "thread": _call(_store().thread, _id(req.arg("id")), _id(req.arg("thread"), "thread"))}


@route("POST", "/api/os/project/thread/turn")
def thread_turn(req: Request) -> tuple[int, dict[str, Any]]:
    """Save one exchange in a project thread. Only a real project thread can be written to, so a quick ask is never stored."""
    b = req.body
    _call(_store().record_turn, _id(b.get("id")), _id(b.get("thread"), "thread"), b.get("user") or "", b.get("assistant") or "")
    return 200, {"ok": True}


@route("POST", "/api/os/project/thread/summary")
def thread_summary(req: Request) -> tuple[int, dict[str, Any]]:
    b = req.body
    _call(_store().summarise_thread, _id(b.get("id")), _id(b.get("thread"), "thread"), b.get("summary") or "")
    return 200, {"ok": True}


@route("POST", "/api/os/project/thread/archive")
def thread_archive(req: Request) -> tuple[int, dict[str, Any]]:
    _call(_store().archive_thread, _id(req.body.get("id")), _id(req.body.get("thread"), "thread"))
    return 200, {"ok": True}
