"""Open a SQLite store without letting one damaged file stop the app (finding #140).

The office log is history: useful, never required to start. A file that is not
a database (a truncated write, a disk that filled) raised out of ``OfficeLogger()``
and the server refused to start, so one bad file took the whole app down. Now
the damaged file is set aside under a dated name, kept for the owner to look at,
and a fresh store starts in its place, with a warning.

Only damage is handled. A database that is merely locked or busy is a normal,
temporary state and a valid file: it is never moved.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")

_DAMAGE_MARKERS = ("not a database", "malformed", "disk image", "corrupt")


def is_damage(exc: BaseException) -> bool:
    """A database that is damaged, as opposed to busy or locked (which say
    "database is locked" and are ordinary, temporary and harmless)."""
    return isinstance(exc, sqlite3.DatabaseError) and any(m in str(exc).lower() for m in _DAMAGE_MARKERS)


def open_with_recovery(factory: Callable[[], T], path: Path, what: str) -> T:
    """``factory()``, and if the file at ``path`` is damaged, set it (and its
    write-ahead files) aside and call ``factory()`` once more on a fresh file."""
    try:
        return factory()
    except sqlite3.DatabaseError as exc:
        if not is_damage(exc):
            raise
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for suffix in ("", "-wal", "-shm"):
            part = Path(str(path) + suffix)
            if part.exists():
                part.rename(part.with_name(f"{part.name}.corrupt-{stamp}"))
        print(
            f"dourmouse: the {what} at {path} was damaged ({exc}); it was moved aside as "
            f"{path.name}.corrupt-{stamp} and a new one was started.",
            file=sys.stderr,
        )
        return factory()
