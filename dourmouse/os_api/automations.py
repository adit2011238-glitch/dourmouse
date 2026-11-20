"""Backend for the Automations view (plan 2.2, finding 207).

* ``GET  /api/os/automations``: the list (name, trigger in words, next run, last result word, switch) and the five templates.
* ``GET  /api/os/automations/detail?id=``: one automation with its run history and anything parked.
* ``POST /api/os/automations/preview``: the parsed card for a sentence, a template or explicit fields. Writes nothing.
* ``POST /api/os/automations/create``: the same input with ``approve: true``. Without it nothing is created.
* ``POST /api/os/automations/run|pause|resume|remove {id}``; ``/approve`` and ``/decline {run}`` for a parked tool step.

Old v1 schedules (one tool on a clock) appear in the same list and keep working; v2 adds triggers, prompts, reminders,
caps, history and parking. The mail trigger is off until plan 3.1.
"""

from __future__ import annotations

import re
from typing import Any

from dourmouse import automation_runs as runs
from dourmouse import automation_triggers as triggers
from dourmouse import schedules

from . import ApiError, Request, route

_ID = re.compile(r"^(sched|run)-[A-Za-z0-9-]{1,40}$")


def _engine(req: Request) -> runs.Engine:
    runner = getattr(req.server, "scheduler_runner", None)
    if runner is not None:
        return runner._engine()  # the one the scheduler uses, so a Run now and a tick share one history
    return runs.Engine(schedules.Schedules(), getattr(req.server, "registry", None))


def _store(req: Request) -> schedules.Schedules:
    return _engine(req).store


def _id(req: Request, name: str = "id") -> str:
    value = str(req.body.get(name) or req.arg(name) or "").strip()
    if not _ID.match(value):
        raise ApiError(400, f"{name} is required")
    return value


def _entry(req: Request) -> dict[str, Any]:
    entry = _store(req).get(_id(req))
    if entry is None:
        raise ApiError(404, "no such automation")
    return entry


def _row(entry: dict[str, Any], engine: runs.Engine) -> dict[str, Any]:
    last = engine.history.last(entry["id"])
    state = entry.get("state") or {}
    caps = runs.clean_caps(entry.get("caps"))
    action = entry.get("action") or {"type": "tool", "tool": entry.get("tool"), "arguments": entry.get("arguments") or {}}
    return {
        "id": entry["id"], "name": entry.get("name") or str(entry.get("tool") or entry["id"]).replace("_", " ").capitalize(),
        "version": int(entry.get("version") or 1),
        "trigger": triggers.describe(entry, schedules.describe_spec),
        "action": runs.action_words(action), "action_type": action.get("type") or "tool",
        "next_run": schedules.describe_next_run(entry) if (entry.get("spec") or {}).get("kind") else "",
        "enabled": bool(entry.get("enabled")), "auto_disabled": bool(state.get("auto_disabled")),
        "result": runs.RESULT_WORDS.get(last["status"], "") if last else "", "result_status": last["status"] if last else "",
        "last_at": last["at"] if last else "", "runs_today": engine.store.runs_today(entry), "caps": caps,
        "fail_count": int(state.get("fail_count") or 0), "watch_error": state.get("watch_error") or "",
        "template": entry.get("template") or "",
    }


@route("GET", "/api/os/automations")
def listing(req: Request) -> tuple[int, dict[str, Any]]:
    engine = _engine(req)
    try:
        engine.refresh_running()
    except Exception:  # noqa: BLE001 -- a job that cannot be read must not hide the list; the row keeps its last word
        pass
    rows = [_row(e, engine) for e in engine.store.list()]
    rows.sort(key=lambda r: (not r["enabled"], r["name"].lower()))
    return 200, {"ok": True, "automations": rows, "templates": _templates(), "mail_trigger": triggers.MAIL_TRIGGER_ENABLED,
                 "defaults": runs.DEFAULT_CAPS}


def _templates() -> list[dict[str, Any]]:
    out = []
    for t in runs.TEMPLATES:
        action = t["action"]
        out.append({"id": t["id"], "name": t["name"], "when": t["when"] or ({"file_appears": "When a file lands in Downloads"}.get(t["trigger"]["type"], "")),
                    "action": runs.action_words(action)})
    return out


@route("GET", "/api/os/automations/detail")
def detail(req: Request) -> tuple[int, dict[str, Any]]:
    engine = _engine(req)
    entry = engine.store.get(_id(req))
    if entry is None:
        raise ApiError(404, "no such automation")
    try:
        engine.refresh_running()
    except Exception:  # noqa: BLE001
        pass
    history = engine.history.for_automation(entry["id"], 50)
    for r in history:
        r["word"] = runs.RESULT_WORDS.get(str(r.get("status") or ""), "")
    action = entry.get("action") or {"type": "tool", "tool": entry.get("tool"), "arguments": entry.get("arguments") or {}}
    return 200, {"ok": True, "automation": _row(entry, engine), "tools": runs.tools_it_may_use(action),
                 "history": history, "parked": [r for r in history if r.get("status") == "parked" and not r.get("job_id")]}


# ---- create ---------------------------------------------------------------------------------------------------


def _build(req: Request) -> dict[str, Any]:
    """Turn the request into a validated automation. Raises ApiError with the reason, never half-builds."""
    body = req.body
    registry = getattr(req.server, "registry", None)
    try:
        template = None
        if body.get("template"):
            template = next((t for t in runs.TEMPLATES if t["id"] == body["template"]), None)
            if template is None:
                raise ValueError("no such template")
        sentence = str(body.get("sentence") or "").strip()
        parsed = runs.parse_sentence(sentence) if sentence else None

        # trigger
        raw_trigger = body.get("trigger") if isinstance(body.get("trigger"), dict) else (template["trigger"] if template else None)
        spec: dict[str, Any] = {}
        text = ""
        if raw_trigger is None:
            if parsed is None:
                raise ValueError("say when, or choose a template")
            raw_trigger = {"type": "time"}
        if raw_trigger.get("type", "time") == "time":
            if parsed is not None:
                spec, text = parsed["spec"], parsed["schedule_text"]
            else:
                text = str(body.get("when") or (template or {}).get("when") or "")
                spec = schedules.parse_schedule(text)
        trigger = triggers.clean_trigger(raw_trigger, spec)

        # action
        raw_action = body.get("action") if isinstance(body.get("action"), dict) else (template["action"] if template else None)
        if raw_action is None and parsed is not None:
            if parsed["reminder"] and parsed["rest"]:
                raw_action = {"type": "reminder", "text": parsed["rest"]}
            elif parsed["rest"]:
                raw_action = {"type": "prompt", "prompt": parsed["rest"], "owner": "orchestrator"}
        if raw_action is None:
            raise ValueError("say what to do, for example: every weekday at 8 check my security")
        action = runs.clean_action(raw_action, registry)

        name = runs.clean_name(body.get("name") or (template or {}).get("name"), runs.action_words(action)[:40])
        caps = runs.clean_caps(body.get("caps"))
        delivery = {"notify": bool((body.get("delivery") or {}).get("notify", True))}
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None
    return {"name": name, "trigger": trigger, "spec": spec, "schedule_text": text, "action": action, "caps": caps,
            "delivery": delivery, "template": (template or {}).get("id", "")}


def _card(built: dict[str, Any]) -> dict[str, Any]:
    entry = {"trigger": built["trigger"], "spec": built["spec"]}
    return {
        "name": built["name"], "trigger": triggers.describe(entry, schedules.describe_spec),
        "action": runs.action_words(built["action"]), "action_type": built["action"]["type"],
        "tools": runs.tools_it_may_use(built["action"]),
        "delivery": "A notice when it finishes" if built["delivery"]["notify"] else "Only in its history",
        "caps": built["caps"],
        "caps_text": f"At most {built['caps']['max_runs_per_day']} runs a day, {built['caps']['max_steps']} steps each",
    }


@route("POST", "/api/os/automations/preview")
def preview(req: Request) -> tuple[int, dict[str, Any]]:
    return 200, {"ok": True, "card": _card(_build(req))}


@route("POST", "/api/os/automations/create")
def create(req: Request) -> tuple[int, dict[str, Any]]:
    built = _build(req)
    if req.body.get("approve") is not True:
        raise ApiError(400, "approve is required: this creates something that runs by itself")
    store = _store(req)
    entry = store.add_v2(built["name"], built["trigger"], built["action"], spec=built["spec"],
                         schedule_text=built["schedule_text"], delivery=built["delivery"], caps=built["caps"],
                         template=built["template"])
    return 200, {"ok": True, "automation": _row(entry, _engine(req))}


# ---- act on one -----------------------------------------------------------------------------------------------


@route("POST", "/api/os/automations/run")
def run_now(req: Request) -> tuple[int, dict[str, Any]]:
    entry = _entry(req)
    row = _engine(req).run(entry, "Run now", manual=True)
    row["word"] = runs.RESULT_WORDS.get(row["status"], "")
    return 200, {"ok": True, "run": row}


@route("POST", "/api/os/automations/pause")
def pause(req: Request) -> tuple[int, dict[str, Any]]:
    entry = _entry(req)
    _store(req).set_enabled(entry["id"], False)
    return 200, {"ok": True}


@route("POST", "/api/os/automations/resume")
def resume(req: Request) -> tuple[int, dict[str, Any]]:
    entry = _entry(req)
    if (entry.get("trigger") or {}).get("type") == "mail_match":
        raise ApiError(409, "the mail trigger is not available yet")
    _store(req).set_enabled(entry["id"], True)
    return 200, {"ok": True}


@route("POST", "/api/os/automations/remove")
def remove(req: Request) -> tuple[int, dict[str, Any]]:
    entry = _entry(req)
    engine = _engine(req)
    engine.store.remove(entry["id"])
    engine.history.forget(entry["id"])
    return 200, {"ok": True}


@route("POST", "/api/os/automations/approve")
def approve(req: Request) -> tuple[int, dict[str, Any]]:
    run_id = _id(req, "run")
    try:
        row = _engine(req).approve_parked(run_id)
    except LookupError as exc:
        raise ApiError(404, str(exc)) from None
    except ValueError as exc:
        raise ApiError(409, str(exc)) from None
    return 200, {"ok": True, "run": row}


@route("POST", "/api/os/automations/decline")
def decline(req: Request) -> tuple[int, dict[str, Any]]:
    try:
        row = _engine(req).decline_parked(_id(req, "run"))
    except LookupError as exc:
        raise ApiError(404, str(exc)) from None
    return 200, {"ok": True, "run": row}
