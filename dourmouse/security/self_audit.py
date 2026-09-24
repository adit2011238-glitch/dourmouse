"""Security self-audit: Dourmouse checks itself (MS-12, spec item 48;
finding #112).

A security tool with broad power over the Mac is itself a target. This
audits the things that would turn Dourmouse into the weak point, each as a
finding with its evidence and fix, like the rest of the security package:

- where the server listens, and whether a non-loopback bind has a token;
- the auto-approve toggle, which silently skips every approval gate;
- the permissions of the files holding keys and settings;
- whether a .env with keys is tracked by git in the source checkout;
- the lockdown helper, the only root code: the installed copy must match
  the packaged one byte for byte, be root-owned and not writable by
  anyone else, and launchd must point at it;
- who can read the quarantine and the security workspace.

What it cannot check is listed, not assumed fine.
"""

from __future__ import annotations

import hashlib
import plistlib
import stat
import subprocess
from pathlib import Path
from typing import Any


def _f(check: str, severity: str, title: str, detail: str, fix: str) -> dict[str, Any]:
    return {"check": check, "severity": severity, "title": title, "detail": detail, "fix": fix}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_bind(host: str, token: str) -> list[dict[str, Any]]:
    if host in ("127.0.0.1", "localhost", "::1"):
        return []
    if not token:
        return [_f("bind", "high", f"Dourmouse listens on {host} with no access token",
                   "Anyone who can reach this Mac on the network can use Dourmouse, and through it the Mac.",
                   "Set DOURMOUSE_HOST=127.0.0.1, or set an access token before listening on the network.")]
    return [_f("bind", "low", f"Dourmouse listens on {host}",
               "Other devices can reach the login page; an access token is required to use it.",
               "Use 127.0.0.1 unless you really use Dourmouse from another device.")]


def check_auto_approve(enabled: bool) -> list[dict[str, Any]]:
    if not enabled:
        return []
    return [_f("auto_approve", "high", "Auto-approve is on: every approval gate is skipped",
               "Lockdown, quarantine, stopping processes, sending mail and every other gated action run "
               "without asking. A prompt injected into a web page or email could use them.",
               "Turn auto-approve off in Settings unless you are watching every action.")]


def check_secret_file(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        return [_f("secret_file", "med", f"{path.name} can be read by other users of this Mac",
                   f"{path} holds keys and has permissions {oct(mode)}.",
                   f"Run: chmod 600 '{path}'")]
    return []


def check_env_tracked(repo: Path) -> list[dict[str, Any]]:
    if not (repo / ".git").exists():
        return []
    proc = subprocess.run(["git", "-C", str(repo), "ls-files", "--error-unmatch", ".env"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    if proc.returncode == 0:
        return [_f("env_tracked", "high", ".env is tracked by git",
                   f"{repo / '.env'} is committed, so its keys go wherever the repository goes.",
                   "git rm --cached .env, add it to .gitignore, and rotate every key it held.")]
    return []


def check_helper(installed: Path, plist: Path, packaged: Path) -> list[dict[str, Any]]:
    if not installed.exists() and not plist.exists():
        return []
    out: list[dict[str, Any]] = []
    if not installed.exists() or not plist.exists():
        return [_f("helper", "med", "The lockdown helper is half installed",
                   f"helper present: {installed.exists()}, launchd job present: {plist.exists()}.",
                   "Reinstall it with the command in the lockdown status, or uninstall it.")]
    if _sha(installed) != _sha(packaged):
        out.append(_f("helper", "high", "The installed lockdown helper differs from Dourmouse's own copy",
                      f"{installed} does not match {packaged} byte for byte: it was changed after install, or "
                      "Dourmouse was updated since. It runs as root.",
                      "Reinstall it with the command in the lockdown status (after checking what changed)."))
    st = installed.stat()
    if st.st_uid != 0 or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        out.append(_f("helper", "high", "The lockdown helper can be modified by a non-root user",
                      f"{installed}: owner uid {st.st_uid}, mode {oct(stat.S_IMODE(st.st_mode))}. A root-run "
                      "script others can edit is a way to become root.",
                      "Reinstall it with the command in the lockdown status."))
    try:
        args = plistlib.loads(plist.read_bytes()).get("ProgramArguments") or []
    except (OSError, plistlib.InvalidFileException, ValueError):
        args = []
    if str(installed) not in args:
        out.append(_f("helper", "high", "launchd runs something other than the lockdown helper",
                      f"{plist} ProgramArguments: {args}",
                      "Uninstall and reinstall the helper."))
    return out


def check_private_dir(path: Path, label: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o007:
        return [_f("private_dir", "low", f"{label} can be listed by other users of this Mac",
                   f"{path} has permissions {oct(mode)}.", f"Run: chmod 700 '{path}'")]
    return []


def run_self_audit() -> dict[str, Any]:
    from dourmouse.config import access_token, auto_approve_enabled, bind_host, user_env_path, workspace_dir

    from . import lockdown_helper as helper
    from .privacy import privacy_mode, settings_path

    repo = Path(__file__).resolve().parents[2]
    findings: list[dict[str, Any]] = []
    findings += check_bind(bind_host(), access_token())
    findings += check_auto_approve(auto_approve_enabled())
    for p in (user_env_path(), repo / ".env", settings_path()):
        findings += check_secret_file(p)
    findings += check_env_tracked(repo)
    findings += check_helper(Path(helper.INSTALLED), Path(helper.PLIST), Path(helper.__file__))
    findings += check_private_dir(workspace_dir() / "security" / "quarantine", "The quarantine folder")
    findings += check_private_dir(workspace_dir() / "security", "The security workspace")
    return {
        "findings": sorted(findings, key=lambda f: {"high": 0, "med": 1, "low": 2}[f["severity"]]),
        "privacy_mode": privacy_mode(),
        "checked": ["where the server listens", "auto-approve", "key and settings file permissions",
                    ".env tracked by git", "the lockdown helper's integrity, ownership and launchd job",
                    "quarantine and security workspace permissions"],
        "not_checked": ["whether the cloud model provider keeps what it is sent (outside this Mac)",
                        "the integrity of Dourmouse's own Python files (no signed release to compare against)"],
    }
