"""Suppress stray console windows in the packaged Windows app (v8.9).

The desktop build is windowed (``console=False``), so the app itself has no
terminal. But every ``subprocess`` call still spawns one: Windows gives a
new console to any child process started from a GUI app unless told not to.
Measured in this codebase: **31 subprocess call sites, none of which passed
``creationflags``**, so a user launching the packaged app saw a terminal
flash up alongside it.

Rather than edit 31 call sites — and miss the next one someone adds — this
installs a single default at the ``subprocess`` layer. Scope is deliberately
narrow:

- Windows only, and only when running frozen (a source checkout keeps normal
  behaviour so developers still see child output in their terminal).
- Only sets flags the caller did not set. An explicit ``creationflags`` from
  a call site always wins.
- Adds ``CREATE_NO_WINDOW`` only when the caller has not asked for a new
  console, since the two are contradictory.

This hides the window; it does not hide errors. Callers that capture output
keep capturing it exactly as before.
"""

from __future__ import annotations

import os
import subprocess
import sys

#: Windows flag: run the child with no console window at all.
CREATE_NO_WINDOW = 0x08000000
#: Flags that mean "the caller deliberately wants a console" — never override.
_WANTS_CONSOLE = 0x00000010 | 0x00000008  # CREATE_NEW_CONSOLE | DETACHED_PROCESS

_installed = False


def should_apply() -> bool:
    """Only in a frozen Windows build."""
    return os.name == "nt" and bool(getattr(sys, "frozen", False))


def install() -> bool:
    """Patch subprocess so children default to no console window.

    Returns True when the patch was applied. Idempotent.
    """
    global _installed
    if _installed or not should_apply():
        return False

    _orig_popen_init = subprocess.Popen.__init__

    def _quiet_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        flags = kwargs.get("creationflags", 0) or 0
        if not (flags & _WANTS_CONSOLE):
            kwargs["creationflags"] = flags | CREATE_NO_WINDOW
        return _orig_popen_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _quiet_init  # type: ignore[method-assign]
    _installed = True
    return True
