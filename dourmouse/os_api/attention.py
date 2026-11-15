"""Routes for the attention feed (plan 1.8, finding 204).

* ``GET  /api/os/attention[?all=1]``: open items (needs you first), counts per kind and per screen, and the prefs.
* ``POST /api/os/attention/read {ids?}``: mark some (or all) items read.
* ``POST /api/os/attention/dismiss {id}`` and ``/clear``: dismiss one item, or every item that is not waiting on the owner.
* ``POST /api/os/attention/prefs {quiet_hours?, kinds?}``: quiet hours and the toast and native switches per kind.
* ``POST /api/os/attention/test``: one heads-up item that goes through the real delivery rules.
"""

from __future__ import annotations

from typing import Any

from dourmouse import attention

from . import ApiError, Request, route


def _store(req: Request) -> attention.AttentionStore:
    store = attention.get_attention_store()
    hub = getattr(req.server, "events_broadcast", None)
    if hub is not None:
        store.bind_hub(hub)
    return store


@route("GET", "/api/os/attention")
def feed(req: Request) -> tuple[int, dict[str, Any]]:
    store = _store(req)
    return 200, {"ok": True, "items": store.items(include_done=req.arg("all") == "1"), "counts": store.counts(), "prefs": store.prefs()}


@route("POST", "/api/os/attention/read")
def read(req: Request) -> tuple[int, dict[str, Any]]:
    ids = req.body.get("ids")
    if ids is not None and (not isinstance(ids, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids)):
        raise ApiError(400, "ids must be a list of item ids")
    store = _store(req)
    return 200, {"ok": True, "marked": store.mark_read(ids), "counts": store.counts()}


@route("POST", "/api/os/attention/dismiss")
def dismiss(req: Request) -> tuple[int, dict[str, Any]]:
    item_id = req.body.get("id")
    if not isinstance(item_id, int) or isinstance(item_id, bool):
        raise ApiError(400, "id must be an item id")
    store = _store(req)
    if not store.dismiss(item_id):
        raise ApiError(404, "no such open item")
    return 200, {"ok": True, "counts": store.counts()}


@route("POST", "/api/os/attention/clear")
def clear(req: Request) -> tuple[int, dict[str, Any]]:
    store = _store(req)
    return 200, {"ok": True, "cleared": store.clear_done(), "counts": store.counts()}


@route("POST", "/api/os/attention/prefs")
def prefs(req: Request) -> tuple[int, dict[str, Any]]:
    try:
        return 200, {"ok": True, "prefs": _store(req).set_prefs(req.body)}
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None


@route("POST", "/api/os/attention/test")
def test(req: Request) -> tuple[int, dict[str, Any]]:
    store = _store(req)
    item = store.add("heads_up", "This is a test notification", detail="If you can read this, notifications reach you.",
                     screen="settings", source="test")
    return 200, {"ok": True, "item": item, "delivery": store.delivery("heads_up")}
