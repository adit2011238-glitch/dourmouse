"""Backend for the Memory view (plan 2.4, PL-06, finding 210).

What Dourmouse knows about the owner, where it learned it, and how to fix it.

* ``GET  /api/os/memory?q=&kind=&origin=&pinned=&expiring=&used_days=&closed=&review=&offset=``: the list and the chip counts.
* ``GET  /api/os/memory/fact?id=``: one fact with its history and the replies that used it.
* ``GET  /api/os/memory/why?session=&turn=``: the facts put in one reply's prompt ("why did you say that").
* ``GET  /api/os/memory/merge``: pairs that look like the same fact.
* ``GET  /api/os/memory/export``: every fact as JSON and as readable text; the screen saves it through the save dialog.
* ``POST /api/os/memory/add|edit|pin|hide|supersede|keep|forget|merge|forget_where``: owner only. Each is written to the
  action ledger by name and argument hash, never the text.

The Memory view works on the local store. With only a remote memory store set, the routes say so (503).
"""

from __future__ import annotations

import re
from typing import Any

from dourmouse import execution_policy
from dourmouse.memory_store import KINDS, ORIGINS, LocalFallbackMemoryStore, MemoryStore

from . import ApiError, Request, route

_SESSION = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")


def _store(req: Request) -> MemoryStore:
    store = getattr(req.server, "memory", None)
    if isinstance(store, LocalFallbackMemoryStore):
        store = store._local_store()
    if not isinstance(store, MemoryStore):
        raise ApiError(503, "Memory is off, or only a remote memory store is set. This view needs the local one.")
    return store


def _fact_id(req: Request) -> int:
    raw = req.body.get("id") if req.body.get("id") is not None else req.arg("id")
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ApiError(400, "id is required") from None


def _audit(req: Request, action: str, **args: Any) -> None:
    execution_policy.record("executed", f"memory.{action}", args, req.user or "owner")


def _int(req: Request, name: str, default: int = 0) -> int:
    try:
        return int(req.arg(name) or default)
    except ValueError:
        raise ApiError(400, f"{name} must be a number") from None


def _flag(req: Request, name: str) -> bool | None:
    value = req.arg(name).lower()
    return None if value == "" else value in ("1", "true", "yes")


def _public(fact: dict[str, Any]) -> dict[str, Any]:
    return fact


@route("GET", "/api/os/memory")
def listing(req: Request) -> tuple[int, dict[str, Any]]:
    store = _store(req)
    kind, origin = req.arg("kind"), req.arg("origin")
    if kind and kind not in KINDS:
        raise ApiError(400, "unknown kind")
    if origin and origin not in ORIGINS:
        raise ApiError(400, "unknown origin")
    out = store.list_facts(query=req.arg("q"), kind=kind, origin=origin, pinned=_flag(req, "pinned") or None,
                           expiring=bool(_flag(req, "expiring")), used_days=_int(req, "used_days"),
                           closed=bool(_flag(req, "closed")), review=bool(_flag(req, "review")),
                           limit=_int(req, "limit", 100) or 100, offset=_int(req, "offset"))
    from dourmouse import privacy

    try:
        cloud = bool(privacy.auto_injection_allowed("memory"))
    except Exception:  # noqa: BLE001 -- the line is informative; a privacy store that cannot be read says "unknown"
        cloud = None
    return 200, {"ok": True, **out, "counts": store.counts(), "kinds": list(KINDS), "origins": list(ORIGINS),
                 "cloud_recall": cloud}


@route("GET", "/api/os/memory/fact")
def fact(req: Request) -> tuple[int, dict[str, Any]]:
    found = _store(req).get_fact(_fact_id(req))
    if found is None:
        raise ApiError(404, "no such fact")
    return 200, {"ok": True, "fact": found}


@route("GET", "/api/os/memory/why")
def why(req: Request) -> tuple[int, dict[str, Any]]:
    session = req.need("session")
    if not _SESSION.match(session):
        raise ApiError(400, "session is not valid")
    turn = _int(req, "turn")
    facts = _store(req).used_in_turn(session, turn)
    return 200, {"ok": True, "facts": [{k: f[k] for k in ("id", "kind", "origin", "body", "pinned", "hidden", "closed")}
                                       for f in facts]}


@route("GET", "/api/os/memory/merge")
def merge_pairs(req: Request) -> tuple[int, dict[str, Any]]:
    return 200, {"ok": True, "pairs": _store(req).merge_suggestions()}


def _markdown(facts: list[dict[str, Any]]) -> str:
    lines = ["# What Dourmouse knows", ""]
    for f in facts:
        state = " (replaced)" if f.get("closed") else ""
        lines.append(f"- {f['body']}{state}  \n  {f.get('kind') or 'fact'}, {f.get('origin') or ''}, {str(f.get('created_at') or '')[:10]}")
    return "\n".join(lines) + "\n"


@route("GET", "/api/os/memory/export")
def export(req: Request) -> tuple[int, dict[str, Any]]:
    data = _store(req).export_all()
    return 200, {"ok": True, "json": data, "markdown": _markdown(data["facts"]), "count": len(data["facts"])}


@route("POST", "/api/os/memory/add")
def add(req: Request) -> tuple[int, dict[str, Any]]:
    try:
        made = _store(req).add_by_hand(str(req.body.get("text") or ""), str(req.body.get("kind") or "fact"),
                                       str(req.body.get("project") or ""))
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None
    _audit(req, "add", text=req.body.get("text"))
    return 200, {"ok": True, "fact": made}


@route("POST", "/api/os/memory/edit")
def edit(req: Request) -> tuple[int, dict[str, Any]]:
    try:
        made = _store(req).edit_fact(_fact_id(req), body=req.body.get("text"), kind=req.body.get("kind"))
    except LookupError:
        raise ApiError(404, "no such fact") from None
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None
    _audit(req, "edit", id=made.get("id"), text=req.body.get("text"))
    return 200, {"ok": True, "fact": made}


def _toggle(req: Request, flag: str) -> tuple[int, dict[str, Any]]:
    value = req.body.get("value", True)
    try:
        made = _store(req).set_flag(_fact_id(req), flag, bool(value))
    except LookupError:
        raise ApiError(404, "no such fact") from None
    _audit(req, flag, id=made.get("id"), value=bool(value))
    return 200, {"ok": True, "fact": made}


@route("POST", "/api/os/memory/pin")
def pin(req: Request) -> tuple[int, dict[str, Any]]:
    return _toggle(req, "pinned")


@route("POST", "/api/os/memory/hide")
def hide(req: Request) -> tuple[int, dict[str, Any]]:
    return _toggle(req, "hidden")


@route("POST", "/api/os/memory/keep")
def keep(req: Request) -> tuple[int, dict[str, Any]]:
    store = _store(req)
    store.mark_reviewed(_fact_id(req))
    _audit(req, "keep", id=_fact_id(req))
    return 200, {"ok": True, "counts": store.counts()}


@route("POST", "/api/os/memory/supersede")
def supersede(req: Request) -> tuple[int, dict[str, Any]]:
    try:
        made = _store(req).supersede(_fact_id(req), str(req.body.get("text") or ""))
    except LookupError:
        raise ApiError(404, "no such fact") from None
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None
    _audit(req, "supersede", id=made.get("id"), text=req.body.get("text"))
    return 200, {"ok": True, "fact": made}


@route("POST", "/api/os/memory/merge")
def merge(req: Request) -> tuple[int, dict[str, Any]]:
    try:
        made = _store(req).merge(int(req.body.get("keep")), int(req.body.get("drop")))
    except (TypeError, ValueError):
        raise ApiError(400, "keep and drop are required") from None
    except LookupError:
        raise ApiError(404, "no such fact") from None
    _audit(req, "merge", keep=req.body.get("keep"), drop=req.body.get("drop"))
    return 200, {"ok": True, "fact": made}


@route("POST", "/api/os/memory/forget")
def forget(req: Request) -> tuple[int, dict[str, Any]]:
    """Needs ``confirm: true``: the screen asks first, and a request without it deletes nothing."""
    if req.body.get("confirm") is not True:
        raise ApiError(400, "Forgetting cannot be undone. Send confirm: true after asking the owner.")
    gone = _store(req).forget(_fact_id(req))
    if not gone:
        raise ApiError(404, "no such fact")
    _audit(req, "forget", id=_fact_id(req))
    return 200, {"ok": True, "forgotten": 1}


@route("POST", "/api/os/memory/forget_where")
def forget_where(req: Request) -> tuple[int, dict[str, Any]]:
    """Forget by source or date range. Without ``confirm: true`` it only counts, so the sheet can show the number."""
    spec = {k: str(req.body.get(k) or "") for k in ("source", "since", "until")}
    store = _store(req)
    try:
        n = store.forget_where(**spec, dry_run=req.body.get("confirm") is not True)
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None
    if req.body.get("confirm") is True:
        _audit(req, "forget_where", **spec, count=n)
        return 200, {"ok": True, "forgotten": n}
    return 200, {"ok": True, "would_forget": n}
