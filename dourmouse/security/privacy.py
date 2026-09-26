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
import stat
import tempfile
from pathlib import Path
from typing import Any

from dourmouse.config import user_config_dir


def settings_path() -> Path:
    return user_config_dir() / "security.json"


def _read() -> dict[str, Any]:
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def privacy_mode() -> bool:
    return bool(_read().get("privacy_mode"))


def set_privacy_mode(on: bool) -> dict[str, Any]:
    data = _read()
    data["privacy_mode"] = bool(on)
    atomic_write_text(settings_path(), json.dumps(data, indent=2))
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
