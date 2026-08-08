"""Account & service connection status (v5.3).

A single deterministic, honest report of which external accounts/services
Dourmouse can actually reach today:

- ``ollama``   — local LLM backend (keyless; probe 127.0.0.1:11434)
- ``nvidia``   — NVIDIA NIM key present in .env (never the key itself)
- ``claude``   — the user's real Claude Code CLI on PATH (their Claude
                 account — claude.ai login is what ``claude -p`` uses)
- ``codex``    — the user's real Codex CLI on PATH + ~/.codex/auth.json
                 (their ChatGPT login). Usage limits surface at run time.
- ``gmail``    — the mail tools' Gmail config (env or local_secrets.py)
- ``freebuff`` — the Freebuff Desktop app (local UI/API ports) + whether
                 FREEBUFF_API_URL/FREEBUFF_API_TOKEN are set for API calls
- ``slack``    — Slack bot/app tokens present
- ``alpaca``   — Alpaca paper-trading keys present
- ``atlas``    — ATLAS_REPO_PATH + ATLAS_VENV_PATH present and the repo
                 directory actually exists

Every probe is cheap, read-only, and failure-safe: a dead probe reports
``offline`` with a hint — never a crash and never a fabricated success
(Rules 2.1 / 2.2). Secrets are NEVER returned; only booleans and safe
labels/versions. No remote network calls — localhost sockets and local CLI
``--version`` probes only, so this is safe to poll from the HUD.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any

_FREEBUFF_UI_PORT = 51819
_FREEBUFF_API_PORT = 51820
_OLLAMA_PROBE = ("127.0.0.1", 11434)


def _env_present(*names: str) -> bool:
    """True when ANY of the given env vars has a non-empty value."""
    return any(bool(os.environ.get(n, "").strip()) for n in names)


def _tcp_reachable(host: str, port: int, timeout: float = 0.6) -> bool:
    """True when a TCP connect to (host, port) succeeds. Never raises."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _cli_version(name: str) -> str | None:
    """First line of ``<name> --version`` stdout/stderr, or None."""
    path = shutil.which(name)
    if path is None:
        return None
    try:
        proc = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,  # version probes are best-effort; never raise
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (proc.stdout or proc.stderr or "").strip()
    return text.splitlines()[0][:80] if text else None


def _codex_auth_mode() -> str:
    """'chatgpt' / 'apikey' from ~/.codex/auth.json, or 'none'.

    Reads only the non-secret ``auth_mode`` key; tokens are never read. The
    auth path is resolved at CALL time (HOME can change after import).
    """
    auth_path = Path("~/.codex/auth.json").expanduser()
    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "none"
    mode = data.get("auth_mode")
    return str(mode) if mode else ("token" if data.get("tokens") else "none")


def _gmail_status() -> dict[str, str]:
    """Honest Gmail config status via the existing google_services module."""
    try:
        from dourmouse import google_services as gs

        if gs.gmail_configured():
            return {"ok": "configured", "detail": gs.status()["detail"]}
        return {"ok": "missing", "detail": gs.status()["detail"]}
    except Exception:  # noqa: BLE001 -- a broken import never kills the report
        return {"ok": "missing", "detail": "google module unavailable"}


def check_connections() -> dict[str, dict[str, Any]]:
    """Deterministic per-service status report (see module docstring)."""
    out: dict[str, dict[str, Any]] = {}

    ollama = _tcp_reachable(*_OLLAMA_PROBE)
    out["ollama"] = {
        "ok": ollama,
        "detail": "local server on 127.0.0.1:11434" if ollama else "not running",
        "hint": "start Ollama (or set DOURMOUSE_LLM_BACKEND=nvidia)",
    }

    nvidia = _env_present("NVIDIA_API_KEY")
    out["nvidia"] = {
        "ok": nvidia,
        "detail": "NVIDIA_API_KEY present" if nvidia else "NVIDIA_API_KEY MISSING",
        "hint": "add NVIDIA_API_KEY to .env",
    }

    claude = _cli_version("claude")
    out["claude"] = {
        "ok": claude is not None,
        "detail": f"Claude Code CLI {claude}" if claude else "claude CLI not on PATH",
        "hint": "npm i -g @anthropic-ai/claude-code",
    }

    codex = _cli_version("codex")
    auth_mode = _codex_auth_mode()
    if codex and auth_mode != "none":
        detail = f"Codex CLI {codex} · logged in ({auth_mode})"
    elif codex:
        detail = f"Codex CLI {codex} · no ~/.codex/auth.json"
    else:
        detail = "codex CLI not on PATH"
    out["codex"] = {
        "ok": codex is not None and auth_mode != "none",
        "detail": detail,
        "hint": "npm i -g @openai/codex && codex login (usage limits show at run time)",
    }

    gmail = _gmail_status()
    out["gmail"] = {
        "ok": gmail["ok"] == "configured",
        "detail": gmail["detail"],
        "hint": "GOOGLE_GMAIL_USER/APP_PASSWORD or dourmouse/local_secrets.py",
    }

    fb_ui = _tcp_reachable("127.0.0.1", _FREEBUFF_UI_PORT)
    fb_api = _tcp_reachable("127.0.0.1", _FREEBUFF_API_PORT)
    fb_token = _env_present("FREEBUFF_API_TOKEN")
    if fb_ui and fb_api and fb_token:
        fb_detail = "app running · API reachable · token set (API ready)"
    elif fb_ui:
        fb_detail = "app running (UI) · API token MISSING"
    else:
        fb_detail = "Freebuff Desktop not running"
    out["freebuff"] = {
        # "ok" means USABLE, not merely present: the app's API needs the
        # bearer token, so a running app without it is honestly not ready.
        "ok": bool(fb_ui and fb_api and fb_token),
        "detail": fb_detail,
        "hint": "paste FREEBUFF_API_TOKEN from the Freebuff app into .env",
    }

    slack = _env_present("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN")
    out["slack"] = {
        "ok": slack,
        "detail": "tokens present" if slack else "no SLACK_* tokens in .env",
        "hint": "SLACK_BOT_TOKEN + SLACK_APP_TOKEN in .env",
    }

    alpaca = _env_present("APCA_API_KEY_ID", "APCA_API_SECRET_KEY")
    out["alpaca"] = {
        "ok": alpaca,
        "detail": "paper-trading keys present" if alpaca else "no APCA_* keys in .env",
        "hint": "APCA_API_KEY_ID + APCA_API_SECRET_KEY in .env",
    }

    atlas_repo = os.environ.get("ATLAS_REPO_PATH", "").strip()
    atlas_venv = os.environ.get("ATLAS_VENV_PATH", "").strip()
    atlas_ok = (
        bool(atlas_repo)
        and Path(atlas_repo).expanduser().is_dir()
        and bool(atlas_venv)
    )
    if atlas_repo and Path(atlas_repo).expanduser().is_dir() and atlas_venv:
        atlas_detail = "repo path + venv configured"
    elif atlas_repo and atlas_venv:
        atlas_detail = "paths set but repo dir not found"
    else:
        atlas_detail = "ATLAS_REPO_PATH / ATLAS_VENV_PATH not set"
    out["atlas"] = {
        "ok": atlas_ok,
        "detail": atlas_detail,
        "hint": "ATLAS_REPO_PATH + ATLAS_VENV_PATH in .env",
    }
    return out


def freebuff_status() -> dict[str, Any]:
    """Freebuff Desktop detail for tools/UI (app + API + token)."""
    fb_ui = _tcp_reachable("127.0.0.1", _FREEBUFF_UI_PORT)
    fb_api = _tcp_reachable("127.0.0.1", _FREEBUFF_API_PORT)
    return {
        "app_running": fb_ui,
        "api_reachable": fb_api,
        "api_token_set": _env_present("FREEBUFF_API_TOKEN"),
        "hint": (
            "FREEBUFF_API_URL + FREEBUFF_API_TOKEN in .env (token shown in "
            "the Freebuff app) unlocks real API reads"
        ),
    }


def format_connections() -> str:
    """Human-readable connection report for the check_connections tool.

    Deterministic (Rule 2.8) — no parameters today; the full report.
    """
    lines = ["CONNECTION STATUS //", ""]
    for name, item in sorted(check_connections().items()):
        mark = "●" if item["ok"] else "○"
        lines.append(f"{mark} {name}: {item['detail']}")
        if not item["ok"]:
            lines.append(f"    fix: {item['hint']}")
    lines.append("")
    fb = freebuff_status()
    lines.append("FREEBUFF APP: " + ("running" if fb["app_running"] else "not running"))
    lines.append(
        "FREEBUFF API: " + ("ready" if fb["api_reachable"] and fb["api_token_set"] else "token missing")
    )
    return "\n".join(lines)
