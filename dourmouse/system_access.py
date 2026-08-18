"""Laptop-wide access tools for the General Dispatch Agent (RUN:GENERAL).

Adds a ``system`` subagent that can operate across the whole machine — not
just the workspace sandbox — so the dispatcher behaves like Claude Cowork:
read/write/list/delete files anywhere, run shell commands, open files and
apps, read the clipboard, and report real system metrics.

SAFETY MODEL (master prompt Section 2.9, enforced deterministically — never
an LLM judgment call, Rule 2.8):

- Reads and local changes (read_path, list_path, write_path, system_info,
  clipboard_get/set, open_path) are REGULAR: they proceed without asking.
- Deletion (delete_path) is REQUIRES_CONFIRMATION per item, engine-enforced.
- run_command is REGULAR but runs inside a kernel-enforced sandbox
  (macOS sandbox-exec / Seatbelt, see sandbox.py): it structurally cannot
  reach credential dirs, cannot write outside the workspace + cwd, and has
  network denied by default. A deterministic danger classifier runs first as
  a cheap fast-path pre-filter (sudo, git push, rm, global package installs,
  remote code via curl|sh, device writes, disk formatting, power control,
  kill-all) — commands it refuses must be re-run through the
  confirmation-gated run_privileged_command, which surfaces in the UI's
  INTERVENTIONS column for explicit human approval.
- Credential/system paths (~/.ssh, ~/.aws, ~/.gnupg, /etc, /usr, /System,
  /Library/Keychains) are never READ, written to, or deleted (Rule 2.6).

SAFETY UPGRADE (v2.0 build prompt Phase 0): the sensitive-path gate is now
applied to ALL four file tools — read_path previously lacked it. Beyond
credential/system directories, filenames that are themselves credentials are
blocked anywhere: .env*, *.pem, *.key, bare id_rsa/id_ed25519-style keys,
.netrc, .npmrc, .pgpass (see _SENSITIVE_FILENAME_PATTERNS). This closes the
real gap where read_path("~/Downloads/id_rsa") or read_path("<project>/.env")
would have silently shipped secrets to the model.

SAFETY UPGRADE (v2.0 build prompt Phase 1): run_command now executes inside
a kernel-enforced sandbox (sandbox.py, sandbox-exec / Seatbelt). The danger
classifier below is now a FAST-PATH PRE-FILTER, not the safety guarantee —
commands that dodge it (python3 -c "os.remove(...)", find ~/.ssh -delete,
cat ~/.aws/credentials) are blocked at the OS level by the sandbox instead.
run_privileged_command stays UNSANDBOXED by design: its entire purpose is
"run exactly this after a human explicitly approved exactly this command."
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from dourmouse.dispatch import Permission, Subagent, ToolSpec
from dourmouse.sandbox import run_sandboxed

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_CWD = str(_PROJECT_ROOT)
_OUTPUT_CAP = 20_000  # chars of command output returned to the model

# Paths that must never be read, written to, or deleted by the agent
# (credentials / system dirs). System roots are matched as path prefixes;
# credential directories are matched by ANY path component (works for any
# home dir, e.g. /Users/me/.ssh or /Users/aditagrawal/.ssh).
_SYSTEM_ROOT_PARTS = (
    "/etc",
    "/usr",
    "/System",
    "/Library/Keychains",
    "/private/etc",
    # Windows system roots (matched case-insensitively — see _is_sensitive).
    # Without these the guard silently misses C:\Windows\... on Windows,
    # which is exactly the credential/system hole this gate exists to close.
    r"C:\Windows",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\ProgramData",
)
_SENSITIVE_COMPONENTS = {".ssh", ".aws", ".gnupg", ".kube", ".docker", "Keychains"}

# Filenames that are themselves credentials, matched regardless of directory
# (v2.0 Phase 0): .env* (API keys in any project root, incl. this repo's own
# .env), key/cert files, bare SSH key names that can live outside .ssh
# (e.g. downloaded to ~/Downloads), and classic netrc/npmrc/pgpass.
_SENSITIVE_FILENAME_PATTERNS = (
    re.compile(r"^\.env(\..*)?$"),
    re.compile(r"\.pem$"),
    re.compile(r"\.key$"),
    re.compile(r"^id_(rsa|ed25519|ecdsa|dsa)$"),
    re.compile(r"^\.netrc$"),
    re.compile(r"^\.npmrc$"),
    re.compile(r"^\.pgpass$"),
)


# --------------------------------------------------------------------------- #
# Deterministic danger classifier for run_command (Rule 2.8)
# --------------------------------------------------------------------------- #

_DANGEROUS_COMMAND_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(sudo|doas|pkexec)\b|\bsu\s+-"), "privilege escalation"),
    (re.compile(r"\bgit\s+push\b"), "remote git push (irreversible)"),
    # rm as a deletion verb requires a following arg/whitespace — a filename
    # containing "rm" (cat rm.txt, ls /etc/rm-dir) is NOT deletion.
    (re.compile(r"\brm(?=\s|$)"), "file/directory deletion (use delete_path or run_privileged_command)"),
    (re.compile(r"\brmdir(?=\s|$)"), "directory removal"),
    (
        re.compile(r"\b(curl|wget)\b[^|;]*\|\s*(sh|bash|zsh|dash)\b"),
        "remote code execution via pipe",
    ),
    # These package managers install globally BY DEFAULT — block unconditionally.
    (
        re.compile(r"\b(brew|apt-get|apt|dnf|yum)\s+(install|remove|update|upgrade|uninstall)\b"),
        "system package manager operation (global by default)",
    ),
    # pip/npm are local by default; only the global flag is blocked.
    (
        re.compile(r"\b(pip|pip3|npm|yarn|pnpm|composer|gem)\s+.*\s(-g|--global)\b"),
        "global package installation",
    ),
    (re.compile(r"\bdd\b.*\bof=/dev/"), "direct write to a device"),
    (re.compile(r"\b(mkfs|fdisk|diskutil)\b"), "disk partitioning/formatting"),
    (re.compile(r"\b(shutdown|reboot|poweroff|halt|init|launchctl)\b"), "system power/service control"),
    (re.compile(r"\b(killall|pkill)\b"), "killing processes"),
    (re.compile(r"\bkill\s+-9?\s+(-1|0)\b"), "kill-all processes"),
    (re.compile(r"\bchmod\b.*\s(/|/usr|/etc|/System)"), "recursive permission change on system roots"),
    (
        re.compile(r"(>|>>)\s*(/etc/|/usr/|/System/|/Library/Keychains)"),
        "redirect overwrite into a system path",
    ),
    # Phase 0 interim read-side hardening (explicitly acknowledged as still
    # bypassable — Phase 1's sandbox replaces this class of protection):
    (
        re.compile(r"\bcat\b.*\.(ssh|aws|gnupg|kube|docker)\b"),
        "reading credential directory contents",
    ),
    (
        re.compile(r"\bos\.remove\b|\bos\.unlink\b|shutil\.rmtree"),
        "programmatic file deletion via interpreter",
    ),
]


def classify_command(command: str) -> tuple[bool, str]:
    """Return (blocked, reason) for a shell command. Pure, deterministic."""
    for pattern, reason in _DANGEROUS_COMMAND_PATTERNS:
        if pattern.search(command):
            return True, reason
    return False, ""


# --------------------------------------------------------------------------- #
# Path guards
# --------------------------------------------------------------------------- #

def _resolve_abs(raw: str) -> Path | None:
    """Expand ~ and require an absolute path (full-laptop scope)."""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        return None
    return path


def _is_sensitive(path: Path) -> bool:
    """True if the path is inside a credential/system root (write/delete).

    System roots match by prefix; credential dirs (.ssh, .aws, .gnupg, ...)
    match by any path component so the guard works for any home directory
    or symlinked path.
    """
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    if any(comp in _SENSITIVE_COMPONENTS for comp in resolved.parts):
        return True
    # A filename that is itself a credential (.env, *.pem, *.key, id_rsa,
    # .netrc, .npmrc, .pgpass) is sensitive in ANY directory — the directory
    # guard alone missed e.g. a downloaded key or a project-root .env.
    # NOTE: `search`, not `match` — the suffix patterns ("*.pem", "*.key")
    # must match ANY file ending that way, not only names STARTING with them.
    if any(pat.search(resolved.name) for pat in _SENSITIVE_FILENAME_PATTERNS):
        return True
    # gcloud credentials live under .config/gcloud — match the slash-delimited
    # tail so a legit path like ~/.config/gcloud-sandbox/ doesn't false-positive.
    if "/.config/gcloud/" in str(resolved) + "/":
        return True
    for root in _SYSTEM_ROOT_PARTS:
        try:
            if os.name == "nt" and root.startswith("C:"):
                # Windows paths are case-insensitive; relative_to is not.
                if str(resolved).lower().startswith(root.lower()):
                    return True
            else:
                resolved.relative_to(Path(root))
                return True
        except ValueError:
            continue
    return False


# --------------------------------------------------------------------------- #
# File tools (full laptop scope)
# --------------------------------------------------------------------------- #

def _read_path_tool(arguments: dict[str, Any]) -> str:
    raw = arguments.get("path", "")
    target = _resolve_abs(raw)
    if target is None:
        return "ERROR: read_path requires an ABSOLUTE path (e.g. /Users/you/file.txt)."
    if _is_sensitive(target):
        return (
            f"REFUSED: {target} is inside a credential/system directory "
            "(Rule 2.6) — the agent never reads there."
        )
    if not target.is_file():
        return f"ERROR: no such file: {target}"
    try:
        return target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"ERROR: could not read {target}: {exc}"


def _list_path_tool(arguments: dict[str, Any]) -> str:
    raw = arguments.get("path", ".")
    target = _resolve_abs(raw)
    if target is None:
        # Allow relative paths here: they resolve against the project root.
        target = Path(raw).expanduser()
        if not target.is_absolute():
            target = _PROJECT_ROOT / target
    if not target.is_dir():
        return f"ERROR: not a directory: {target}"
    try:
        entries = sorted(
            p.name + ("/" if p.is_dir() else "") for p in target.iterdir()
        )
    except OSError as exc:
        return f"ERROR: could not list {target}: {exc}"
    return f"LISTING {target}:\n" + ("\n".join(entries) if entries else "(empty)")


def _write_path_tool(arguments: dict[str, Any]) -> str:
    raw = arguments.get("path", "")
    target = _resolve_abs(raw)
    if target is None:
        return "ERROR: write_path requires an ABSOLUTE path."
    if _is_sensitive(target):
        return (
            f"REFUSED: {target} is inside a credential/system directory "
            "(Rule 2.6) — the agent never writes there."
        )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        content = arguments.get("content", "")
        target.write_text(content)
    except OSError as exc:
        return f"ERROR: could not write {target}: {exc}"
    return f"WROTE {target} ({len(arguments.get('content', ''))} chars)"


def _delete_path_tool(arguments: dict[str, Any]) -> str:
    # Engine enforces REQUIRES_CONFIRMATION BEFORE this handler runs.
    raw = arguments.get("path", "")
    target = _resolve_abs(raw)
    if target is None:
        return "ERROR: delete_path requires an ABSOLUTE path."
    if _is_sensitive(target):
        return (
            f"REFUSED: {target} is inside a credential/system directory "
            "(Rule 2.6) — the agent never deletes there."
        )
    if not target.is_file():
        return f"ERROR: not a file (or missing): {target}"
    try:
        target.unlink()
    except OSError as exc:
        return f"ERROR: could not delete {target}: {exc}"
    return f"DELETED {target}"


# --------------------------------------------------------------------------- #
# Terminal tools
# --------------------------------------------------------------------------- #

def _run_shell(command: str, cwd: str, timeout: int) -> str:
    proc = subprocess.run(
        command,
        shell=True,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    parts = [f"EXIT CODE: {proc.returncode}"]
    if out:
        truncated = out[-_OUTPUT_CAP:]
        parts.append("STDOUT:\n" + truncated + ("\n[output truncated]" if len(out) > _OUTPUT_CAP else ""))
    if err:
        parts.append("STDERR:\n" + err[:_OUTPUT_CAP])
    return "\n".join(parts)


def _run_command_tool(arguments: dict[str, Any]) -> str:
    command = (arguments.get("command") or "").strip()
    if not command:
        return "ERROR: run_command requires a non-empty 'command'."
    blocked, reason = classify_command(command)
    if blocked:
        return (
            f"REFUSED by deterministic safety guard: {reason}. "
            "Run this through run_privileged_command for explicit human "
            "approval (it surfaces in the UI INTERVENTIONS column)."
        )
    try:
        timeout = min(int(arguments.get("timeout_seconds", 60)), 300)
    except (TypeError, ValueError):
        return "ERROR: timeout_seconds must be an integer."
    cwd = (arguments.get("cwd") or _DEFAULT_CWD).strip()
    # Phase 1: the REAL safety boundary is the kernel-enforced sandbox (see
    # sandbox.py). The classifier above is now only a fast-path pre-filter.
    # run_sandboxed NEVER silently falls back to unsandboxed execution — on
    # a system without sandbox-exec it returns NOT CONFIGURED (Rule 2.2).
    return run_sandboxed(command, cwd, timeout)


def _run_privileged_command_tool(arguments: dict[str, Any]) -> str:
    # Engine enforces REQUIRES_CONFIRMATION BEFORE this handler runs. Once a
    # human approves in the UI, ANY command executes — that is the point of
    # the escape hatch. No re-classification here.
    command = (arguments.get("command") or "").strip()
    if not command:
        return "ERROR: run_privileged_command requires a 'command'."
    try:
        timeout = min(int(arguments.get("timeout_seconds", 120)), 300)
    except (TypeError, ValueError):
        return "ERROR: timeout_seconds must be an integer."
    cwd = (arguments.get("cwd") or _DEFAULT_CWD).strip()
    try:
        return _run_shell(command, cwd, timeout)
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {timeout}s."
    except OSError as exc:
        return f"ERROR: could not run command: {exc}"


# --------------------------------------------------------------------------- #
# System info / apps / clipboard
# --------------------------------------------------------------------------- #

def _system_info_tool(arguments: dict[str, Any]) -> str:
    info = [
        f"PLATFORM: {platform.platform()}",
        f"MACHINE: {platform.machine()}",
        f"PYTHON: {platform.python_version()} ({sys.executable})",
        f"CPUS: {os.cpu_count() or 'unknown'}",
        f"HOSTNAME: {platform.node()}",
    ]
    # Best-effort memory + disk (stdlib only; honest when unavailable).
    try:
        if sys.platform == "darwin":
            mem = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            if mem.isdigit():
                info.append(f"MEMORY: {int(mem) / (1024 ** 3):.1f} GB")
        elif os.name == "nt":
            # os.sysconf is POSIX-only; use the GlobalMemoryStatusEx API.
            import ctypes

            class _MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            m = _MEMORYSTATUSEX()
            m.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
                info.append(f"MEMORY: {m.ullTotalPhys / (1024 ** 3):.1f} GB")
        else:
            mem = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
            if mem > 0:
                info.append(f"MEMORY: {mem / (1024 ** 3):.1f} GB")
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        pass
    try:
        usage = shutil.disk_usage(Path.home())
        info.append(
            f"DISK /home: {usage.free / (1024 ** 3):.1f} GB free "
            f"of {usage.total / (1024 ** 3):.1f} GB"
        )
    except OSError:
        pass
    return "\n".join(info)


def _open_path_tool(arguments: dict[str, Any]) -> str:
    raw = arguments.get("path", "")
    target = _resolve_abs(raw)
    if target is None:
        return "ERROR: open_path requires an ABSOLUTE path."
    if not target.exists():
        return f"ERROR: no such file or directory: {target}"
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", str(target)], check=True, timeout=15)
            return f"OPENED: {target} (Finder/default app)"
        # Linux/other: xdg-open best effort.
        subprocess.run(["xdg-open", str(target)], check=True, timeout=15)
        return f"OPENED: {target}"
    except (subprocess.SubprocessError, OSError) as exc:
        return f"ERROR: could not open {target}: {exc}"


def _clipboard_get_tool(arguments: dict[str, Any]) -> str:
    if sys.platform != "darwin":
        return "NOT CONFIGURED: clipboard read uses macOS pbpaste (this is not macOS)."
    try:
        proc = subprocess.run(
            ["pbpaste"], capture_output=True, text=True, timeout=10
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return f"ERROR: could not read clipboard: {exc}"
    text = proc.stdout
    return "CLIPBOARD CONTENT:\n" + (text[:_OUTPUT_CAP] if text else "(empty)")


def _clipboard_set_tool(arguments: dict[str, Any]) -> str:
    if sys.platform != "darwin":
        return "NOT CONFIGURED: clipboard write uses macOS pbcopy (this is not macOS)."
    try:
        proc = subprocess.run(
            ["pbcopy"], input=arguments.get("content", ""), text=True, timeout=10
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return f"ERROR: could not write clipboard: {exc}"
    if proc.returncode != 0:
        return f"ERROR: pbcopy exited {proc.returncode}"
    return "CLIPBOARD SET OK."


def _uploads_root() -> Path:
    """The HUD uploads sandbox (<workspace>/uploads), created on demand."""
    import os

    raw = os.environ.get("DOURMOUSE_WORKSPACE")
    root = Path(raw).expanduser() if raw else _PROJECT_ROOT / "workspace"
    up = root / "uploads"
    up.mkdir(parents=True, exist_ok=True)
    return up


def _check_connections_tool(arguments: dict[str, Any]) -> str:
    """v5.3: deterministic per-account connection status (read-only)."""
    from dourmouse.connections import format_connections

    return format_connections()


def _extract_pdf_tool(arguments: dict[str, Any]) -> str:
    """v5.x: extract text from a PDF by absolute path."""
    from dourmouse.extract import extract_pdf_text

    path = (arguments.get("path") or "").strip()
    if not path:
        return "ERROR: extract_pdf requires an absolute 'path'."
    try:
        return extract_pdf_text(path)
    except RuntimeError as exc:
        return f"EXTRACT PDF (reported honestly): {exc}"
    except Exception as exc:  # noqa: BLE001 - readable failure
        return f"EXTRACT PDF FAILED: {type(exc).__name__}: {exc}"


def _extract_receipt_tool(arguments: dict[str, Any]) -> str:
    """v5.x: parse a receipt/invoice PDF into structured fields."""
    from dourmouse.extract import extract_receipt

    path = (arguments.get("path") or "").strip()
    if not path:
        return "ERROR: extract_receipt requires an absolute 'path'."
    try:
        return extract_receipt(path)
    except RuntimeError as exc:
        return f"EXTRACT RECEIPT (reported honestly): {exc}"
    except Exception as exc:  # noqa: BLE001 - readable failure
        return f"EXTRACT RECEIPT FAILED: {type(exc).__name__}: {exc}"


def _read_upload_tool(arguments: dict[str, Any]) -> str:
    """v5.0: read a file the user uploaded through the HUD (/uploads/<name>).

    The uploads sandbox is the only allowed root — a name that escapes it is
    refused, never silently resolved elsewhere (Rule 2.6). Returns the file's
    text (or a binary/oversize hint) for the model to use.
    """
    name = (arguments.get("name") or "").strip()
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return "ERROR: read_upload needs a plain filename (no paths), e.g. report.pdf."
    target = (_uploads_root() / name).resolve()
    try:
        target.relative_to(_uploads_root().resolve())
    except ValueError:
        return f"REFUSED: {name!r} escapes the uploads sandbox."
    if not target.is_file():
        return f"ERROR: no such upload: {name} (upload it in the HUD first)."
    try:
        size = target.stat().st_size
        if size > 200_000:
            data = target.read_bytes()[:200_000]
            note = f"\n[NOTE: file is {size} bytes; showing first 200k of binary data]"
        else:
            data = target.read_bytes()
            note = ""
        text = data.decode("utf-8", errors="replace")
        if not text.strip() or "\x00" in text[:512]:
            return (
                f"UPLOAD {name} ({size} bytes): binary file — not readable as "
                f"text. Use write_path/run_command to process it if needed."
            )
        return f"UPLOAD {name} ({size} bytes):\n" + text[:200_000] + note
    except OSError as exc:
        return f"ERROR: could not read upload {name}: {exc}"


# --------------------------------------------------------------------------- #
# Subagent builder
# --------------------------------------------------------------------------- #

def build_system_subagent() -> Subagent:
    """The laptop-wide access subagent (Claude-Cowork-style scope)."""
    return Subagent(
        name="system",
        domain="General",
        description=(
            "Full laptop access: read/write/list/delete files anywhere, run "
            "shell commands (dangerous ones require confirmation), open "
            "files/apps, read clipboard, report system info."
        ),
        tools=(
            ToolSpec(
                name="read_path",
                description=(
                    "Read any text file on the laptop by ABSOLUTE path "
                    "(e.g. /Users/you/project/main.py)."
                ),
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
                handler=_read_path_tool,
            ),
            ToolSpec(
                name="read_upload",
                description=(
                    "Read a file the user uploaded in the HUD, by its plain "
                    "filename only (no paths). Only reads the uploads "
                    "sandbox."
                ),
                parameters={
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
                handler=_read_upload_tool,
            ),
            ToolSpec(
                name="extract_pdf",
                description=(
                    "Extract the words from a PDF document by absolute path "
                    "(receipts, invoices, reports). Needs the optional pypdf "
                    "extra installed; otherwise reports NOT CONFIGURED "
                    "honestly."
                ),
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
                handler=_extract_pdf_tool,
            ),
            ToolSpec(
                name="extract_receipt",
                description=(
                    "Parse a receipt/invoice PDF into structured fields "
                    "(vendor, date, total, line items). Best-effort regex; "
                    "reports fields it could NOT parse, never estimates them."
                ),
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
                handler=_extract_receipt_tool,
            ),
            ToolSpec(
                name="list_path",
                description="List a directory anywhere on the laptop (absolute path).",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string", "default": "."}},
                },
                handler=_list_path_tool,
            ),
            # v8.15: gated — same full-laptop scope as delete_path (already
            # gated, right below), no diff shown; a silent overwrite destroys
            # content exactly as delete_path's unlink does.
            ToolSpec(
                name="write_path",
                description=(
                    "Write (create/overwrite) any text file by absolute path. "
                    "Refused inside credential/system dirs (~/.ssh, /etc, ...). "
                    "REQUIRES human confirmation before it overwrites anything."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
                handler=_write_path_tool,
                permission=Permission.REQUIRES_CONFIRMATION,
                confirm_prompt=lambda a: (
                    f"Write {a.get('path', '?')!r} "
                    f"({len(a.get('content', ''))} chars)? This overwrites any "
                    "existing file at that path with no diff shown."
                ),
            ),
            ToolSpec(
                name="delete_path",
                description=(
                    "Delete ONE file anywhere by absolute path. REQUIRES "
                    "per-item human confirmation; refused in system dirs."
                ),
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
                handler=_delete_path_tool,
                permission=Permission.REQUIRES_CONFIRMATION,
                confirm_prompt=lambda a: f"Permanently delete {a.get('path')!r}?",
            ),
            ToolSpec(
                name="run_command",
                description=(
                    "Run a shell command on the laptop (default cwd: the "
                    "dispatch app root). A deterministic guard REFUSES "
                    "destructive/irreversible commands (sudo, rm, git push, "
                    "global installs, curl|sh, ...) — use "
                    "run_privileged_command for those."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "cwd": {"type": "string", "default": _DEFAULT_CWD},
                        "timeout_seconds": {"type": "integer", "default": 60},
                    },
                    "required": ["command"],
                },
                handler=_run_command_tool,
            ),
            ToolSpec(
                name="run_privileged_command",
                description=(
                    "Run ANY shell command after explicit human approval "
                    "(surfaces in the UI INTERVENTIONS column). Use when "
                    "run_command refuses a command the user genuinely wants."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "cwd": {"type": "string", "default": _DEFAULT_CWD},
                        "timeout_seconds": {"type": "integer", "default": 120},
                    },
                    "required": ["command"],
                },
                handler=_run_privileged_command_tool,
                permission=Permission.REQUIRES_CONFIRMATION,
                confirm_prompt=lambda a: f"Run shell command: {a.get('command')!r}?",
            ),
            ToolSpec(
                name="system_info",
                description="Report real OS/hardware info: platform, CPU, memory, disk.",
                parameters={"type": "object", "properties": {}},
                handler=_system_info_tool,
            ),
            ToolSpec(
                name="open_path",
                description=(
                    "Open a file or folder in Finder / its default app "
                    "(macOS). Use when the user wants to look at something."
                ),
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
                handler=_open_path_tool,
            ),
            ToolSpec(
                name="clipboard_get",
                description="Read the current clipboard text (macOS).",
                parameters={"type": "object", "properties": {}},
                handler=_clipboard_get_tool,
            ),
            ToolSpec(
                name="clipboard_set",
                description="Replace the clipboard with the given text (macOS).",
                parameters={
                    "type": "object",
                    "properties": {"content": {"type": "string"}},
                    "required": ["content"],
                },
                handler=_clipboard_set_tool,
            ),
            ToolSpec(
                name="check_connections",
                description=(
                    "Report which external accounts/services Dourmouse can "
                    "reach right now: ollama, nvidia, claude (Claude Code "
                    "CLI), codex (Codex CLI + login), gmail, freebuff "
                    "app/API, slack, alpaca, atlas repo. Read-only and "
                    "honest — a missing credential is reported as not "
                    "configured, never assumed."
                ),
                parameters={"type": "object", "properties": {}},
                handler=_check_connections_tool,
            ),
        ),
    )
