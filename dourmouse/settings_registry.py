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
            if os.pathsep in str(p):
                # Folders are stored joined by os.pathsep; a name that contains
                # it would read back as two bogus entries.
                raise ValueError(f"{item!r} contains the path separator {os.pathsep!r}, which this setting cannot store")
            paths.append(str(p))
        stored = os.pathsep.join(paths)
    _write_user_setting(key, stored)
    os.environ[key] = stored
    return {"ok": True, "key": key, "value": _current(feature, {}), "restart_required": True}


def reset_features() -> dict[str, Any]:
    """Put the background switches back to their defaults (OS shell SETTINGS,
    finding #150).

    Scope is deliberate and narrow: only the keys in ``FEATURES`` are touched.
    A saved API key, the orchestrator model, the auto-approve flag and every
    other setting in the same file are left exactly as they are.

    The saved line is removed rather than overwritten with the default, so a
    future change to a default reaches this user too. A switch that is also set
    by the real process environment (a variable exported before launch) cannot
    be reset from here: it is listed under ``still_overridden`` so the screen
    can say so instead of claiming a reset that did not happen."""
    saved = config._read_user_config_file()
    cleared: list[str] = []
    still_overridden: list[str] = []
    for feature in FEATURES:
        key = feature["key"]
        if key in saved:
            cleared.append(key)
        env_value = os.environ.get(key)
        if env_value is None:
            continue
        if key in saved and env_value == saved[key]:
            # save_feature() put it in this process's environment; take it out again
            os.environ.pop(key, None)
        else:
            still_overridden.append(key)
    if cleared:
        _remove_user_settings(cleared)
    return {"ok": True, "cleared": cleared, "still_overridden": still_overridden,
            "restart_required": True}


def _update_user_settings(mutate: Any) -> None:
    """Read-modify-write the user's config file under the one shared lock.

    ``mutate`` edits a dict of the saved values; only the keys it added,
    changed or removed are written, line by line, through
    ``config.update_user_config`` (W2R-4, W2R-10): every other line, comment
    included, is kept as the owner wrote it, the file is written atomically
    and privately (0600), two concurrent saves cannot lose one change, and a
    file that EXISTS but cannot be read is left exactly as it is and the save
    is refused (OSError), because reading it as empty and writing that back
    would erase every saved key."""
    with config.config_lock:
        before = config._read_user_config_file()
        values = dict(before)
        mutate(values)
        updates = {k: v for k, v in values.items() if before.get(k) != v}
        removals = [k for k in before if k not in values]
        config.update_user_config(updates, removals)


def _remove_user_settings(keys: list[str]) -> None:
    """Drop these keys from the user's config file, keeping every other line
    and the file private (0600)."""
    def drop(values: dict[str, str]) -> None:
        for key in keys:
            values.pop(key, None)

    _update_user_settings(drop)


def _write_user_setting(key: str, value: str) -> None:
    """Merge one key into the user's config file, keeping every other line
    and the file private (0600)."""
    def put(values: dict[str, str]) -> None:
        values[key] = value

    _update_user_settings(put)
