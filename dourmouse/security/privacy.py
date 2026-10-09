"""Privacy mode (MS-12, spec item 42; finding #112).

When it is on, security evidence about this Mac does not leave it: the AI
analyst does not send findings to the cloud model, the browser-history tool
gives counts and sources only, and every chat tool that returns evidence
(findings, download sources, hostnames, process and connection lists, file
names, the report) answers with a plain "withheld" note instead, because the
chat model is in the cloud. The console and everything local keep working:
detection, the report, lockdown, response actions. Stored in the user config
dir, read fresh on every use, so switching it needs no restart.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

from dourmouse.config import user_config_dir


def settings_path() -> Path:
    return user_config_dir() / "security.json"


def _load() -> tuple[dict[str, Any], str]:
    """(settings, state); state is "ok", "missing" (never written: the default,
    off) or "unusable" (present but unreadable or not a JSON object)."""
    try:
        text = settings_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, "missing"
    except OSError:
        return {}, "unusable"
    try:
        data = json.loads(text)
    except ValueError:
        return {}, "unusable"
    return (data, "ok") if isinstance(data, dict) else ({}, "unusable")


def privacy_mode() -> bool:
    """On when switched on, and also when the file that says so cannot be
    read: the guarantee is that evidence stays on this Mac, so a damaged
    setting must not turn it off (finding P5-52)."""
    data, state = _load()
    return state == "unusable" or bool(data.get("privacy_mode"))


def set_privacy_mode(on: bool) -> dict[str, Any]:
    data, state = _load()
    path = settings_path()
    if state == "unusable" and path.exists():
        # Keep what could not be parsed instead of silently dropping it.
        with contextlib.suppress(OSError):
            shutil.copy2(path, path.with_name(path.name + ".corrupt"))
    data["privacy_mode"] = bool(on)
    atomic_write_text(path, json.dumps(data, indent=2))
    return {"privacy_mode": bool(on)}


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    """Write a file so a reader sees the old content or the new content,
    never a truncated half: a temp file in the same folder, flushed to disk,
    then renamed over the target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


class PrivacyModeOn(Exception):
    """Raised instead of sending security data off this Mac."""

    def __init__(self, what: str) -> None:
        super().__init__(f"privacy mode is on: {what} stays on this Mac (turn privacy mode off to allow it)")


def private_dir(path: Path) -> Path:
    """Create a security folder readable by the owner only (finding #112:
    the self-audit found the security workspace world-listable), and
    tighten the security root above it too."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    from dourmouse.config import workspace_dir

    root = workspace_dir() / "security"
    for p in {path, root}:
        if p.exists() and stat.S_IMODE(p.stat().st_mode) & 0o077:
            p.chmod(0o700)
    return path
