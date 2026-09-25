"""Every background switch, settable from the console (OS-8.3, finding #120).

The owner's bar for a settings surface: nobody should need a terminal or a
config file to change how Dourmouse behaves. The runtimes added for the Mac
security plan and the OS work (continuous scans, the network watcher, the
AI analyst, the downloads watcher, the lockdown enforcer, standing agents,
the librarian and its folders) were switched only by environment variables.
This registry lists each one with a plain-English description, reads its
current value, and saves a change to the user's own config file (the same
0600 file the other settings use), so it survives restarts.

A runtime is started when the server starts, so a change to one takes
effect at the next launch; each entry says so rather than pretending to
apply live.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dourmouse import config

FEATURES: list[dict[str, Any]] = [
    {"key": "DOURMOUSE_SECURITY_SENTRY_LOOP", "section": "security", "kind": "bool", "default": True,
     "label": "Continuous security scans",
     "help": "Scan this Mac's network, ports, startup items and protections every few minutes."},
    {"key": "DOURMOUSE_NETWATCH", "section": "security", "kind": "bool", "default": True,
     "label": "Scan when the network changes",
     "help": "Run a scan the moment you join a different network instead of waiting for the next one."},
    {"key": "DOURMOUSE_SECURITY_ANALYST", "section": "security", "kind": "bool", "default": True,
     "label": "AI security analyst",
     "help": "Explain new medium and high findings in plain English using the cloud model. Privacy mode also stops it."},
    {"key": "DOURMOUSE_DOWNLOADS_WATCH", "section": "security", "kind": "bool", "default": True,
     "label": "Check new downloads",
     "help": "Assess every file that lands in Downloads (real type, origin, signature, Gatekeeper)."},
    {"key": "DOURMOUSE_LOCKDOWN_ENFORCER", "section": "security", "kind": "bool", "default": True,
     "label": "Lockdown app blocking",
     "help": "While a lockdown is on, close blocklisted apps the moment they open."},
    {"key": "DOURMOUSE_STANDING_AGENTS", "section": "agents", "kind": "bool", "default": True,
     "label": "Agents that work without being asked",
     "help": "Let standing agents (the file librarian) run in the background. They can only read and suggest."},
    {"key": "DOURMOUSE_LIBRARIAN", "section": "agents", "kind": "bool", "default": True,
     "label": "File librarian",
     "help": "Keep an index of your files so you and other agents can find things, and suggest tidying."},
    {"key": "DOURMOUSE_LIBRARIAN_ROOTS", "section": "agents", "kind": "paths", "default": "",
     "label": "Folders the librarian may read",
     "help": "Leave empty for Documents, Desktop and Downloads. Otherwise list folders, one per line."},
]
_BY_KEY = {f["key"]: f for f in FEATURES}
_FALSE = ("0", "false", "no", "off")


def _current(feature: dict[str, Any], saved: dict[str, str]) -> Any:
    raw = os.environ.get(feature["key"], saved.get(feature["key"]))
    if feature["kind"] == "bool":
        return feature["default"] if raw is None or raw.strip() == "" else raw.strip().lower() not in _FALSE
    return [p for p in (raw or "").split(os.pathsep) if p.strip()]


def feature_settings() -> list[dict[str, Any]]:
    saved = config._read_user_config_file()
    return [{**f, "value": _current(f, saved), "restart_required": True} for f in FEATURES]


def save_feature(key: str, value: Any) -> dict[str, Any]:
    feature = _BY_KEY.get(key)
    if feature is None:
        raise ValueError(f"{key!r} is not a setting this screen can change")
    if feature["kind"] == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{feature['label']} must be on or off")
        stored = "1" if value else "0"
    else:
        items = value if isinstance(value, list) else str(value or "").splitlines()
        paths = []
        for item in items:
            item = str(item).strip()
            if not item:
                continue
            p = Path(item).expanduser()
            if not p.is_absolute():
                raise ValueError(f"{item!r} is not a full folder path")
            if not p.is_dir():
                raise ValueError(f"{item!r} is not a folder on this Mac")
            paths.append(str(p))
        stored = os.pathsep.join(paths)
    _write_user_setting(key, stored)
    os.environ[key] = stored
    return {"ok": True, "key": key, "value": _current(feature, {}), "restart_required": True}


def _write_user_setting(key: str, value: str) -> None:
    """Merge one key into the user's config file, keeping every other line
    and the file private (0600)."""
    path = config.user_env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    values = config._read_user_config_file()
    values[key] = value
    body = ["# Dourmouse configuration: written by first-run setup and Settings.",
            "# This file holds credentials. Keep it to yourself; it is never",
            "# bundled into a build or uploaded anywhere.", ""]
    body += [f"{k}={v}" for k, v in sorted(values.items())]
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    path.chmod(0o600)
