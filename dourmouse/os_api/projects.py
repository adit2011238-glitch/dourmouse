"""Backend for the OS shell's PROJECTS screen (finding #150).

Almost everything PROJECTS needs already exists in ``project_bookkeeper``:
listing, opening (which returns the project's own chat tab id) and stopping
tracking are served by ``/api/projects/bookkeeper``, ``/open`` and ``/delete``.
Three things were missing for an honest screen:

* a read that says so when the bookkeeper fails, instead of an empty shelf
  with a hidden ``error`` field (the old route answers 200 either way);
* a way to show the owner the exact folder a new project will create BEFORE it
  is created, so the confirm card can state it;
* a create that bounds the name and description and turns a filesystem error
  into an honest refusal, rather than an unhandled 500.

Nothing here touches a folder that already exists, and "stop tracking" never
deletes anything (``project_bookkeeper.delete_project`` says the same).
"""

from __future__ import annotations

import re
from typing import Any

from . import ApiError, Request, route

NAME_MAX = 120
DESCRIPTION_MAX = 2000
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _clean_name(raw: Any) -> str:
    if not isinstance(raw, str):
        raise ApiError(400, "name must be text")
    name = raw.strip()
    if not name:
        raise ApiError(400, "name is required")
    if len(name) > NAME_MAX:
        raise ApiError(400, f"name is too long ({len(name)} characters, the limit is {NAME_MAX})")
    if _CONTROL.search(name):
        raise ApiError(400, "name must not contain control characters")
    return name


def _clean_description(raw: Any) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ApiError(400, "description must be text")
    text = raw.strip()
    if len(text) > DESCRIPTION_MAX:
        raise ApiError(400, f"description is too long ({len(text)} characters, the limit is {DESCRIPTION_MAX})")
    return text


@route("GET", "/api/os/projects/list")
def project_list(req: Request):
    """The persisted bookshelf. A failure is a real error, never an empty list."""
    from dourmouse.project_bookkeeper import get_bookkeeper, project_tab_id

    try:
        view = get_bookkeeper()
        # the chat tab a project's thread lives under, so the screen can tell
        # which project is the active scope without guessing from a name
        view["projects"] = [{**p, "tab_id": project_tab_id(p["path"])} for p in view.get("projects", []) if p.get("path")]
    except Exception as exc:  # noqa: BLE001 - reported to the screen in the caller's words
        raise ApiError(500, f"could not read the project history: {type(exc).__name__}: {str(exc)[:200]}") from exc
    return 200, {"ok": True, **view}


@route("GET", "/api/os/projects/plan")
def project_plan(req: Request):
    """Where a project with this name would be created, and nothing else.
    Creates nothing: the folder appears only when ``create`` is called."""
    from dourmouse.project_bookkeeper import _default_project_path

    name = _clean_name(req.arg("name"))
    path = _default_project_path(name)
    return 200, {"ok": True, "name": name, "path": str(path), "creates_folder": True}


@route("POST", "/api/os/projects/create")
def project_create(req: Request):
    """Create a project under ~/Documents/Dourmouse Projects/<name>.

    The owner's click plus the confirm card is the consent. There is no
    ``path`` argument on purpose: the screen never records or creates a folder
    somewhere the owner did not see in the card."""
    from dourmouse.project_bookkeeper import create_project

    name = _clean_name(req.body.get("name"))
    description = _clean_description(req.body.get("description"))
    if "path" in req.body and req.body.get("path"):
        raise ApiError(400, "this route creates the folder itself; a custom path is not accepted")
    try:
        record = create_project(name=name, description=description)
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc
    except OSError as exc:
        raise ApiError(500, f"could not create the project folder: {exc}") from exc
    return 200, {"ok": True, "project": record}
