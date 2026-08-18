"""First-run setup for an INSTALLED Dourmouse (v8.9).

Why this exists: the packaged app deliberately ships NO ``.env`` — bundling
one would hand the builder's own NVIDIA and brokerage keys to everyone who
installs it. Without a setup flow that leaves a freshly installed app that
looks alive and answers nothing, which is exactly the failure this module
closes.

Design decisions, in order of importance:

- **Local first.** Ollama is a complete, valid configuration with no key,
  no account and no cloud. Setup defaults to it so a new install works
  immediately, offline, with nothing typed.
- **Then guide, don't gate.** The NVIDIA free tier is much faster, so setup
  walks the user through obtaining a key step by step and VALIDATES it
  against the real API before saving. A wrong key is reported at setup
  time, not discovered later as silence.
- **Never fabricate readiness.** Detection performs real probes. If Ollama
  is not running we say so; we never report a backend as available because
  it is merely configured.
- **Config outside the app directory** so an update or reinstall cannot
  erase it (see ``config.user_env_path``).

Honest limitation: keys are written to a user-only ``.env`` rather than the
OS credential store. That is a real weakness — a local attacker reading the
user's own profile can read it — and is tracked as the next hardening step.
It is NOT presented to the user as secure storage.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from dourmouse.config import user_config_dir, user_env_path

_OLLAMA_URL = "http://127.0.0.1:11434/api/tags"
# Validation MUST hit an endpoint that actually checks the key. Measured:
# GET /v1/models returns HTTP 200 for an obviously fake key (it serves the
# public model catalogue without auth), so validating against it reported
# "key works" for `nvapi-0000...` — a false green that would send the user
# away believing a broken key was fine. POST /v1/chat/completions returns
# 403 for the same fake key, so a one-token completion is the real check.
_NVIDIA_CHAT = "https://integrate.api.nvidia.com/v1/chat/completions"
_NVIDIA_PROBE_MODEL = "nvidia/nemotron-3-super-120b-a12b"
_TIMEOUT = 6.0

#: Where a user actually gets a free NVIDIA key. Surfaced in the UI so the
#: walkthrough links to the real page rather than describing it vaguely.
NVIDIA_SIGNUP_URL = "https://build.nvidia.com"


def detect_ollama() -> dict[str, Any]:
    """Real probe of a local Ollama server. Never guesses."""
    try:
        with urllib.request.urlopen(_OLLAMA_URL, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.URLError as exc:
        return {
            "ok": False,
            "detail": "not running on 127.0.0.1:11434",
            "hint": "Install Ollama from ollama.com, then run: ollama pull qwen2.5:7b",
            "error": str(getattr(exc, "reason", exc))[:120],
        }
    except Exception as exc:  # noqa: BLE001 - a probe must never crash setup
        return {"ok": False, "detail": "probe failed", "error": str(exc)[:120]}

    models = [m.get("name", "") for m in (data.get("models") or []) if m.get("name")]
    if not models:
        return {
            "ok": False,
            "detail": "Ollama is running but has no models installed",
            "hint": "Run: ollama pull qwen2.5:7b",
            "models": [],
        }
    return {
        "ok": True,
        "detail": f"{len(models)} model(s) available",
        "models": models[:12],
    }


def validate_nvidia_key(api_key: str) -> dict[str, Any]:
    """Check a key against the REAL NVIDIA API before we save it.

    Saving an unvalidated key is how a user ends up with a silent, broken
    install; the whole point of this step is that a bad key fails loudly
    here instead.
    """
    key = (api_key or "").strip()
    if not key:
        return {"ok": False, "detail": "no key entered"}
    if not key.startswith("nvapi-"):
        return {
            "ok": False,
            "detail": "that does not look like an NVIDIA key",
            "hint": "NVIDIA keys begin with 'nvapi-'. Copy the whole value.",
        }
    payload = {
        "model": _NVIDIA_PROBE_MODEL,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }
    req = urllib.request.Request(
        _NVIDIA_CHAT,
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT + 14) as resp:
            json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return {"ok": False, "detail": f"NVIDIA rejected that key ({exc.code})",
                    "hint": "Check you copied the whole key, including 'nvapi-'."}
        if exc.code == 429:
            return {"ok": False, "detail": "rate limited — the key may be valid",
                    "hint": "Wait a moment and check again."}
        return {"ok": False, "detail": f"NVIDIA returned HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": "could not reach NVIDIA",
                "error": str(exc)[:120],
                "hint": "Check your internet connection and try again."}
    return {"ok": True, "detail": "key works — verified with a live request"}


def probe_node(url: str) -> dict[str, Any]:
    """Check a user-owned compute node actually answers."""
    target = (url or "").strip().rstrip("/")
    if not target:
        return {"ok": False, "detail": "no address entered"}
    if not target.startswith(("http://", "https://")):
        target = "http://" + target
    try:
        with urllib.request.urlopen(target + "/v1/status", timeout=_TIMEOUT) as resp:
            json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": "no answer from that address",
                "error": str(exc)[:120]}
    return {"ok": True, "detail": f"node reachable at {target}"}


def setup_status() -> dict[str, Any]:
    """What setup should show on open — all probes are real."""
    from dourmouse.config import is_configured

    return {
        "configured": is_configured(),
        "config_path": str(user_env_path()),
        "ollama": detect_ollama(),
        "nvidia_signup_url": NVIDIA_SIGNUP_URL,
        "has_nvidia_key": bool(os.environ.get("NVIDIA_API_KEY", "").strip()),
    }


#: Only these keys may be written by setup. An allowlist, so a malformed or
#: hostile payload cannot inject arbitrary environment variables into the
#: user's config file.
_ALLOWED = {
    "DOURMOUSE_LLM_BACKEND",
    "NVIDIA_API_KEY",
    "OLLAMA_MODEL",
    "DOURMOUSE_SERVER_URL",
    "DOURMOUSE_SETUP_DONE",
}


def save_config(values: dict[str, str]) -> dict[str, Any]:
    """Write the chosen configuration to the user's config file.

    Merges with anything already present so re-running setup never silently
    drops settings the user configured elsewhere.
    """
    clean = {
        k: str(v).strip()
        for k, v in (values or {}).items()
        if k in _ALLOWED and str(v).strip()
    }
    if not clean:
        return {"ok": False, "detail": "nothing to save"}

    path = user_env_path()
    try:
        user_config_dir().mkdir(parents=True, exist_ok=True)
        existing: dict[str, str] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                existing[k.strip()] = v.strip()
        existing.update(clean)
        existing["DOURMOUSE_SETUP_DONE"] = "1"

        body = [
            "# Dourmouse configuration — written by first-run setup.",
            "# This file holds credentials. Keep it to yourself; it is never",
            "# bundled into a build or uploaded anywhere.",
            "",
        ]
        body += [f"{k}={v}" for k, v in sorted(existing.items())]
        path.write_text("\n".join(body) + "\n", encoding="utf-8")
        # Best-effort: make it user-only. Windows inherits profile ACLs.
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as exc:
        return {"ok": False, "detail": f"could not write config: {exc}"}

    # Update the live environment too, but do NOT pretend this is sufficient.
    for k, v in clean.items():
        os.environ[k] = v
    os.environ["DOURMOUSE_SETUP_DONE"] = "1"
    return {
        "ok": True,
        "detail": "saved",
        "path": str(path),
        "keys": sorted(clean),
        # v8.9: setting os.environ is NOT enough. The backend client is
        # resolved once at startup, so a process that booted unconfigured has
        # already chosen (and cached) a backend — measured: after saving
        # DOURMOUSE_LLM_BACKEND=nvidia the running app still reported
        # {"backend":"ollama"}. The UI must restart the app, not just carry on.
        "restart_required": True,
    }


def restart_app() -> dict[str, Any]:
    """Relaunch the desktop app so the new configuration actually applies.

    Only meaningful in a frozen build, where ``sys.executable`` is the app
    itself. Running from source we report honestly that the caller has to
    restart rather than silently doing nothing and claiming success.
    """
    import subprocess
    import sys
    import threading

    if not getattr(sys, "frozen", False):
        return {
            "ok": False,
            "detail": "not a packaged build — restart Dourmouse manually to apply",
        }
    exe = sys.executable
    try:
        # Detach so the child survives this process exiting.
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        subprocess.Popen([exe], close_fds=True, creationflags=flags)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"could not relaunch: {exc}"}

    # Give the HTTP response time to flush before this process dies, or the
    # browser sees a dropped connection instead of a confirmation.
    def _die() -> None:
        import time

        time.sleep(1.5)
        os._exit(0)

    threading.Thread(target=_die, daemon=True).start()
    return {"ok": True, "detail": "restarting"}
