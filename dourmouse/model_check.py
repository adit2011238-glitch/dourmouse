"""Verify configured models actually exist on the backend.

Origin: `.env` set DOURMOUSE_FAST_MODEL=qwen3:4b, the model was never pulled
on that machine, and every short question the fast lane handled returned
"404 page not found" in under a second. The configuration was wrong for
months of wall-clock and nothing said so — the mismatch only surfaced as a
bare 404 in chat, which reads like a network fault rather than a missing
model.

The lesson is that configuration naming a model is a *claim*, and a claim
that is cheap to verify should be verified. This module does that: list what
the backend actually has, compare against what config asks for, and let the
caller either warn loudly at startup or substitute a model that exists.

Stdlib only, short timeouts, never raises — a probe that hangs or throws
would be worse than the mismatch it is looking for.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

__all__ = [
    "installed_models",
    "check_configured_models",
    "resolve_available",
    "OLLAMA_DEFAULT",
]

OLLAMA_DEFAULT = "http://127.0.0.1:11434"
_TIMEOUT = 3.0


def ollama_host() -> str:
    """Base URL for the local Ollama daemon, honouring the usual env vars."""
    for var in ("OLLAMA_BASE_URL", "OLLAMA_HOST"):
        raw = os.environ.get(var, "").strip()
        if not raw:
            continue
        # OLLAMA_HOST is often bare host:port; OLLAMA_BASE_URL often ends /v1.
        if not raw.startswith(("http://", "https://")):
            raw = "http://" + raw
        return raw.rstrip("/").removesuffix("/v1")
    return OLLAMA_DEFAULT


def installed_models(host: str | None = None) -> list[str] | None:
    """Names Ollama reports as installed.

    Returns None when the daemon cannot be reached — deliberately distinct
    from an empty list, which means "reachable, nothing installed". Callers
    must not treat unreachable as "model missing", or a stopped daemon would
    trigger a spurious model swap.
    """
    base = (host or ollama_host()).rstrip("/")
    try:
        req = urllib.request.Request(f"{base}/api/tags")
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None
    models = payload.get("models")
    if not isinstance(models, list):
        return None
    out: list[str] = []
    for entry in models:
        if isinstance(entry, dict):
            name = entry.get("name") or entry.get("model")
            if isinstance(name, str) and name:
                out.append(name)
    return out


def _matches(wanted: str, available: list[str]) -> bool:
    """Whether `wanted` is present, tolerating the implicit :latest tag."""
    if wanted in available:
        return True
    if ":" not in wanted:
        return any(a.split(":", 1)[0] == wanted for a in available)
    return False


def check_configured_models(
    configured: dict[str, str], *, host: str | None = None
) -> dict[str, Any]:
    """Compare configured model names against what is installed.

    `configured` maps a human label ("fast lane", "primary") to a model name.
    The result carries `reachable`, the installed list, and any `missing`
    entries, so a caller can print one actionable line at startup.
    """
    available = installed_models(host)
    if available is None:
        return {
            "reachable": False,
            "installed": [],
            "missing": {},
            "detail": "Ollama not reachable; model names not verified.",
        }
    missing = {
        label: name
        for label, name in configured.items()
        if name and not _matches(name, available)
    }
    return {
        "reachable": True,
        "installed": available,
        "missing": missing,
        "detail": (
            "all configured models present"
            if not missing
            else "configured models not installed: "
            + ", ".join(f"{label}={name!r}" for label, name in missing.items())
        ),
    }


def resolve_available(
    wanted: str, *, prefer: list[str] | None = None, host: str | None = None
) -> tuple[str, str | None]:
    """Return a model that exists, plus a note when a substitution happened.

    Substitution order: the wanted model, then each `prefer` candidate, then
    any installed model. If the daemon is unreachable the wanted name is
    returned unchanged — an unreachable probe is not evidence of absence, and
    silently switching models because a health check timed out would be a
    worse failure than the one being prevented.
    """
    available = installed_models(host)
    if available is None:
        return wanted, None
    if _matches(wanted, available):
        return wanted, None
    for candidate in prefer or []:
        if _matches(candidate, available):
            return candidate, (
                f"{wanted!r} is not installed; using {candidate!r} instead. "
                f"Run 'ollama pull {wanted}' to restore the configured model."
            )
    if available:
        return available[0], (
            f"{wanted!r} is not installed; falling back to {available[0]!r}. "
            f"Run 'ollama pull {wanted}' to restore the configured model."
        )
    return wanted, f"{wanted!r} is not installed and no other model is available."
