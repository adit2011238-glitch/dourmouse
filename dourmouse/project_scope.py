"""Per-project scope for a chat turn (OS-6, finding #118).

A project on the bookshelf already had its own folder
(~/Documents/Dourmouse Projects/<name>) and its own chat. But the file and
coding tools were sandboxed to Dourmouse's global workspace, so a project's
chat was told "work in this folder" and then could not write there. While a
project's turn runs, this scope makes the sandboxed tools (read/write/list
files, run_python, code search) use the project folder as their root. The
scope travels with the turn: parallel delegate branches copy it (see
delegate_parallel), and it is cleared when the request ends.
"""

from __future__ import annotations

import contextvars
import threading
from pathlib import Path
from typing import Any

_current: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("dourmouse_project", default=None)
_tokens = threading.local()


def enter(project: dict[str, Any] | None) -> None:
    """Scope the rest of this request's thread to ``project`` (None: no
    project). Paired with ``leave()`` in the request's finally."""
    _tokens.token = _current.set(project if project and project.get("path") else None)


def leave() -> None:
    token = getattr(_tokens, "token", None)
    if token is not None:
        try:
            _current.reset(token)
        except ValueError:  # set in a different context; clear instead
            _current.set(None)
        _tokens.token = None


def current_project() -> dict[str, Any] | None:
    return _current.get()


def project_root() -> Path | None:
    """The folder a project's tools work in, created if it vanished; None
    outside a project."""
    p = _current.get()
    if not p:
        return None
    root = Path(str(p["path"])).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root
