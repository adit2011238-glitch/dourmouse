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

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

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
    "Library/Application Support",
    "Library/Cookies",
    "Library/HTTPStorages",
    "Library/Messages",
    "Library/Mail",
    "Library/Safari",
    "Library/Containers",
    "Library/Group Containers",
    ".config",
    ".zsh_sessions",
)

# Home FILES (shell and interpreter history) denied for reading inside the
# sandbox (S23).
_SENSITIVE_HOME_FILES = (".zsh_history", ".bash_history", ".python_history", ".node_repl_history", ".lesshst")

# System locations a process needs to start and run: libraries, frameworks,
# the dyld shared cache, timezone and locale data, and /etc. Reads outside
# this list, the caller's own directories and the discovered interpreter are
# denied by default (S23).
_SYSTEM_READ_SUBPATHS = (
    "/usr/lib", "/usr/share", "/usr/libexec", "/usr/bin", "/bin", "/sbin", "/usr/sbin",
    "/System", "/Library/Frameworks", "/Library/Preferences/Logging",
    "/private/etc", "/private/var/db", "/private/var/select", "/dev",
)

# Extra read locations only for run_command shells: developer toolchains.
_TOOLCHAIN_READ_SUBPATHS = ("/opt/homebrew", "/usr/local", "/Library/Developer", "/Applications/Xcode.app")

# Environment variables that are safe to pass to a sandboxed process and hold
# no secrets. Everything else, above all API keys and tokens, is dropped.
_SAFE_LOCALE_KEYS = ("LANG", "LC_ALL")
_SAFE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

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


def job_environment(home: str | Path, tmpdir: str | Path | None = None, path: str = _SAFE_PATH) -> dict[str, str]:
    """ALLOWLIST environment for a sandboxed process (S19, S23): a minimal
    PATH, the locale variables, Python safety flags and HOME pointing at a
    directory the process owns. Anything else in the server's environment
    (ANTHROPIC, OLLAMA and GEMINI keys, tokens) is never passed on."""
    env = {"PATH": path, "HOME": str(home)}
    for key, value in os.environ.items():
        if key in _SAFE_LOCALE_KEYS or key.startswith("LC_"):
            env[key] = value
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if tmpdir is not None:
        env["TMPDIR"] = str(tmpdir)
    return env


def python_read_paths(python: str | None = None) -> list[Path]:
    """The exact interpreter installation a job needs, found at runtime: the
    venv prefix (site-packages), the base installation the venv points at,
    and the resolved interpreter's own directory. Nothing broader."""
    exe = python or sys.executable
    found: list[Path] = []
    for raw in (sys.prefix, sys.base_prefix, os.path.dirname(os.path.realpath(exe)), os.path.dirname(exe)):
        with contextlib.suppress(OSError):
            path = Path(raw).resolve()
            if path not in found:
                found.append(path)
    return found


def _under(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _profile_lines(
    read_dirs: list[Path],
    write_dirs: list[Path],
    allow_network: bool,
    system_reads: tuple[str, ...],
) -> list[str]:
    home = Path.home().resolve()
    lines: list[str] = [
        "(version 1)",
        "(deny default)",
        # --- process / runtime basics ---
        "(allow process-exec*)",
        "(allow process-fork)",
        "(allow process-info-pidinfo (target self))",
        "(allow process-info-setcontrol (target self))",
        "(allow signal (target self))",
        "(allow sysctl-read)",
        "(allow ipc-posix-sem)",
        # stat() on any path (existence and size only, never contents or
        # directory listings) so the interpreter can resolve paths through
        # parent directories it may not read.
        "(allow file-read-metadata)",
        # --- reads: DEFAULT DENY, then an explicit allowlist ---
        # The root directory itself (path resolution reads it), not its contents.
        '(allow file-read-data (literal "/"))',
    ]
    for sub in system_reads:
        lines.append(f"(allow file-read* (subpath {_quote(sub)}))")
    for d in read_dirs:
        lines.append(f"(allow file-read* (subpath {_quote(str(d))}))")
    # --- credential and personal data denied even where an allowed
    # directory would cover it (belt and braces over default deny) ---
    for sub in _SENSITIVE_HOME_SUBDIRS:
        target = home / sub
        if any(_under(d, target) for d in read_dirs):
            continue  # an allowed directory lives inside it: denying it would cut the job's own folder off
        lines.append(f"(deny file-read* (subpath {_quote(str(target))}))")
    for name in _SENSITIVE_HOME_FILES:
        lines.append(f"(deny file-read* (literal {_quote(str(home / name))}))")
    for pat in _SECRET_FILENAME_REGEXES:
        lines.append(f'(deny file-read* (regex #"{pat}"))')
    # --- writes: only the given directories (+ device nodes) ---
    for allow_path in write_dirs:
        lines.append(f"(allow file-write* (subpath {_quote(str(allow_path))}))")
    for dev in _DEV_NODES:
        lines.append(f"(allow file-write* (literal {_quote(dev)}))")
    # --- network: denied unless the caller explicitly needs it ---
    lines.append("(allow network*)" if allow_network else "(deny network*)")
    return lines


def build_sandbox_profile(cwd: str, allow_network: bool = False) -> str:
    """Render the Seatbelt profile for a run_command shell in ``cwd``.

    The profile is generated (not a static string) because paths must be
    RESOLVED to their real form: Seatbelt matches resolved paths, and on
    macOS `/tmp` is a symlink to `/private/tmp` — an unresolv'ed deny on
    `/tmp/...` silently leaks (verified empirically; a deny on the resolved
    path blocks it).

    Reads are DEFAULT DENY (S23): system libraries and toolchains, the
    workspace, the cwd and the running interpreter. Writes go to the cwd
    only, never to the workspace at large, so a command cannot rewrite
    Dourmouse's own state or the approved self-extension files.
    """
    ws = _workspace_root().resolve()
    cwd_path = Path(cwd).expanduser().resolve()
    read_dirs = [ws, cwd_path, *python_read_paths()]
    lines = _profile_lines(read_dirs, [cwd_path], allow_network, _SYSTEM_READ_SUBPATHS + _TOOLCHAIN_READ_SUBPATHS)
    lines.append(f"(deny file-write* (subpath {_quote(str(ws / 'self_extensions'))}))")
    return "\n".join(lines)


def build_job_profile(job_dir: str | Path, python: str | None = None, allow_network: bool = False) -> str:
    """Seatbelt profile for a model-written compute job (S19).

    DEFAULT DENY for reads with an explicit allowlist: system libraries and
    frameworks, the exact Python installation and site-packages discovered
    at runtime, and the job directory. Writes only inside the job
    directory. Network is denied unless ``allow_network`` (reserved for a
    future approved option; nothing in the job spec can set it).
    """
    job = Path(job_dir).expanduser().resolve()
    return "\n".join(_profile_lines([job, *python_read_paths(python)], [job], allow_network, _SYSTEM_READ_SUBPATHS))


def sandbox_exe() -> str | None:
    exe = shutil.which("sandbox-exec")
    return exe if exe is not None and os.access(exe, os.X_OK) else None


def write_profile(profile: str) -> str:
    """Write a profile to a temp file for ``sandbox-exec -f`` (the caller
    unlinks it once the process has started or finished)."""
    fd, profile_path = tempfile.mkstemp(prefix="dourmouse-sb-", suffix=".sb")
    with os.fdopen(fd, "w") as fh:
        fh.write(profile)
    return profile_path


def sandbox_available() -> bool:
    """True if sandbox-exec exists and is executable on this system."""
    exe = shutil.which("sandbox-exec")
    return exe is not None and os.access(exe, os.X_OK)


def _run_path() -> str:
    """PATH for run_command shells: the system directories plus the
    toolchain directories the profile lets them read."""
    extra = [p + "/bin" for p in _TOOLCHAIN_READ_SUBPATHS[:2]]
    return ":".join([*extra, _SAFE_PATH])


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
        # Own process group: on a timeout the whole group is killed, not
        # just the shell, so background children do not outlive the command.
        with subprocess.Popen(  # noqa: S603 -- fixed sandbox-exec, model command runs inside it
            [exe, "-f", profile_path, "/bin/sh", "-c", command],
            cwd=cwd,
            env=job_environment(os.environ.get("HOME") or Path.home(), path=_run_path()),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            start_new_session=True,
        ) as proc:
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
                raise
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {timeout}s (sandboxed)."
    except OSError as exc:
        return f"ERROR: could not run sandboxed command: {exc}"
    finally:
        with contextlib.suppress(OSError):
            os.unlink(profile_path)

    out = (stdout or "").strip()
    err = (stderr or "").strip()
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


def host_environment() -> dict[str, str]:
    """Environment for a run the owner approved to happen OUTSIDE the sandbox
    (finding #137): the real HOME and a PATH with the toolchain directories, but
    none of the server's secrets."""
    return job_environment(os.environ.get("HOME") or Path.home(), path=_run_path())


_PY_MAX_FILE_BYTES = 256 * 1024 * 1024  # RLIMIT_FSIZE: the biggest single file a snippet may write


def _limit_python_run() -> None:  # pragma: no cover -- runs in the child between fork and exec
    import resource

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (_PY_MAX_FILE_BYTES, _PY_MAX_FILE_BYTES))
    resource.setrlimit(resource.RLIMIT_NOFILE, (1024, 1024))


def format_run(returncode: int, stdout: str, stderr: str) -> str:
    """The run_python result text: exit code, then stdout and stderr, capped."""
    out = (stdout or "").strip()
    err = (stderr or "").strip()
    parts = [f"EXIT CODE: {returncode}"]
    if out:
        parts.append("STDOUT:\n" + out[-_OUTPUT_CAP:] + ("\n[output truncated]" if len(out) > _OUTPUT_CAP else ""))
    if err:
        parts.append("STDERR:\n" + err[:_OUTPUT_CAP])
    return "\n".join(parts)


def run_python_sandboxed(code: str, workdir: str | Path, timeout: int = 30, allow_network: bool = False) -> str:
    """Run model-written Python in the same sandbox compute jobs use (finding
    #137): reads only the system, the interpreter and ``workdir``; writes only
    ``workdir``; no network unless ``allow_network``; an allowlist environment
    with no keys; CPU and file-size limits; the whole process group killed on
    a timeout. NEVER runs unsandboxed: without sandbox-exec it says so."""
    exe = sandbox_exe()
    if exe is None:
        return (
            "NOT CONFIGURED: sandboxed execution needs macOS sandbox-exec, which is unavailable. "
            "Nothing was run. run_python_host runs code outside the sandbox after you approve it."
        )
    job = Path(workdir).expanduser().resolve()
    job.mkdir(parents=True, exist_ok=True)
    tmp = job / "tmp"
    tmp.mkdir(exist_ok=True)
    profile_path = write_profile(build_job_profile(job, allow_network=allow_network))
    cpu = timeout + 5

    def _limits() -> None:  # pragma: no cover -- child side
        import resource

        _limit_python_run()
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))

    try:
        with subprocess.Popen(  # noqa: S603 -- fixed sandbox-exec; the model's code runs inside it
            [exe, "-f", profile_path, sys.executable, "-I", "-B", "-c", code],
            cwd=str(job), env=job_environment(job, tmpdir=tmp),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            start_new_session=True, preexec_fn=_limits,  # noqa: PLW1509 -- resource limits must be set before exec
        ) as proc:
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
                raise
    except subprocess.TimeoutExpired:
        return f"ERROR: code timed out after {timeout}s (sandboxed)."
    except OSError as exc:
        return f"ERROR: could not run python: {exc}"
    finally:
        with contextlib.suppress(OSError):
            os.unlink(profile_path)
    return format_run(proc.returncode, stdout, stderr)
