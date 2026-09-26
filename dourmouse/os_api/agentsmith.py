"""Backend for the OS shell's AGENTSMITH screen (finding #149).

The review gate for a tool Dourmouse drafted for itself. The model can draft;
only the owner approves, and this module adds no path that lets a model do it.

* ``GET  /api/os/agentsmith/board``: every draft (light fields only), with
  whether each approved tool is actually live in the running server. An
  approved tool is callable only after a restart, so "approved" and "live" are
  different facts and the screen shows both.
* ``GET  /api/os/agentsmith/draft?id=``: one draft with everything the owner
  must read before approving: the exact module text that would be written and
  executed (``module_preview``), its sha256, the draft's own test, the
  parameters schema, and for an approved draft whether the file on disk still
  matches the hash recorded at approval.
* ``POST /api/os/agentsmith/approve {id, sha256}``: approves only if ``sha256``
  is the hash of the module text as it stands now, so the owner approves the
  bytes they read and a draft edited after they opened it is refused. It calls
  the same ``self_extensions.approve`` as ``/api/self_extensions/approve``.

Rejecting goes through the existing ``/api/self_extensions/reject``.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from . import ApiError, Request, route

_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
MAX_DRAFTS = 200
#: The owner must be able to read every byte before approving; a draft larger
#: than this is shown as too large to review and cannot be approved here.
MAX_REVIEW_CHARS = 300_000

_LIGHT = (
    "id", "status", "tool_name", "description", "capability_gap", "goal_id",
    "created_at", "decided_at", "decision_reason",
)


def _store():
    from dourmouse.self_extensions import SelfExtensions

    return SelfExtensions()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _live_names(server: Any) -> frozenset[str] | None:
    """Tool names in the running server's registry, or None when it cannot say."""
    registry = getattr(server, "registry", None)
    names = getattr(registry, "tool_names", None)
    if names is None:
        return None
    try:
        return frozenset(names)
    except TypeError:
        return None


@route("GET", "/api/os/agentsmith/board")
def board(req: Request):
    drafts = _store().list()
    live = _live_names(req.server)
    out = []
    for d in drafts[-MAX_DRAFTS:][::-1]:
        row = {k: d.get(k) for k in _LIGHT}
        row["description"] = str(row["description"] or "")[:600]
        row["capability_gap"] = str(row["capability_gap"] or "")[:600]
        row["decision_reason"] = str(row["decision_reason"] or "")[:2000] or None
        schema = d.get("parameters_schema")
        row["parameters_schema"] = schema if isinstance(schema, dict) else {}
        if d.get("status") == "APPROVED":
            row["live"] = None if live is None else str(d.get("tool_name")) in live
        out.append(row)
    counts: dict[str, int] = {}
    for d in drafts:
        counts[str(d.get("status"))] = counts.get(str(d.get("status")), 0) + 1
    restart = sum(1 for r in out if r.get("live") is False)
    return 200, {"ok": True, "drafts": out, "total": len(drafts), "shown": len(out), "counts": counts, "restart_needed": restart}


def _draft_id(value: Any) -> str:
    did = str(value or "").strip()
    if not did:
        raise ApiError(400, "id is required")
    if not _ID.match(did):
        raise ApiError(400, "id is not a valid draft id")
    return did


@route("GET", "/api/os/agentsmith/draft")
def draft(req: Request):
    did = _draft_id(req.arg("id"))
    entry = _store().get(did)
    if entry is None:
        raise ApiError(404, f"no such draft: {did}")
    preview = str(entry.get("module_preview") or "")
    test = str(entry.get("test_source") or "")
    too_large = len(preview) > MAX_REVIEW_CHARS or len(test) > MAX_REVIEW_CHARS
    out: dict[str, Any] = {k: entry.get(k) for k in _LIGHT}
    out["parameters_schema"] = entry.get("parameters_schema") if isinstance(entry.get("parameters_schema"), dict) else {}
    out["too_large_to_review"] = too_large
    out["module_chars"] = len(preview)
    out["module_lines"] = preview.count("\n") + 1 if preview else 0
    out["test_chars"] = len(test)
    out["preview_sha256"] = _sha(preview)
    out["module_preview"] = "" if too_large else preview
    out["test_source"] = "" if too_large else test
    recorded = entry.get("module_sha256")
    out["module_sha256"] = recorded if isinstance(recorded, str) else None
    out["on_disk_sha256"] = None
    out["on_disk_matches"] = None
    if entry.get("status") == "APPROVED" and out["module_sha256"]:
        from dourmouse.self_extensions import _NAME_RE, _approved_dir

        name = str(entry.get("tool_name") or "")
        path = _approved_dir() / f"{name}.py"
        if _NAME_RE.match(name) and path.is_file():
            out["on_disk_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            out["on_disk_matches"] = out["on_disk_sha256"] == out["module_sha256"]
        else:
            out["on_disk_matches"] = False
    live = _live_names(req.server)
    if entry.get("status") == "APPROVED":
        out["live"] = None if live is None else str(entry.get("tool_name")) in live
    return 200, {"ok": True, "draft": out}


@route("POST", "/api/os/agentsmith/approve")
def approve(req: Request):
    did = _draft_id(req.body.get("id"))
    sha = str(req.body.get("sha256") or "").strip().lower()
    if not _SHA.match(sha):
        raise ApiError(400, "sha256 is required: the hash of the module text the owner read")
    from dourmouse.self_extensions import approve as approve_extension

    entry = _store().get(did)
    if entry is None:
        raise ApiError(404, f"no such draft: {did}")
    preview = str(entry.get("module_preview") or "")
    if len(preview) > MAX_REVIEW_CHARS or len(str(entry.get("test_source") or "")) > MAX_REVIEW_CHARS:
        raise ApiError(409, "this draft is too large to review on this screen, so it cannot be approved here")
    if _sha(preview) != sha:
        raise ApiError(409, "the draft changed after it was opened. Nothing was approved. Open it again and read the current code.")
    result = approve_extension(did)
    if not result.get("ok"):
        raise ApiError(409, str(result.get("error") or "approval failed"))
    return 200, {"ok": True, "entry": {k: (result.get("entry") or {}).get(k) for k in (*_LIGHT, "module_sha256")}, "note": result.get("note", "")}
