"""Backend for app driving (phase F1): allow list, kill switch, snapshot, act.

Every route sits behind the server's auth gate and request guard like the rest
of ``os_api``. The writes here are the owner's own clicks: allowing an app,
removing one, and resuming after a kill exist only as these routes, never as
model tools. Refusals come back with a real status (403 deny or not allowed,
409 stale or changed, 423 killed, 503 no Accessibility permission) and the
specific reason, never a 200 with ``ok: false``.
"""

from __future__ import annotations

from typing import Any

from . import ApiError, Request, route


def _call(fn, *args, **kwargs) -> Any:
    from dourmouse.app_driver import AppDriverError

    try:
        return fn(*args, **kwargs)
    except AppDriverError as exc:
        raise ApiError(exc.http_status, str(exc)) from exc


def _actor(req: Request) -> str:
    return f"owner:{req.user}" if req.user else "owner"


def _text(req: Request, name: str, limit: int = 200) -> str:
    value = req.body.get(name)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ApiError(400, f"{name} must be a string")
    return value.strip()[:limit]


@route("GET", "/api/os/apps/status")
def status(req: Request) -> tuple[int, dict[str, Any]]:
    from dourmouse import app_driver

    return 200, {"ok": True, **_call(app_driver.status)}


@route("GET", "/api/os/apps/allowed")
def allowed(req: Request) -> tuple[int, dict[str, Any]]:
    from dourmouse import app_driver

    payload: dict[str, Any] = {"ok": True, "allowed": _call(app_driver.list_allowed)}
    try:
        payload["running"] = app_driver.running_apps()
    except app_driver.AppDriverError as exc:
        payload["running"] = []
        payload["running_error"] = str(exc)  # the allow list still shows; the running list says why it is empty
    return 200, payload


@route("POST", "/api/os/apps/allow")
def allow(req: Request) -> tuple[int, dict[str, Any]]:
    """Body ``{app}`` (name or bundle id) and optional ``{bundle_id}``. If the
    app is running, its real bundle id is recorded so a renamed impostor does
    not inherit the grant."""
    from dourmouse import app_driver

    name = _text(req, "app", 100)
    bundle_id = _text(req, "bundle_id", 100) or None
    if not name and not bundle_id:
        raise ApiError(400, "app is required")
    note = None
    try:
        running_apps = app_driver.running_apps()
    except app_driver.AppDriverError as exc:
        running_apps = []
        note = f"running apps could not be listed ({exc}); stored by the name given, without a bundle id"
    for running in running_apps:
        if name.casefold() in (str(running.get("name") or "").casefold(), str(running.get("bundle_id") or "").casefold()):
            name, bundle_id = str(running.get("name") or name), str(running.get("bundle_id") or "") or bundle_id
            break
    entry = _call(app_driver.allow, name, bundle_id, by=_actor(req))
    from dourmouse.app_driver import safety

    safety.audit("executed", "allow", {"app": name, "bundle_id": bundle_id}, _actor(req), app=entry.get("name"))
    return 200, {"ok": True, "entry": entry, "allowed": app_driver.list_allowed(), "note": note}


@route("POST", "/api/os/apps/deny")
def deny(req: Request) -> tuple[int, dict[str, Any]]:
    """Body ``{app}``: remove it from the allow list."""
    from dourmouse import app_driver
    from dourmouse.app_driver import safety

    name = _text(req, "app", 100)
    if not name:
        raise ApiError(400, "app is required")
    removed = _call(app_driver.disallow, name, _text(req, "bundle_id", 100) or None)
    safety.audit("executed", "disallow", {"app": name}, _actor(req), removed=removed)
    return 200, {"ok": True, "removed": removed, "allowed": app_driver.list_allowed()}


@route("POST", "/api/os/apps/kill")
def kill(req: Request) -> tuple[int, dict[str, Any]]:
    from dourmouse import app_driver

    return 200, {"ok": True, "kill": app_driver.engage_kill(_text(req, "reason") or "stopped by the owner", by=_actor(req))}


@route("POST", "/api/os/apps/resume")
def resume(req: Request) -> tuple[int, dict[str, Any]]:
    from dourmouse import app_driver

    return 200, {"ok": True, "kill": _call(app_driver.release_kill, by=_actor(req))}


@route("GET", "/api/os/apps/snapshot")
def snapshot(req: Request) -> tuple[int, dict[str, Any]]:
    from dourmouse import app_driver

    return 200, {"ok": True, **_call(app_driver.snapshot, req.need("app"), actor=_actor(req))}


@route("POST", "/api/os/apps/act")
def act(req: Request) -> tuple[int, dict[str, Any]]:
    """Body ``{action, snapshot_id, element_id, text?, direction?, amount?,
    dry_run?}`` for click, type and scroll, or ``{action: "press_key", app,
    key, modifiers?, dry_run?}``. Same checks as the model's tools."""
    from dourmouse import app_driver

    action = _text(req, "action", 20).lower()
    dry_run = req.body.get("dry_run", False)
    if not isinstance(dry_run, bool):
        raise ApiError(400, "dry_run must be true or false")
    if action == "press_key":
        modifiers = req.body.get("modifiers") or []
        if not isinstance(modifiers, list) or not all(isinstance(m, str) for m in modifiers):
            raise ApiError(400, "modifiers must be a list of strings")
        result = _call(app_driver.press_key, _text(req, "app", 100), _text(req, "key", 20), modifiers,
                       dry_run=dry_run, actor=_actor(req))
        return 200, result
    text = req.body.get("text")
    if text is not None and not isinstance(text, str):
        raise ApiError(400, "text must be a string")
    amount = req.body.get("amount", 0.25)
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        raise ApiError(400, "amount must be a number")
    result = _call(app_driver.act, _text(req, "snapshot_id", 40), _text(req, "element_id", 200), action,
                   text=text, direction=_text(req, "direction", 10) or "down", amount=amount,
                   dry_run=dry_run, actor=_actor(req))
    return 200, result
