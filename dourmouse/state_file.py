"""Small helpers for state files that must never be replaced by an empty default after a failed read.

The defect class (fix wave FD1): ``try: data = json.load(f) except: data = []`` followed later by a write of
``data`` puts an empty list over the real file the moment it is unreadable (truncated by a crash, hand-edited
badly). The pattern here is: keep the unreadable file aside under a dated name, never overwrite it, and tell the
caller so the failure is reported rather than hidden.

* ``set_aside(path)`` renames an unreadable file to ``<name>.corrupt-<utc stamp>`` and returns the new path.
* ``load_json_or_aside(path, kind)`` returns ``(value, note)``: the parsed value when it is a ``kind``, ``None``
  when the file is simply absent, and when the file was read but is bad JSON or the wrong shape it is set
  aside, ``value`` is ``None`` and ``note`` says where it went. A file that cannot be READ at all right now
  (permission, I/O error) is not corrupt: it is never moved, and ``StateUnreadable`` is raised so the caller
  refuses its write instead of starting from an empty value.
* ``atomic_write_text(path, text, mode=None)`` writes a temp file in the same directory and ``os.replace``s it,
  so a crash leaves either the old file or the new one, never a truncated one.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


class StateUnreadable(OSError):
    """The state file exists but could not be read right now (permission, a sync client's lock, an I/O error).
    It was left exactly where it is; the caller must not write an empty replacement over it."""


def set_aside(path: Path) -> Path:
    """Rename ``path`` to ``<name>.corrupt-<utc stamp>`` (never onto an existing file) and return the new path."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    aside = path.with_name(f"{path.name}.corrupt-{stamp}")
    n = 1
    while aside.exists():
        aside = path.with_name(f"{path.name}.corrupt-{stamp}-{n}")
        n += 1
    os.replace(path, aside)
    return aside


def load_json_or_aside(path: Path, kind: type) -> tuple[Any, str]:
    """``(value, note)``. Absent file: ``(None, "")``. Readable and a ``kind``: ``(value, "")``.
    Bad JSON or the wrong shape: the file is kept aside, ``(None, note)`` names where. Not readable at all:
    raises ``StateUnreadable`` and the file is not touched."""
    path = Path(path)
    if not path.exists():
        return None, ""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:  # read fine, but the bytes are not text: corrupt
        text, reason = None, f"{type(exc).__name__}: {exc}"
    except OSError as exc:
        raise StateUnreadable(
            f"{path.name} could not be read right now ({type(exc).__name__}: {exc}); it was left untouched."
        ) from exc
    if text is not None:
        try:
            value = json.loads(text)
            if isinstance(value, kind):
                return value, ""
            reason = f"it holds a {type(value).__name__}, not a {kind.__name__}"
        except ValueError as exc:
            reason = f"{type(exc).__name__}: {exc}"
    try:
        aside = set_aside(path)
    except OSError as exc:
        return None, f"{path.name} could not be read ({reason}) and could not be moved aside ({exc}); left untouched."
    return None, f"{path.name} could not be read ({reason}); it was kept as {aside.name} and not overwritten."


def atomic_write_text(path: Path, text: str, mode: int | None = None) -> None:
    """Write ``text`` to ``path`` through a temp file in the same directory and ``os.replace``.
    ``mode`` (for example 0o600) is applied to the temp file before it is moved into place."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
