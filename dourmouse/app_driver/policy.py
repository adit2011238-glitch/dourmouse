"""Which apps the model may drive: a hard deny list and an owner-only allow list.

Every app starts NOT allowed. The owner adds an app through the HTTP route
(``POST /api/os/apps/allow``); the model has no tool that can. The deny list
is code, not configuration: an app on it cannot be allowed, and an entry for
it written into the allow file by hand is still refused at act time.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from .errors import AppDriverError

#: Bundle ids that can never be driven.
DENIED_BUNDLE_IDS = frozenset(
    b.casefold()
    for b in (
        "com.apple.Terminal",
        "com.googlecode.iterm2",
        "dev.warp.Warp-Stable",
        "com.mitchellh.ghostty",
        "org.alacritty",
        "net.kovidgoyal.kitty",
        "com.github.wez.wezterm",
        "co.zeit.hyper",
        "com.microsoft.VSCode",
        "com.todesktop.230313mzl4w4u92",
        "com.apple.dt.Xcode",
        "com.apple.systempreferences",
        "com.apple.Settings",
        "com.apple.keychainaccess",
        "com.apple.Passwords",
        "com.apple.ScriptEditor2",
        "com.apple.Automator",
        "com.apple.shortcuts",
        "com.apple.SecurityAgent",
        "com.apple.loginwindow",
        "com.1password.1password",
        "com.agilebits.onepassword7",
        "com.agilebits.onepassword-osx",
        "com.bitwarden.desktop",
        "com.dashlane.dashlanephonefinal",
        "com.lastpass.lastpassmacdesktop",
        "org.keepassxc.keepassxc",
        "in.sinew.enpass-desktop",
        "me.proton.pass.electron",
        "com.dourmouse.app",
        "com.github.electron",
    )
)

#: Names (case-insensitive, with or without ".app") that can never be driven.
DENIED_NAMES = frozenset(
    n.casefold()
    for n in (
        "Terminal",
        "iTerm",
        "iTerm2",
        "System Settings",
        "System Preferences",
        "Keychain Access",
        "Passwords",
        "Script Editor",
        "Automator",
        "Shortcuts",
        "SecurityAgent",
        "loginwindow",
        "Dourmouse",
        "Electron",
    )
)

#: Name fragments for password managers whose bundle ids vary by edition.
DENIED_NAME_FRAGMENTS = ("1password", "bitwarden", "dashlane", "lastpass", "keepass", "enpass", "proton pass")

_MAX_NAME = 100
_lock = threading.Lock()


def _fold(text: str | None) -> str:
    value = (text or "").strip().casefold()
    return value[:-4] if value.endswith(".app") else value


def deny_reason(name: str | None, bundle_id: str | None = None, pid: int | None = None) -> str | None:
    """Why this app can never be driven, or None if it is not on the deny list."""
    folded_name = _fold(name)
    folded_bundle = (bundle_id or "").strip().casefold()
    if pid is not None and pid in (os.getpid(), os.getppid()):
        return "it is Dourmouse itself (or the process that launched it)"
    if folded_bundle and folded_bundle in DENIED_BUNDLE_IDS:
        return f"{bundle_id} is on the hard deny list"
    if folded_name in DENIED_NAMES:
        return f"{name} is on the hard deny list"
    for fragment in DENIED_NAME_FRAGMENTS:
        if fragment in folded_name or fragment.replace(" ", "") in folded_bundle:
            return f"{name or bundle_id} is a password manager, which is on the hard deny list"
    return None


def deny_list() -> dict[str, list[str]]:
    return {
        "names": sorted(DENIED_NAMES),
        "bundle_ids": sorted(DENIED_BUNDLE_IDS),
        "name_fragments": list(DENIED_NAME_FRAGMENTS),
    }


def allow_file() -> Path:
    from dourmouse.config import user_config_dir

    return user_config_dir() / "app_driver" / "allowed_apps.json"


def _read() -> list[dict[str, Any]]:
    path = allow_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        # A corrupt file means "nothing is allowed", never "everything is".
        raise AppDriverError("failed", f"the allow list at {path} could not be read ({exc}); no app is allowed until it is fixed") from exc
    apps = data.get("apps") if isinstance(data, dict) else None
    if not isinstance(apps, list):
        return []
    return [a for a in apps if isinstance(a, dict) and (a.get("name") or a.get("bundle_id"))]


def _write(apps: list[dict[str, Any]]) -> None:
    path = allow_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 1, "apps": apps}, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def list_allowed() -> list[dict[str, Any]]:
    with _lock:
        return [dict(a) for a in _read()]


def _matches(entry: dict[str, Any], name: str | None, bundle_id: str | None) -> bool:
    entry_bundle = str(entry.get("bundle_id") or "").casefold()
    app_bundle = (bundle_id or "").strip().casefold()
    if entry_bundle and app_bundle:
        # Both sides know the bundle id: it alone decides, so an app that
        # merely renames itself to an allowed name does not inherit the grant.
        return entry_bundle == app_bundle
    return bool(_fold(entry.get("name"))) and _fold(entry.get("name")) == _fold(name)


def is_allowed(name: str | None, bundle_id: str | None = None) -> bool:
    with _lock:
        return any(_matches(e, name, bundle_id) for e in _read())


def check_drivable(name: str | None, bundle_id: str | None, pid: int | None) -> None:
    """Raise unless this app may be driven: not denied, and allowed by the owner."""
    reason = deny_reason(name, bundle_id, pid)
    if reason:
        raise AppDriverError("denied", f"{name or bundle_id} can never be driven: {reason}.")
    if not is_allowed(name, bundle_id):
        raise AppDriverError(
            "not_allowed",
            f"{name or bundle_id} is not on the allow list. Only the owner can allow an app, "
            "from the Apps settings (POST /api/os/apps/allow); the model cannot.",
        )


def allow(name: str, bundle_id: str | None = None, by: str = "") -> dict[str, Any]:
    name = (name or "").strip()
    bundle_id = (bundle_id or "").strip() or None
    if not name and not bundle_id:
        raise AppDriverError("invalid", "an app name or bundle id is required")
    if len(name) > _MAX_NAME or (bundle_id and len(bundle_id) > _MAX_NAME) or not (name + (bundle_id or "")).isprintable():
        raise AppDriverError("invalid", "that app name is not valid")
    reason = deny_reason(name, bundle_id)
    if reason:
        raise AppDriverError("denied", f"{name or bundle_id} cannot be allowed: {reason}.")
    with _lock:
        apps = _read()
        if any(_matches(e, name, bundle_id) for e in apps):
            entry = next(e for e in apps if _matches(e, name, bundle_id))
            if bundle_id and not entry.get("bundle_id"):
                entry["bundle_id"] = bundle_id
                _write(apps)
            return dict(entry)
        entry = {"name": name or bundle_id, "bundle_id": bundle_id, "added_at": time.time(), "added_by": by or "owner"}
        apps.append(entry)
        _write(apps)
        return dict(entry)


def disallow(name: str, bundle_id: str | None = None) -> bool:
    """Remove every entry matching this app. True if anything was removed."""
    with _lock:
        apps = _read()
        keep = [e for e in apps if not (_matches(e, name, bundle_id) or (_fold(e.get("name")) and _fold(e.get("name")) == _fold(name)))]
        if len(keep) == len(apps):
            return False
        _write(keep)
        return True
