"""Privacy mode (MS-12, spec item 42; finding #112).

When it is on, security data about this Mac does not leave it: the AI
analyst does not send findings to the cloud model, and the browser-history
tool gives counts and sources only, never URLs or search terms, to the chat
(whose model is in the cloud). Everything local keeps working: detection,
the report, lockdown, response actions. Stored in the user config dir,
read fresh on every use, so switching it needs no restart.
"""

from __future__ import annotations

import json
import stat
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
    p = settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    p.chmod(0o600)
    return {"privacy_mode": bool(on)}


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
