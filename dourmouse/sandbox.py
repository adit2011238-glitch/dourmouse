"""Kernel-enforced sandboxed execution for run_command (v2.0 Phase 1).

Replaces the regex-only guardrail for run_command with a REAL boundary: macOS
`sandbox-exec` (Seatbelt) runs the command in a kernel-enforced sandbox that
structurally cannot reach the user's credential directories, cannot write
anywhere outside an explicit allow-list (the workspace root + the command's
cwd), and has network denied by default. The regex classifier in
system_access.py remains as a cheap fast-path pre-filter — it is no longer
the safety guarantee.

WHY sandbox-exec (decision + tradeoff, recorded per the build prompt):

- Strong, kernel-enforced isolation, ships with macOS, zero new dependencies
  — consistent with this project's "no Docker, no Node, no build step"
  constraint.
- sandbox-exec IS deprecated by Apple (still shipped and functional as of
  current macOS, but Apple has signaled eventual removal in favor of the App
  Sandbox entitlement model, which does not fit a CLI tool). We ship it
  anyway — it is the correct tool available today — and flag this as a known
  future-migration item: if sandbox-exec is ever pulled, the next option is
  a dedicated non-privileged OS user + launchd.

HONEST DEGRADATION (the single most important property of this file, Rule
2.1/2.2): if sandbox-exec is unavailable (non-macOS, or the binary is
missing/removed by a future OS update), run_sandboxed returns a plain
NOT CONFIGURED message and NEVER silently falls back to unsandboxed
execution. A silent fallback would be worse than not having the feature.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

_OUTPUT_CAP = 20_000  # chars of command output returned to the model

# Credential dirs denied for READING inside the sandbox (paths resolved to
# their real form — macOS /tmp is a symlink to /private/tmp and Seatbelt
# matches resolved paths, verified empirically).
_SENSITIVE_HOME_SUBDIRS = (
    ".ssh",
    ".aws",
    ".gnupg",
    ".kube",
    ".docker",
    "Library/Keychains",
)

# Writable device nodes the shell legitimately needs (redirects like
# `2>/dev/null` must keep working inside the sandbox).
_DEV_NODES = ("/dev/null", "/dev/zero", "/dev/random", "/dev/urandom", "/dev/tty")

# Secret FILENAMES denied for reading anywhere, by path regex (Seatbelt
# supports (regex #"...")); mirrors _SENSITIVE_FILENAME_PATTERNS in
# system_access.py as defense in depth (Phase 0 + 1).
_SECRET_FILENAME_REGEXES = (
    r"\.env(\..*)?$",
    r"\.pem$",
    r"\.key$",
    r"id_(rsa|ed25519|ecdsa|dsa)$",
    r"\.netrc$",
    r"\.npmrc$",
    r"\.pgpass$",
)


def _workspace_root() -> Path:
    """Workspace root (env wins, else <project>/workspace) — same convention
    as chat.py/general_roster.py, without importing them (avoids a cycle)."""
    raw = os.environ.get("DOURMOUSE_WORKSPACE")
    root = Path(raw).expanduser() if raw else Path(__file__).resolve().parent.parent / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_sandbox_profile(cwd: str, allow_network: bool = False) -> str:
    """Render the Seatbelt profile for a given cwd.

    The profile is generated (not a static string) because paths must be
    RESOLVED to their real form: Seatbelt matches resolved paths, and on
    macOS `/tmp` is a symlink to `/private/tmp` — an unresolv'ed deny on
    `/tmp/...` silently leaks (verified empirically; a deny on the resolved
    path blocks it).
    """
    home = Path.home().resolve()
    ws = _workspace_root().resolve()
    cwd_path = Path(cwd).expanduser().resolve()

    lines: list[str] = [
        "(version 1)",
        "(deny default)",
        # --- process / runtime basics ---
        "(allow process-exec*)",
        "(allow process-fork)",
        "(allow process-info*)",
        "(allow signal (target self))",
        "(allow sysctl-read)",
        "(allow mach-lookup)",
        "(allow ipc-posix*)",
        # --- reads: broadly allowed, then credential dirs + secret files denied ---
        "(allow file-read*)",
    ]
    for sub in _SENSITIVE_HOME_SUBDIRS:
        lines.append(f'(deny file-read* (subpath {_quote(str(home / sub))}))')
    for pat in _SECRET_FILENAME_REGEXES:
        lines.append(f'(deny file-read* (regex #"{pat}"))')
    # --- writes: only workspace + cwd (+ device nodes) ---
    for allow_path in (ws, cwd_path):
        lines.append(f'(allow file-write* (subpath {_quote(str(allow_path))}))')
    for dev in _DEV_NODES:
        lines.append(f'(allow file-write* (literal {_quote(dev)}))')
    # --- network: denied unless the caller explicitly needs it ---
    if not allow_network:
        lines.append("(deny network*)")
    return "\n".join(lines)


def sandbox_available() -> bool:
    """True if sandbox-exec exists and is executable on this system."""
    exe = shutil.which("sandbox-exec")
    return exe is not None and os.access(exe, os.X_OK)


def run_sandboxed(
    command: str,
    cwd: str,
    timeout: int,
    allow_network: bool = False,
) -> str:
    """Run ``command`` inside a kernel-enforced Seatbelt sandbox.

    Returns the same output shape as system_access._run_shell so callers
    don't change: "EXIT CODE: n" + STDOUT/STDERR (truncated at _OUTPUT_CAP).

    If sandbox-exec is unavailable this returns a plain NOT CONFIGURED
    message and NEVER runs the command unsandboxed (Rule 2.2 — a silent
    fallback would be worse than no sandbox at all).
    """
    exe = shutil.which("sandbox-exec")
    if exe is None or not os.access(exe, os.X_OK):
        return (
            "NOT CONFIGURED: sandboxed execution requires macOS sandbox-exec, "
            "which is unavailable on this system. Refusing to run the command "
            "unsandboxed (a silent fallback would defeat the sandbox)."
        )
    profile = build_sandbox_profile(cwd, allow_network=allow_network)

    # Write the profile to a temp file and pass it with -f: passing a
    # multi-line profile as a -p argv element risks quoting ambiguities, and
    # -f is the well-trodden path. The file is unlinked in the finally below.
    fd, profile_path = tempfile.mkstemp(prefix="dourmouse-sb-", suffix=".sb")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(profile)
        proc = subprocess.run(
            [exe, "-f", profile_path, "/bin/sh", "-c", command],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {timeout}s (sandboxed)."
    except OSError as exc:
        return f"ERROR: could not run sandboxed command: {exc}"
    finally:
        try:
            os.unlink(profile_path)
        except OSError:
            pass

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    parts = [f"EXIT CODE: {proc.returncode}"]
    if out:
        truncated = out[-_OUTPUT_CAP:]
        parts.append(
            "STDOUT:\n" + truncated
            + ("\n[output truncated]" if len(out) > _OUTPUT_CAP else "")
        )
    if err:
        parts.append("STDERR:\n" + err[:_OUTPUT_CAP])
    return "\n".join(parts)
