"""Backend routes for the OS shell's screens (finding #143).

The mockup shows features that no endpoint backed: pausing a goal, starting a
new one, archiving a message, creating a project, and so on. Each screen's
backend now lives in its own module in this package and registers its routes
with ``@route``, so several people can add endpoints at once without editing
``webui.py`` (one hot file of eight thousand lines). ``webui`` has one hook per
method that asks :func:`find` for a handler after the auth gate and the request
guard, so every route here is already behind both.

A handler takes a :class:`Request` and returns ``(status, payload)``. It may
raise :class:`ApiError` for an expected refusal. Anything else becomes an
honest 500 with the exception's message, never a fabricated success.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any


class ApiError(Exception):
    """An expected refusal: the message goes back to the caller verbatim."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Request:
    server: Any
    query: Mapping[str, list[str]] = field(default_factory=dict)
    body: dict[str, Any] = field(default_factory=dict)
    user: str | None = None

    def arg(self, name: str, default: str = "") -> str:
        values = self.query.get(name) or []
        return values[0] if values else default

    def need(self, name: str) -> str:
        value = self.arg(name).strip()
        if not value:
            raise ApiError(400, f"{name} is required")
        return value


Handler = Callable[[Request], tuple[int, dict[str, Any]]]

_ROUTES: dict[tuple[str, str], Handler] = {}
_loaded = False


def route(method: str, path: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        _ROUTES[(method.upper(), path)] = fn
        return fn

    return register


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda i: i.name):
        importlib.import_module(f"{__name__}.{info.name}")


def find(method: str, path: str) -> Handler | None:
    _load()
    return _ROUTES.get((method.upper(), path))


def routes() -> list[tuple[str, str]]:
    _load()
    return sorted(_ROUTES)
