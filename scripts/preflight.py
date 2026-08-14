#!/usr/bin/env python3
"""preflight.py — verify a Dourmouse host BEFORE trusting it.

Run this on the new machine after copying the repo, workspace and .env. It
checks the things that actually break a migration, in the order they bite,
and it is deliberately blunt: FAIL means do not run the server yet.

Cross-platform (Windows/macOS/Linux) — stdlib only apart from the repo
itself, so it runs before the venv is fully populated.

Usage:
  python scripts/preflight.py
  python scripts/preflight.py --host 0.0.0.0 --port 8765
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
_results: list[tuple[str, str, str]] = []


def record(status: str, name: str, detail: str = "") -> None:
    _results.append((status, name, detail))
    mark = {PASS: "  ok ", WARN: " warn", FAIL: "FAIL "}[status]
    print(f"{mark} {name}" + (f" — {detail}" if detail else ""), flush=True)


# -- environment ----------------------------------------------------------- #

def check_python() -> None:
    v = sys.version_info
    if v < (3, 11):
        record(FAIL, "python version", f"{v.major}.{v.minor} — need 3.11+")
    else:
        record(PASS, "python version", f"{v.major}.{v.minor}.{v.micro}")


def load_env() -> dict[str, str]:
    """Read .env WITHOUT requiring python-dotenv (may not be installed yet)."""
    env: dict[str, str] = {}
    path = REPO / ".env"
    if not path.exists():
        record(FAIL, ".env present", f"missing at {path}")
        return env
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    record(PASS, ".env present", f"{len(env)} keys")
    return env


def check_security(env: dict[str, str], host: str) -> None:
    """The single most important check on a LAN box."""
    token = env.get("DOURMOUSE_ACCESS_TOKEN", "").strip()
    bind = env.get("DOURMOUSE_HOST", host).strip() or host
    loopback = bind in ("127.0.0.1", "localhost", "::1")

    if loopback:
        record(PASS, "bind posture", f"{bind} (loopback — token optional)")
        return
    if not token:
        record(
            FAIL,
            "ACCESS TOKEN",
            f"binding {bind} with NO token — every route would be open "
            "(mail, files, shell). The server will refuse to start.",
        )
        return
    if len(token) < 24:
        record(
            WARN,
            "ACCESS TOKEN",
            f"only {len(token)} chars — use 32+ random characters",
        )
    else:
        record(PASS, "ACCESS TOKEN", f"set ({len(token)} chars), bind {bind}")


def check_paths(env: dict[str, str]) -> None:
    """Machine-specific paths are the classic migration breakage."""
    for key in (
        "ATLAS_REPO_PATH",
        "ATLAS_VENV_PATH",
        "OBSIDIAN_VAULT_PATH",
        "DOURMOUSE_WORKSPACE",
    ):
        raw = env.get(key, "").strip()
        if not raw:
            record(WARN, f"path {key}", "unset (feature disabled)")
            continue
        if Path(raw).exists():
            record(PASS, f"path {key}", raw)
        else:
            record(FAIL, f"path {key}", f"does not exist here: {raw}")


def check_workspace(env: dict[str, str]) -> None:
    raw = env.get("DOURMOUSE_WORKSPACE", "").strip()
    ws = Path(raw) if raw else REPO / "workspace"
    if not ws.is_dir():
        record(FAIL, "workspace", f"missing: {ws}")
        return
    record(PASS, "workspace", str(ws))

    for sub in ("memory", "auth", "sessions", "state"):
        record(
            PASS if (ws / sub).is_dir() else WARN,
            f"workspace/{sub}",
            "present" if (ws / sub).is_dir() else "missing (will be created)",
        )

    # A copied SQLite file that did not survive the transfer is silent until
    # first use — open it now instead.
    for db in list(ws.rglob("*.db"))[:10]:
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            con.execute("PRAGMA quick_check").fetchone()
            n = con.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0]
            con.close()
            record(PASS, f"db {db.name}", f"{n} tables, readable")
        except sqlite3.Error as exc:
            record(FAIL, f"db {db.name}", f"unreadable: {exc}")


# -- runtime dependencies -------------------------------------------------- #

def check_imports() -> None:
    for mod, why in (
        ("openai", "LLM client"),
        ("dotenv", "env loading"),
        ("numpy", "neural orchestrator"),
        ("psutil", "host telemetry (optional)"),
        ("playwright", "browser agent (optional)"),
        ("segno", "phone QR (optional)"),
    ):
        try:
            __import__(mod)
            record(PASS, f"import {mod}", why)
        except ImportError:
            optional = "optional" in why
            record(
                WARN if optional else FAIL,
                f"import {mod}",
                f"missing — {why}. pip install -r requirements.txt",
            )


def check_port(host: str, port: int) -> None:
    """Bindable now? A port already in use is a confusing failure later."""
    probe = "0.0.0.0" if host in ("0.0.0.0", "::") else host
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((probe, port))
        record(PASS, f"port {port}", "free")
    except OSError as exc:
        record(FAIL, f"port {port}", f"cannot bind: {exc}")
    finally:
        sock.close()


# -- inference backend ----------------------------------------------------- #

def _http_ok(url: str, timeout: float, headers: dict[str, str] | None = None) -> bool:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 500
    except (urllib.error.URLError, OSError, ValueError):
        return False


def check_backend(env: dict[str, str]) -> None:
    """Without the Mac and without a GPU, a reachable backend IS the product."""
    backend = env.get("DOURMOUSE_LLM_BACKEND", "auto").strip().lower() or "auto"
    record(PASS, "backend selected", backend)

    if backend in ("ollama", "auto"):
        base = env.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1").strip()
        root = base[:-3] if base.endswith("/v1") else base
        record(
            PASS if _http_ok(root + "/api/tags", 3) else WARN,
            "ollama reachable",
            root,
        )

    if backend in ("nvidia", "auto"):
        key = env.get("NVIDIA_API_KEY", "").strip()
        if not key:
            record(WARN, "NVIDIA key", "not set")
        else:
            base = env.get(
                "NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"
            ).strip() or "https://integrate.api.nvidia.com/v1"
            ok = _http_ok(
                base + "/models", 10, {"Authorization": f"Bearer {key}"}
            )
            record(
                PASS if ok else FAIL,
                "NVIDIA reachable",
                base if ok else f"{base} — key set but endpoint did not answer",
            )

    if backend == "omniroute":
        base = env.get("OMNIROUTE_BASE_URL", "http://127.0.0.1:20128/v1").strip()
        record(
            PASS if _http_ok(base + "/models", 3) else FAIL,
            "omniroute reachable",
            base,
        )


def check_integrations(env: dict[str, str]) -> None:
    """Credential PRESENCE only — never values, never a live send."""
    for key, label in (
        ("GOOGLE_CLIENT_ID", "Google OAuth"),
        ("GOOGLE_GMAIL_USER", "Gmail"),
        ("SPOTIFY_CLIENT_ID", "Spotify"),
    ):
        record(
            PASS if env.get(key, "").strip() else WARN,
            label,
            "configured" if env.get(key, "").strip() else "not configured",
        )

    redirect = env.get("GOOGLE_REDIRECT_URI", "").strip()
    if redirect and ("localhost" in redirect or "127.0.0.1" in redirect):
        record(
            WARN,
            "Google redirect URI",
            f"{redirect} — points at loopback; sign-in from another machine "
            "will fail until the host address is authorised in Google Cloud "
            "Console",
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    print(f"DOURMOUSE preflight — {REPO}")
    print(f"platform: {sys.platform}\n")

    check_python()
    env = load_env()
    host = args.host or env.get("DOURMOUSE_HOST", "127.0.0.1").strip() or "127.0.0.1"

    print("\n-- security --")
    check_security(env, host)

    print("\n-- data --")
    check_paths(env)
    check_workspace(env)

    print("\n-- runtime --")
    check_imports()
    check_port(host, args.port)

    print("\n-- backend --")
    check_backend(env)

    print("\n-- integrations --")
    check_integrations(env)

    fails = [r for r in _results if r[0] == FAIL]
    warns = [r for r in _results if r[0] == WARN]
    print(
        f"\n{len(_results)} checks — {len(_results) - len(fails) - len(warns)} "
        f"pass, {len(warns)} warn, {len(fails)} FAIL"
    )
    if args.json:
        print(json.dumps([
            {"status": s, "check": n, "detail": d} for s, n, d in _results
        ], indent=2))
    if fails:
        print("\nDo NOT start the server yet. Fix the FAIL lines above:")
        for _, name, detail in fails:
            print(f"  - {name}: {detail}")
        return 1
    print("\nReady to start.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
