"""Phase F1: the core that lets the model drive other Mac apps, with permission.

Design: ~/Documents/DOURMOUSE/F1_APP_DRIVING_DESIGN.md.

* ``backend``: the only macOS-touching code (PyObjC AX, osascript fallback), injectable.
* ``policy``: hard deny list and the owner-only allow list (default: nothing allowed).
* ``safety``: kill switch, driving indicator, typed-text secret check, audit.
* ``driver``: snapshot, re-resolve, click/type/scroll by element id, press_key.
* ``tools``: ToolSpecs for the model (not registered here; the main thread wires them).
"""

from __future__ import annotations

from .driver import act, describe, press_key, running_apps, snapshot, status
from .errors import AppDriverError
from .policy import allow, deny_reason, disallow, is_allowed, list_allowed
from .safety import add_indicator_listener, engage_kill, indicator, is_killed, release_kill, remove_indicator_listener

__all__ = [
    "AppDriverError",
    "act",
    "add_indicator_listener",
    "allow",
    "deny_reason",
    "describe",
    "disallow",
    "engage_kill",
    "indicator",
    "is_allowed",
    "is_killed",
    "list_allowed",
    "press_key",
    "release_kill",
    "remove_indicator_listener",
    "running_apps",
    "snapshot",
    "status",
]
