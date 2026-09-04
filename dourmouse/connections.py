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
- ``freebuff`` — the Freebuff Desktop app (its own loopback renderer API
                 on 51819) reporting whether an authed account is readable
                 (v5.5: read-only, no token needed — the 51820 bridge's
                 per-launch random token is the debugger API, not the read
                 path)
- ``slack``    — Slack bot/app tokens present
- ``alpaca``   — Alpaca paper-trading keys present
- ``atlas``    — ATLAS_REPO_PATH + ATLAS_VENV_PATH present and the repo
                 directory actually exists
- ``server``   — the DOURMOUSE compute node (the Dell): reachable on the
                 LAN at DOURMOUSE_SERVER_URL (default 192.168.1.108:8000)
                 via its /v1/status endpoint. The ONE deliberate LAN HTTP
                 probe in this module — cheap (1.5 s timeout), cached 30 s
                 by dourmouse.remote_server, read-only, and the UI needs it
                 to show the node online/latency (v5.26).

Every probe is cheap, read-only, and failure-safe: a dead probe reports
``offline`` with a hint — never a crash and never a fabricated success
(Rules 2.1 / 2.2). Secrets are NEVER returned; only booleans and safe
labels/versions. All probes are localhost sockets / local CLI ``--version``
checks EXCEPT the single ``server`` LAN status probe documented above.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

_FREEBUFF_UI_PORT = 51819
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


def _claude_signin() -> str:
    """'yes' | 'unknown' — never a confident 'no'.

    Presence on PATH was the old test and produced a green tick for a CLI
    that answers "Not logged in · Please run /login". The obvious fix — look
    for a credentials file — is ALSO wrong: on Windows the session lives in
    the Credential Manager, so no file exists even when signed in, and an
    earlier attempt here read a non-empty ``projects/`` directory as proof
    and went straight back to a false green.

    Real gap found and fixed live on a real Mac: the same class of problem
    exists on macOS too — Claude Code stores its session in the Keychain
    (service "Claude Code-credentials"), not a file, so the file check alone
    reported 'unknown' for a genuinely signed-in CLI that had just been
    proven to work end-to-end. ``security find-generic-password`` (macOS's
    own, always-present CLI) confirms an ENTRY EXISTS without ever reading
    or printing the secret value itself — read-only, cheap, real evidence,
    same "hard evidence only" standard as the file check.

    There is still no cheap, portable, reliable signal on every platform.
    So this reports 'yes' only on hard evidence (a credentials file, or a
    real macOS Keychain entry) and 'unknown' otherwise, and the caller says
    so plainly rather than inventing a verdict in either direction.
    """
    home = Path("~/.claude").expanduser()
    for name in (".credentials.json", "credentials.json"):
        if (home / name).exists():
            return "yes"
    if sys.platform == "darwin":
        try:
            proc = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials"],
                capture_output=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return "unknown"
        if proc.returncode == 0:
            return "yes"
    return "unknown"


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


def _spotify_status() -> dict[str, str]:
    """Honest Spotify status via dourmouse.spotify_services (v5.7)."""
    try:
        from dourmouse import spotify_services as ss

        st = ss.status()
        if st.get("linked"):
            return {"ok": "configured", "detail": f"linked · {st['detail']}"}
        if st.get("configured"):
            return {"ok": "missing", "detail": st["detail"]}
        return {"ok": "missing", "detail": st["detail"]}
    except Exception:  # noqa: BLE001 -- a broken import never kills the report
        return {"ok": "missing", "detail": "spotify module unavailable"}


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

    # v5.26: the DOURMOUSE compute node (Dell) — the one LAN HTTP probe.
    try:
        from dourmouse.remote_server import server_status

        srv = server_status()
        out["server"] = {
            "ok": bool(srv.get("online")),
            "detail": (
                f"{(srv.get('node') or 'node')} · {(srv.get('model') or '?')} · "
                f"{srv.get('latency_ms')}ms"
                if srv.get("online")
                else f"offline — {srv.get('error') or 'no response'}"
            ),
            "hint": (
                "compute node at "
                + (srv.get("url") or "DOURMOUSE_SERVER_URL")
                + " (set DOURMOUSE_SERVER_URL to change)"
            ),
        }
    except Exception:  # noqa: BLE001 - a broken import never kills the report
        out["server"] = {
            "ok": False,
            "detail": "server module unavailable",
            "hint": "set DOURMOUSE_SERVER_URL",
        }

    claude = _cli_version("claude")
    signin = _claude_signin() if claude else "unknown"
    if claude and signin == "yes":
        claude_detail = f"Claude Code CLI {claude} · signed in"
    elif claude:
        claude_detail = f"Claude Code CLI {claude} · sign-in not verified"
    else:
        claude_detail = "claude CLI not on PATH"
    out["claude"] = {
        "ok": bool(claude) and signin == "yes",
        "detail": claude_detail,
        "hint": (
            "installed; sign-in could not be confirmed (Windows keeps it in the "
            "Credential Manager). Run 'claude' once on the host and complete "
            "/login — it will work here once signed in."
            if claude
            else "npm i -g @anthropic-ai/claude-code"
        ),
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

    # v5.5: the REAL Freebuff read surface is the app's own renderer API on
    # 51819 (loopback, no token). The 51820 bridge needs a per-launch random
    # token (crypto.randomUUID — regenerates every app start, never persisted)
    # and is the app's internal debugger/preview API, NOT the read path. So
    # "usable" = the 51819 API answers auth/status as authed. The old
    # token-gated check was misleading — a pasted token would break on the
    # next app launch anyway. Uses the module-level ``freebuff_status()``
    # (a clean seam tests can patch without touching the real API).
    fb = freebuff_status()
    out["freebuff"] = {
        "ok": bool(fb.get("ok")),
        "detail": fb.get("detail", "Freebuff Desktop not running"),
        "hint": fb.get("hint", "start the Freebuff app and sign in (reads need no token)"),
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

    # v5.22: resolve through the shared helpers so a personal dist with the
    # bundled atlas/ engine reads as configured WITHOUT any env vars. The
    # display strings are unchanged (env-setup users see exactly what they
    # saw before); imports stay lazy + failure-safe so a broken module never
    # kills the report (this module's contract).
    try:
        from dourmouse.atlas_ops import get_atlas_repo_path as _atlas_repo
        from dourmouse.research_agent import get_atlas_venv_python as _atlas_venv

        atlas_repo_resolves = True
        try:
            _atlas_repo()
        except Exception:  # noqa: BLE001 -- probe failure is just "not configured"
            atlas_repo_resolves = False
        # venv slot: any non-empty ATLAS_VENV_PATH counts (the historic loose
        # contract — the actual CLI checks the binary at use time), else the
        # personal dist's shared venv must actually resolve.
        atlas_venv_resolves = bool(os.environ.get("ATLAS_VENV_PATH", "").strip())
        if not atlas_venv_resolves:
            try:
                _atlas_venv()
                atlas_venv_resolves = True
            except Exception:  # noqa: BLE001 -- probe failure is just "not configured"
                atlas_venv_resolves = False
    except Exception:  # noqa: BLE001
        atlas_repo_resolves = atlas_venv_resolves = False
    # Detail text keys off the ENV vars (historic contract) so a bundled
    # engine with broken env paths still says so instead of a happy lie;
    # the ok boolean already counts the bundle.
    env_repo = os.environ.get("ATLAS_REPO_PATH", "").strip()
    env_venv = os.environ.get("ATLAS_VENV_PATH", "").strip()
    if atlas_repo_resolves and atlas_venv_resolves:
        atlas_detail = "repo path + venv configured"
    elif env_repo and env_venv:
        atlas_detail = "paths set but repo dir not found"
    else:
        atlas_detail = "ATLAS_REPO_PATH / ATLAS_VENV_PATH not set"
    # Long-term memory. Added after a real live failure: with
    # DOURMOUSE_MEMORY_REMOTE_URL set to a machine that was switched off,
    # every memory and RAG feature failed, /api/profile dropped its
    # connection outright, and NOTHING in the connections panel mentioned
    # memory at all -- so there was no way for a human to see why. The
    # probe is a cheap TCP reachability check rather than a real query,
    # to honor this module's "no remote network calls on HUD polls" rule.
    _mem_remote = (os.environ.get("DOURMOUSE_MEMORY_REMOTE_URL") or "").strip()
    if _mem_remote:
        from urllib.parse import urlsplit

        _mp = urlsplit(_mem_remote if "://" in _mem_remote else "http://" + _mem_remote)
        _mhost, _mport = _mp.hostname or "", _mp.port or 8765
        _mup = bool(_mhost) and _tcp_reachable(_mhost, _mport)
        out["memory"] = {
            "ok": _mup,
            "detail": (
                f"remote store on {_mhost}:{_mport}" if _mup
                else f"remote store at {_mhost}:{_mport} is unreachable"
            ),
            "hint": (
                "Start Dourmouse on that machine, or unset "
                "DOURMOUSE_MEMORY_REMOTE_URL in .env to use this machine's own "
                "local memory store instead."
            ),
        }
    else:
        out["memory"] = {
            "ok": True,
            "detail": "local store on this machine",
            "hint": "set DOURMOUSE_MEMORY_REMOTE_URL to share one store across machines",
        }

    out["atlas"] = {
        "ok": atlas_repo_resolves and atlas_venv_resolves,
        "detail": atlas_detail,
        "hint": "ATLAS_REPO_PATH + ATLAS_VENV_PATH in .env (or build the personal dist with the bundled engine)",
    }

    # v5.7: Spotify — Client ID set AND account linked once (PKCE login).
    spotify = _spotify_status()
    out["spotify"] = {
        "ok": spotify["ok"] == "configured",
        "detail": spotify["detail"],
        "hint": "SPOTIFY_CLIENT_ID in .env/local_secrets + run spotify_login (music agent)",
    }

    # v5.12: World Monitor — global intelligence API. The data tools need a
    # key, so "ok" = key present AND the API answers. To honor this module's
    # "no remote network calls on HUD polls" contract, the remote health
    # probe runs ONLY when a key is configured — a keyless user learns the
    # same thing (missing key) from env alone, with zero network cost.
    wm_key = _env_present("WORLDMONITOR_API_KEY", "WM_API_KEY")
    try:
        from dourmouse import worldmonitor as wm

        if wm_key:
            wm_st = wm.worldmonitor_status()
            wm_ok = bool(wm_st.get("ok"))
            wm_detail = wm_st.get("detail", "") + " · key present"
        else:
            wm_ok = False
            wm_detail = "no WORLDMONITOR_API_KEY (status probe skipped — keyless poll)"
        out["worldmonitor"] = {
            "ok": wm_ok,
            "detail": wm_detail,
            "hint": "WORLDMONITOR_API_KEY in .env (worldmonitor.app/pro) for data tools",
        }
    except Exception:  # noqa: BLE001 -- a broken probe never kills the report
        out["worldmonitor"] = {
            "ok": False,
            "detail": "worldmonitor module unavailable",
            "hint": "pip install worldmonitor-sdk",
        }
    return out


def freebuff_status() -> dict[str, Any]:
    """Freebuff Desktop detail for tools/UI (real API probe, no token).

    v5.5: the app's own renderer API (51819, loopback, unauthenticated) is
    the read surface. A running + authed app is USABLE (``ok`` True) — no
    token needed (the 51820 bridge's per-launch random token is the debugger
    API, not the read path). Shape matches the bridge module so callers can
    treat them interchangeably: ok/detail/hint/account/app_running.
    """
    fb_ui = _tcp_reachable("127.0.0.1", _FREEBUFF_UI_PORT)
    base: dict[str, Any] = {
        "app_running": fb_ui,
        "ok": False,
        "hint": "start the Freebuff app and sign in (reads need no token)",
    }
    if not fb_ui:
        base["detail"] = "Freebuff Desktop not running"
        return base
    try:
        from dourmouse.freebuff_bridge import freebuff_status as fb_probe

        st = fb_probe()
    except Exception:  # noqa: BLE001 -- a broken probe never kills the report
        base["detail"] = "app running · probe error"
        return base
    base["ok"] = bool(st.get("ok"))
    base["account"] = st.get("account")
    base["detail"] = st.get("detail", "app running")
    return base


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
        "FREEBUFF API: " + ("ready" if fb.get("api_ready") else "app running · no authed account")
    )
    return "\n".join(lines)
