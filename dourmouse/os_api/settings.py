"""Backend for the OS shell's SETTINGS screen (finding #150).

One read (``summary``) gathers what the screen shows, and four writes change a
setting. Every value is a real read: the bind host comes from the running
socket, the token state from the server, the models from the loaded config, the
switches from the config file. A section that cannot be read says so in its own
``error`` field; one broken section never blanks the others, and nothing is
invented to fill it.

The writes are the owner's own click behind a confirm card on the screen (the
same model as ``/api/security/action``). They reuse the existing savers in
``config`` and ``settings_registry`` so the file format is unchanged, but every
answer here is a real HTTP status: the old routes report a failed write as a
200 with ``ok: false``.

Secrets: an API key's value is never returned, only whether one is set and
where it comes from. The reset touches the background switches only.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

from . import ApiError, Request, route

_LOOPBACK = ("127.0.0.1", "::1", "localhost")
SHELL_VALUES = ("auto", "electron", "pywebview")
SHELL_KEY = "DOURMOUSE_SHELL"

#: The switches that change how the server behaves without a restart. Each id
#: maps to the existing getter and saver in ``config``. ``danger`` marks the one
#: that removes a safety gate; the screen puts it behind a stronger confirm.
_TOGGLES: list[dict[str, Any]] = [
    {"id": "auto_approve", "getter": "auto_approve_enabled", "saver": "save_auto_approve_setting",
     "label": "Auto approve", "danger": True, "default": False,
     "help": "Skips the confirmation card for every gated tool, so the model can send mail, run commands and change files without asking. Takes effect at once. Off is the safe default."},
    {"id": "grounded_mode", "getter": "grounded_mode_enabled", "saver": "save_grounded_mode_setting",
     "label": "Grounded mode", "danger": False, "default": False,
     "help": "When a pinned agent answers without using any of its tools, ask it once to call a tool or say plainly why none was needed."},
    {"id": "app_control_dry_run", "getter": "app_control_dry_run_enabled", "saver": "save_app_control_dry_run_setting",
     "label": "App control dry run", "danger": False, "default": False,
     "help": "Show what app control would click or type without doing it. Confirmation is still required either way."},
    {"id": "claude_front_mode", "getter": "claude_front_mode_enabled", "saver": "save_claude_front_mode_setting",
     "label": "Claude front mode", "danger": False, "default": True,
     "help": "Claude is the front end for every tab; lighter work is split across Ollama and Gemini behind the scenes. On by default."},
    {"id": "google_full_scopes", "getter": "google_oauth_full_scopes_enabled", "saver": "save_google_oauth_full_scopes_setting",
     "label": "Google full access", "danger": False, "default": False,
     "help": "Ask Google for Gmail, Calendar and Drive access at sign-in, not only identity. Google's restricted scopes can fail for an app it has not verified."},
]
_TOGGLE_BY_ID = {t["id"]: t for t in _TOGGLES}


def _guard(fn) -> dict[str, Any]:
    """Run one section reader; a failure becomes that section's own error."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - shown on the screen, never hidden or turned into a guess
        return {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}


def _electron_available() -> bool:
    from dourmouse.desktop import _electron_shell_argv

    return _electron_shell_argv() is not None


def _shell_section() -> dict[str, Any]:
    raw = os.environ.get(SHELL_KEY, "auto").strip().lower() or "auto"
    valid = raw in SHELL_VALUES
    requested = raw if valid else "auto"
    available = _electron_available()
    effective = "electron" if requested != "pywebview" and available else "pywebview"
    return {
        "requested": requested,
        "invalid_value": None if valid else raw[:40],
        "electron_available": available,
        "effective_next_launch": effective,
        "restart_required": True,
    }


def _access_section(server: Any) -> dict[str, Any]:
    host, port = server.server_address[0], server.server_address[1]
    override = os.environ.get("DOURMOUSE_ALLOW_INSECURE_BIND", "").strip().lower() not in ("", "0", "false", "no", "off")
    return {
        "host": host,
        "port": port,
        "loopback": host in _LOOPBACK,
        "token_gate": bool(getattr(server, "access_token", "")),
        "insecure_override": override,
    }


def _models_section(server: Any) -> dict[str, Any]:
    from dourmouse.webui import _backend_label, _effective_model

    cfg = server.config
    base_url = getattr(cfg, "base_url", None) if cfg is not None else None
    host = (urlparse(base_url).hostname or "") if base_url else ""
    return {
        "backend": _backend_label(cfg),
        "model": getattr(cfg, "model", None) if cfg is not None else None,
        "base_url": base_url,
        "local_active": host in _LOOPBACK,
        "researcher": {"agent": "research_info", "model": _effective_model(cfg, "research_info")},
    }


def _keys_section() -> dict[str, Any]:
    """Whether each key is set and where from. Never the value."""
    from dourmouse.config import BYOK_API_KEY_NAMES, api_key_setting

    out: dict[str, Any] = {}
    for name in BYOK_API_KEY_NAMES:
        if api_key_setting(name):
            out[name] = "saved"
        elif os.environ.get(name, "").strip():
            out[name] = "environment"
        else:
            out[name] = "none"
    return out


def _toggles_section() -> list[dict[str, Any]]:
    from dourmouse import config

    rows = []
    for t in _TOGGLES:
        row = {k: t[k] for k in ("id", "label", "help", "danger", "default")}
        try:
            row["value"] = bool(getattr(config, t["getter"])())
        except Exception as exc:  # noqa: BLE001 - this row says why, the others still show
            row["value"] = None
            row["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        rows.append(row)
    return rows


def _features_section() -> list[dict[str, Any]]:
    from dourmouse.settings_registry import feature_settings

    return feature_settings()


@route("GET", "/api/os/settings/summary")
def summary(req: Request):
    return 200, {
        "ok": True,
        "shell": _guard(_shell_section),
        "access": _guard(lambda: _access_section(req.server)),
        "models": _guard(lambda: _models_section(req.server)),
        "keys": _guard(_keys_section),
        "toggles": _guard(lambda: {"items": _toggles_section()}),
        "features": _guard(lambda: {"items": _features_section()}),
    }


def _bool_of(value: Any, what: str) -> bool:
    if not isinstance(value, bool):
        raise ApiError(400, f"{what} must be true or false")
    return value


@route("POST", "/api/os/settings/toggle")
def set_toggle(req: Request):
    """Body ``{id, enabled}``. Only the ids listed above are accepted."""
    from dourmouse import config

    toggle = _TOGGLE_BY_ID.get(str(req.body.get("id") or ""))
    if toggle is None:
        raise ApiError(400, f"{str(req.body.get('id'))[:60]!r} is not a setting this screen can change")
    enabled = _bool_of(req.body.get("enabled"), toggle["label"])
    result = getattr(config, toggle["saver"])(enabled)
    if not result.get("ok"):
        raise ApiError(500, str(result.get("detail") or "the setting could not be saved"))
    return 200, {"ok": True, "id": toggle["id"], "enabled": bool(getattr(config, toggle["getter"])())}


@route("POST", "/api/os/settings/feature")
def set_feature(req: Request):
    """Body ``{key, value}`` for one background switch. Applies at next launch."""
    from dourmouse.settings_registry import save_feature

    key = str(req.body.get("key") or "")
    try:
        result = save_feature(key, req.body.get("value"))
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc
    except OSError as exc:
        raise ApiError(500, f"could not write the settings file: {exc}") from exc
    return 200, result


@route("POST", "/api/os/settings/shell")
def set_shell(req: Request):
    """Body ``{value: auto|electron|pywebview}``. Read at the next launch."""
    from dourmouse.settings_registry import _write_user_setting

    value = str(req.body.get("value") or "").strip().lower()
    if value not in SHELL_VALUES:
        raise ApiError(400, "the shell must be auto, electron or pywebview")
    if value == "electron" and not _electron_available():
        raise ApiError(400, "the Electron shell is not installed in this checkout (electron/node_modules is missing). Run npm install in the electron folder first.")
    try:
        _write_user_setting(SHELL_KEY, value)
    except OSError as exc:
        raise ApiError(500, f"could not write the settings file: {exc}") from exc
    os.environ[SHELL_KEY] = value
    return 200, {"ok": True, **_shell_section()}


@route("POST", "/api/os/settings/reset")
def reset(req: Request):
    """Reset the background switches to their defaults. The body must say
    ``{"scope": "features"}``: anything else is refused, so a request cannot
    reach further than the screen's own confirm card promised."""
    from dourmouse.settings_registry import reset_features

    if req.body.get("scope") != "features":
        raise ApiError(400, 'reset only covers the background switches: send {"scope": "features"}')
    try:
        return 200, reset_features()
    except OSError as exc:
        raise ApiError(500, f"could not write the settings file: {exc}") from exc
