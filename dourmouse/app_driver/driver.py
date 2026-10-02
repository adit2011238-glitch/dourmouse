"""Snapshot an allowed app, then click, type, press a key or scroll by element id.

Order of checks on every act: kill switch, Accessibility trust, the app is
still running with the same pid, deny list, allow list, a fresh re-read of the
tree with the target's neighbourhood unchanged, the action's own checks, then
the kill switch once more right before the native call. See
~/Documents/DOURMOUSE/F1_APP_DRIVING_DESIGN.md.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from . import policy, safety
from .backend import NOT_TRUSTED_MESSAGE, get_backend
from .errors import AppDriverError

MAX_DEPTH = 12
MAX_NODES = 600
SNAPSHOT_TTL = 300.0
SNAPSHOT_CACHE = 16
TYPE_CHUNK = 20
MAX_TYPE_CHARS = 2000
ACTIVATE_WAIT = 1.5

#: Injectable clock and sleep, so tests never wait.
_sleep = time.sleep
_monotonic = time.monotonic

ACTIONS = ("click", "type", "press_key", "scroll")


@dataclass
class Snapshot:
    snapshot_id: str
    app: dict[str, Any]
    taken_at: float
    taken_mono: float
    elements: dict[str, dict[str, Any]]
    children: dict[str, list[str]]
    truncated: bool
    actor: str = ""
    order: list[str] = field(default_factory=list)


_cache: OrderedDict[str, Snapshot] = OrderedDict()
_cache_lock = threading.Lock()


def _store(snap: Snapshot) -> None:
    with _cache_lock:
        _cache[snap.snapshot_id] = snap
        while len(_cache) > SNAPSHOT_CACHE:
            _cache.popitem(last=False)


def _lookup(snapshot_id: str) -> Snapshot:
    with _cache_lock:
        snap = _cache.get(str(snapshot_id or ""))
    if snap is None:
        raise AppDriverError("stale", f"snapshot {str(snapshot_id)[:40]!r} is unknown or was evicted; take a new snapshot first.")
    if _monotonic() - snap.taken_mono > SNAPSHOT_TTL:
        raise AppDriverError("stale", f"snapshot {snap.snapshot_id} is older than {int(SNAPSHOT_TTL)}s; take a new snapshot first.")
    return snap


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _identity(node: dict[str, Any]) -> tuple[str, str, str, str]:
    return (node.get("role") or "", node.get("subrole") or "", node.get("title") or "", node.get("description") or "")


def _flatten(tree: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]], list[str]]:
    """Index-path ids ("0", "0.3", "0.3.1"); children lists keyed by parent id ("" is the window list)."""
    elements: dict[str, dict[str, Any]] = {}
    children: dict[str, list[str]] = {"": []}
    order: list[str] = []

    def walk(node: dict[str, Any], eid: str, parent: str, depth: int) -> None:
        elements[eid] = {**{k: v for k, v in node.items() if k != "children"}, "id": eid, "depth": depth}
        children.setdefault(parent, []).append(eid)
        children.setdefault(eid, [])
        order.append(eid)
        for i, child in enumerate(node.get("children") or []):
            walk(child, f"{eid}.{i}", eid, depth + 1)

    for i, window in enumerate(tree.get("windows") or []):
        walk(window, str(i), "", 0)
    return elements, children, order


def _neighbourhood(elements: dict[str, dict[str, Any]], children: dict[str, list[str]], eid: str) -> list[list[tuple[str, ...]]] | None:
    """The identities of the sibling list at every level down to ``eid``,
    which covers the target, every ancestor, and any insertion or removal
    that would shift an index along the path. None if ``eid`` is absent."""
    if eid not in elements:
        return None
    parts = eid.split(".")
    parents = [""] + [".".join(parts[:i]) for i in range(1, len(parts))]
    return [[_identity(elements[c]) for c in children.get(p, [])] for p in parents]


def _trusted_backend():
    backend = get_backend()
    if not backend.is_trusted():
        raise AppDriverError("not_trusted", NOT_TRUSTED_MESSAGE)
    return backend


def _find_app(backend, query: str) -> dict[str, Any]:
    q = (query or "").strip()
    if not q:
        raise AppDriverError("invalid", "an app name or bundle id is required")
    folded = q.casefold().removesuffix(".app")
    apps = backend.running_apps()
    for app in apps:
        if folded in (str(app.get("name") or "").casefold(), str(app.get("bundle_id") or "").casefold()):
            return app
    names = sorted({str(a.get("name") or "") for a in apps if a.get("name")})
    raise AppDriverError("not_running", f"no running app named {q!r}. Running: {', '.join(names[:20]) or 'none readable'}.")


def _app_by_pid(backend, app: dict[str, Any]) -> dict[str, Any]:
    for running in backend.running_apps():
        if int(running.get("pid") or -1) == int(app["pid"]):
            if (running.get("bundle_id") or "") != (app.get("bundle_id") or ""):
                break
            return running
    raise AppDriverError("stale", f"{app.get('name')} (pid {app.get('pid')}) is no longer running as it was at snapshot time; take a new snapshot.")


def _public(el: dict[str, Any]) -> dict[str, Any]:
    out = {k: el.get(k) for k in ("id", "role", "subrole", "title", "description", "placeholder", "value", "enabled", "focused", "x", "y", "w", "h", "depth")}
    if el.get("secure"):
        out["secure"] = True
        out["value"] = None
    for key in ("title", "description", "placeholder", "value"):
        out[key] = safety.scrub(out[key]) if out[key] else out[key]
    return out


def snapshot(app_query: str, actor: str = "model") -> dict[str, Any]:
    try:
        safety.require_not_killed()
        backend = _trusted_backend()
        app = _find_app(backend, app_query)
        policy.check_drivable(app.get("name"), app.get("bundle_id"), int(app["pid"]))
        tree = backend.read_tree(int(app["pid"]), MAX_DEPTH, MAX_NODES)
    except AppDriverError as exc:
        safety.audit("denied" if exc.is_refusal else "failed", "snapshot", {"app": app_query}, actor, reason=exc.code)
        raise
    elements, children, order = _flatten(tree)
    snap = Snapshot(
        snapshot_id=secrets.token_hex(6),
        app={"name": app.get("name"), "bundle_id": app.get("bundle_id"), "pid": int(app["pid"])},
        taken_at=time.time(),
        taken_mono=_monotonic(),
        elements={k: {kk: vv for kk, vv in v.items() if kk != "handle"} for k, v in elements.items()},
        children=children,
        truncated=bool(tree.get("truncated")),
        actor=actor,
        order=order,
    )
    _store(snap)
    safety.audit("executed", "snapshot", {"app": app_query}, actor, app=snap.app["name"], elements=len(order))
    return {
        "snapshot_id": snap.snapshot_id,
        "app": dict(snap.app),
        "taken_at": snap.taken_at,
        "truncated": snap.truncated,
        "count": len(order),
        "elements": [_public(snap.elements[eid]) for eid in order],
    }


def describe(snapshot_id: str, element_id: str) -> str:
    """Plain words for a confirmation prompt, from the cache only (no macOS call)."""
    with _cache_lock:
        snap = _cache.get(str(snapshot_id or ""))
    if snap is None:
        return f"element {element_id} of snapshot {snapshot_id} (that snapshot is no longer cached; the action will be refused)"
    el = snap.elements.get(str(element_id or ""))
    if el is None:
        return f"element {element_id} in {snap.app['name']} (not in that snapshot; the action will be refused)"
    pub = _public(el)
    label = pub.get("title") or pub.get("description") or pub.get("placeholder") or (pub.get("value") or "")[:40]
    role = (pub.get("role") or "element").removeprefix("AX").lower()
    return f"the {role} {label!r} ({element_id}) in {snap.app['name']}" if label else f"the {role} {element_id} in {snap.app['name']}"


def _reresolve(backend, snap: Snapshot, element_id: str) -> dict[str, Any]:
    tree = backend.read_tree(int(snap.app["pid"]), MAX_DEPTH, MAX_NODES)
    elements, children, _ = _flatten(tree)
    before = _neighbourhood(snap.elements, snap.children, element_id)
    if before is None:
        raise AppDriverError("invalid", f"element {element_id!r} is not in snapshot {snap.snapshot_id}.")
    after = _neighbourhood(elements, children, element_id)
    if after != before:
        raise AppDriverError(
            "tree_changed",
            f"the app's interface around element {element_id} changed since snapshot {snap.snapshot_id} "
            "(the element, an ancestor, or a sibling list differs). Nothing was done; take a new snapshot.",
        )
    fresh = elements[element_id]
    fresh["_children"] = children
    fresh["_elements"] = elements
    return fresh


def _type(backend, pid: int, text: str, app_name: str) -> int:
    """Post text in chunks; kill switch and frontmost are checked before each.
    Returns characters typed. A stop mid-way says exactly how far it got."""
    backend.activate(pid)
    deadline = _monotonic() + ACTIVATE_WAIT
    while backend.frontmost_pid() != pid:
        if _monotonic() >= deadline:
            raise AppDriverError("not_frontmost", f"{app_name} did not come to the front; nothing was typed.")
        _sleep(0.05)
    typed = 0
    for start in range(0, len(text), TYPE_CHUNK):
        if safety.is_killed():
            raise AppDriverError("killed", f"the kill switch stopped typing after {typed} of {len(text)} characters.")
        if backend.frontmost_pid() != pid:
            raise AppDriverError("not_frontmost", f"another app came to the front; typing stopped after {typed} of {len(text)} characters.")
        chunk = text[start:start + TYPE_CHUNK]
        backend.post_text(pid, chunk)
        typed += len(chunk)
    return typed


def _scroll_area(fresh: dict[str, Any], element_id: str) -> dict[str, Any]:
    elements = fresh["_elements"]
    parts = element_id.split(".")
    for i in range(len(parts), 0, -1):
        candidate = elements.get(".".join(parts[:i]))
        if candidate and candidate.get("role") == "AXScrollArea":
            return candidate
    for cid in fresh["_children"].get(element_id, []):
        if elements[cid].get("role") == "AXScrollArea":
            return elements[cid]
    raise AppDriverError("invalid", f"element {element_id} is not inside a scroll area, so there is nothing to scroll.")


def act(snapshot_id: str, element_id: str, action: str, *, text: str | None = None, direction: str = "down",
        amount: float = 0.25, dry_run: bool = False, actor: str = "model") -> dict[str, Any]:
    action = str(action or "").strip().lower()
    element_id = str(element_id or "").strip()
    audit_args = {"snapshot_id": snapshot_id, "element_id": element_id, "action": action, "text": text, "direction": direction}
    app_name = "?"
    try:
        safety.require_not_killed()
        if action not in ("click", "type", "scroll"):
            raise AppDriverError("invalid", f"action must be click, type or scroll (press_key has its own call), not {action!r}.")
        snap = _lookup(snapshot_id)
        app_name = str(snap.app.get("name"))
        backend = _trusted_backend()
        live = _app_by_pid(backend, snap.app)
        policy.check_drivable(live.get("name"), live.get("bundle_id"), int(live["pid"]))
        if action == "type":
            if not isinstance(text, str) or not text:
                raise AppDriverError("invalid", "type needs non-empty text.")
            if len(text) > MAX_TYPE_CHARS:
                raise AppDriverError("invalid", f"type is limited to {MAX_TYPE_CHARS} characters per call.")
        fresh = _reresolve(backend, snap, element_id)
        pid = int(snap.app["pid"])
        if action == "click" and not fresh.get("enabled", True):
            raise AppDriverError("invalid", f"{describe(snapshot_id, element_id)} is disabled.")
        if action == "type":
            safety.check_typed_text(text, fresh)
        if action == "scroll":
            if direction not in ("up", "down", "left", "right"):
                raise AppDriverError("invalid", "direction must be up, down, left or right.")
            if not isinstance(amount, (int, float)) or not 0 < float(amount) <= 1:
                raise AppDriverError("invalid", "amount is a fraction of the scroll range, above 0 and at most 1.")
            area = _scroll_area(fresh, element_id)
        target = describe(snapshot_id, element_id)
        if dry_run:
            safety.audit("executed", action, audit_args, actor, app=app_name, dry_run=True)
            return {"ok": True, "dry_run": True, "action": action, "app": app_name, "detail": f"DRY RUN: would {action} {target}. Checks passed; nothing was done."}
        safety.begin_action(app_name, action)
        try:
            safety.require_not_killed()
            if action == "click":
                backend.press(pid, fresh["handle"])
                detail = f"clicked {target}"
            elif action == "type":
                backend.focus(pid, fresh["handle"])
                typed = _type(backend, pid, text, app_name)
                detail = f"typed {typed} character(s) into {target}"
            else:
                orientation = "vertical" if direction in ("up", "down") else "horizontal"
                delta = float(amount) * (-1 if direction in ("up", "left") else 1)
                old, new = backend.scroll(pid, area["handle"], orientation, delta)
                detail = f"scrolled {direction} in {app_name} (scroll position {old:.2f} to {new:.2f})"
        finally:
            safety.end_action()
    except AppDriverError as exc:
        safety.audit("denied" if exc.is_refusal else "failed", action or "act", audit_args, actor, app=app_name, reason=exc.code)
        raise
    safety.audit("executed", action, audit_args, actor, app=app_name, text_chars=len(text) if text else 0)
    return {"ok": True, "action": action, "app": app_name, "element_id": element_id, "detail": detail}


def press_key(app_query: str, key: str, modifiers: list[str] | None = None, *, dry_run: bool = False, actor: str = "model") -> dict[str, Any]:
    from dourmouse.app_control_ax import _KEY_CODES, _MODIFIER_FLAGS

    key = str(key or "").strip().lower()
    mods = [str(m).strip().lower() for m in (modifiers or [])]
    audit_args = {"app": app_query, "key": key, "modifiers": mods}
    app_name = str(app_query)
    try:
        safety.require_not_killed()
        if key not in _KEY_CODES:
            raise AppDriverError("invalid", f"unknown key {key!r}; supported: {', '.join(sorted(_KEY_CODES))}.")
        bad = [m for m in mods if m not in _MODIFIER_FLAGS]
        if bad:
            raise AppDriverError("invalid", f"unknown modifier(s) {bad}; supported: {', '.join(sorted(_MODIFIER_FLAGS))}.")
        backend = _trusted_backend()
        app = _find_app(backend, app_query)
        app_name = str(app.get("name"))
        pid = int(app["pid"])
        policy.check_drivable(app.get("name"), app.get("bundle_id"), pid)
        label = "+".join(mods + [key])
        if dry_run:
            safety.audit("executed", "press_key", audit_args, actor, app=app_name, dry_run=True)
            return {"ok": True, "dry_run": True, "action": "press_key", "app": app_name, "detail": f"DRY RUN: would press {label} in {app_name}."}
        safety.begin_action(app_name, "press_key")
        try:
            backend.activate(pid)
            deadline = _monotonic() + ACTIVATE_WAIT
            while backend.frontmost_pid() != pid:
                if _monotonic() >= deadline:
                    raise AppDriverError("not_frontmost", f"{app_name} did not come to the front; no key was pressed.")
                _sleep(0.05)
            safety.require_not_killed()
            backend.post_key(pid, _KEY_CODES[key], mods)
        finally:
            safety.end_action()
    except AppDriverError as exc:
        safety.audit("denied" if exc.is_refusal else "failed", "press_key", audit_args, actor, app=app_name, reason=exc.code)
        raise
    safety.audit("executed", "press_key", audit_args, actor, app=app_name)
    return {"ok": True, "action": "press_key", "app": app_name, "detail": f"pressed {label} in {app_name}"}


def running_apps() -> list[dict[str, Any]]:
    """Foreground apps with their allowed and denied state (no trust needed for PyObjC)."""
    backend = get_backend()
    out = []
    for app in backend.running_apps():
        reason = policy.deny_reason(app.get("name"), app.get("bundle_id"), int(app.get("pid") or -1))
        out.append({**app, "denied": bool(reason), "deny_reason": reason,
                    "allowed": (not reason) and policy.is_allowed(app.get("name"), app.get("bundle_id"))})
    return out


def status() -> dict[str, Any]:
    from .backend import backend_note

    info: dict[str, Any] = {"kill": safety.kill_info(), "indicator": safety.indicator(), "allowed": policy.list_allowed(),
                            "deny_list": policy.deny_list(), "recent": safety.recent_actions()[-10:]}
    try:
        backend = get_backend()
        info.update({"backend": backend.name, "trusted": backend.is_trusted(), "backend_note": backend_note()})
    except AppDriverError as exc:
        info.update({"backend": None, "trusted": False, "backend_note": str(exc)})
    if not info["trusted"]:
        info["trust_help"] = NOT_TRUSTED_MESSAGE
    return info
