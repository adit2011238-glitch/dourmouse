"""Response actions (MS-8, spec items 30-33; finding #106).

Detection says what is wrong; these act on it. Every action here changes
the machine, so every chat tool that calls one is REQUIRES_CONFIRMATION
(the owner approves each one), and every action is built to be undoable
or refused:

- kill_process: SIGTERM, then SIGKILL after a grace period. Refuses the
  processes whose death would take the Mac or Dourmouse down with them,
  and refuses when the pid now belongs to a different program than the one
  approved (pids are reused): the caller must give the identity it captured
  when it found the process (name, start time, executable), and every part
  given is re-checked before any signal is sent.
- quarantine_file: MOVES the file (never deletes it) into Dourmouse's
  quarantine folder with a manifest (original path, sha256, why) kept beside
  the item, never mixed with it. A file has its execute permission removed;
  a folder or app bundle has it removed from every file inside, and a bundle
  is renamed so LaunchServices no longer treats it as an app. restore() puts
  the name, the modes and the item back exactly.
- disable_startup_item: unloads a launch agent and quarantines its plist,
  so it is undone by restore(). A system-wide item needs root; for those
  the exact commands are returned for the owner to run, never run here.

Nothing here ever uses sudo or asks for a password.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from dourmouse.config import workspace_dir

#: Killing any of these logs the user out, hangs the display, or kills
#: Dourmouse itself. They are never a legitimate response target.
PROTECTED_NAMES = frozenset({
    "launchd", "kernel_task", "WindowServer", "loginwindow", "logd", "securityd", "opendirectoryd",
    "configd", "mDNSResponder", "coreservicesd", "notifyd", "powerd", "syslogd", "UserEventAgent",
    "Dock", "Finder", "SystemUIServer", "tccd", "trustd", "diskarbitrationd", "fseventsd",
})
#: Never quarantined: the operating system and its own apps.
PROTECTED_PREFIXES = ("/System/", "/usr/", "/bin/", "/sbin/", "/private/var/db/", "/Library/Apple/",
                      "/etc/", "/private/etc/", "/dev/")


class ResponseRefused(Exception):
    """The action was not taken; the message says why."""


def quarantine_dir() -> Path:
    return workspace_dir() / "security" / "quarantine"


# --------------------------------------------------------------------------- #
# Kill a process
# --------------------------------------------------------------------------- #

def process_identity(pid: int) -> dict[str, Any]:
    """Who a pid is right now, to be captured when a process is found or
    shown for approval and handed back to kill_process."""
    import psutil

    try:
        proc = psutil.Process(pid)
        return {"pid": pid, "name": proc.name(), "exe": _safe(proc.exe), "user": _safe(proc.username),
                "create_time": _create_time(proc)}
    except psutil.NoSuchProcess:
        raise ResponseRefused(f"no process {pid} is running (it may already have exited)") from None


def _create_time(proc: Any) -> float | None:
    try:
        return float(proc.create_time())
    except Exception:  # noqa: BLE001 -- AccessDenied/ZombieProcess: the start time is just unknown
        return None


def kill_process(pid: int, *, expect_name: str | None = None, expect_create_time: float | None = None,
                 expect_exe: str | None = None, grace: float = 3.0) -> dict[str, Any]:
    import psutil

    if pid <= 1:
        raise ResponseRefused(f"pid {pid} is the system itself")
    if pid in (os.getpid(), os.getppid()):
        raise ResponseRefused("that is Dourmouse itself")
    try:
        proc = psutil.Process(pid)
        name, exe, user = proc.name(), _safe(proc.exe), _safe(proc.username)
    except psutil.NoSuchProcess:
        raise ResponseRefused(f"no process {pid} is running (it may already have exited)") from None
    if name in PROTECTED_NAMES:
        raise ResponseRefused(f"{name} is part of macOS; killing it would log you out or hang the Mac")
    if not (expect_name or expect_exe or expect_create_time is not None):
        raise ResponseRefused(f"no identity was given for pid {pid} ({name}); pids are reused, so nothing was killed")
    if expect_name and name != expect_name:
        raise ResponseRefused(f"pid {pid} is now {name!r}, not {expect_name!r}: the pid was reused, nothing killed")
    if expect_exe and exe and exe != expect_exe:
        raise ResponseRefused(f"pid {pid} now runs {exe}, not {expect_exe}: the pid was reused, nothing killed")
    if expect_create_time is not None:
        started = _create_time(proc)
        if started is None or abs(started - expect_create_time) > 1.0:
            raise ResponseRefused(f"pid {pid} was not started when the approved process was: the pid was reused "
                                  "or its start time cannot be read, nothing killed")
    try:
        proc.terminate()
        try:
            proc.wait(timeout=grace)
            how = "stopped (SIGTERM)"
        except psutil.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=grace)
            how = f"force-killed (SIGKILL after {grace:g}s)"
    except psutil.AccessDenied:
        return {"ok": False, "pid": pid, "name": name, "exe": exe,
                "needs_root": f"sudo kill -{int(signal.SIGTERM)} {pid}",
                "note": f"{name} runs as {user}; stopping it needs root. Run the command yourself if you are sure."}
    except psutil.NoSuchProcess:
        how = "already exited"
    return {"ok": True, "pid": pid, "name": name, "exe": exe, "result": how}


def _safe(fn: Any) -> str | None:
    try:
        return str(fn())
    except Exception:  # noqa: BLE001 -- AccessDenied/ZombieProcess: the field is just unknown
        return None


# --------------------------------------------------------------------------- #
# Quarantine
# --------------------------------------------------------------------------- #

def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _check_quarantinable(path: Path) -> None:
    text = str(path) + ("/" if path.is_dir() else "")
    system = any(text.startswith(p) for p in PROTECTED_PREFIXES) and not text.startswith("/usr/local/")
    if system or text.startswith("/Applications/Utilities/"):
        raise ResponseRefused(f"{path} is part of macOS and is never quarantined")
    if path.is_symlink():
        raise ResponseRefused(f"{path} is a link; quarantine the file it points to instead")
    if not path.exists():
        raise ResponseRefused(f"{path} does not exist")
    q = quarantine_dir().resolve()
    real = path.resolve()
    if real == q or q in real.parents:
        raise ResponseRefused(f"{path} is already in quarantine")
    if path == Path.home() or path in Path.home().parents or path.is_mount():
        raise ResponseRefused(f"{path} is a whole home folder or disk, not a file")


#: A folder with one of these extensions is treated as a program by macOS;
#: renamed inside quarantine so it cannot be opened or launched from there.
BUNDLE_SUFFIXES = frozenset({".app", ".appex", ".bundle", ".plugin", ".prefpane", ".saver", ".kext", ".xpc",
                             ".framework", ".workflow"})
QUARANTINE_SUFFIX = ".quarantined"
_RUN_BITS = 0o7111  # execute for anyone, plus setuid and setgid


def _run_bit_files(root: Path) -> dict[str, int]:
    """Relative path -> mode of every regular file under root that could run."""
    found: dict[str, int] = {}
    for base, _dirs, files in os.walk(root):
        for f in files:
            full = Path(base) / f
            st = full.lstat()
            if stat.S_ISREG(st.st_mode) and stat.S_IMODE(st.st_mode) & _RUN_BITS:
                found[str(full.relative_to(root))] = stat.S_IMODE(st.st_mode)
    return found


def _neutralize(moved: Path, run_files: dict[str, int]) -> None:
    if moved.is_dir():
        for rel, mode in run_files.items():
            (moved / rel).chmod(mode & ~_RUN_BITS)
    else:
        moved.chmod(0o400)  # readable for inspection, no longer runnable


def quarantine_file(path: str | Path, *, reason: str = "") -> dict[str, Any]:
    src = Path(os.path.abspath(os.path.expanduser(str(path))))
    _check_quarantinable(src)
    qid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    from .privacy import atomic_write_text, private_dir

    box = quarantine_dir() / qid
    private_dir(quarantine_dir())
    stored_name = src.name + (QUARANTINE_SUFFIX if src.is_dir() and src.suffix.lower() in BUNDLE_SUFFIXES else "")
    run_files = _run_bit_files(src) if src.is_dir() else {}
    manifest = {"id": qid, "original_path": str(src), "name": src.name, "stored_name": stored_name,
                "layout": 2, "is_dir": src.is_dir(), "sha256": _sha256(src),
                "size": src.stat().st_size if src.is_file() else None, "mode": stat.S_IMODE(src.stat().st_mode),
                "run_files": run_files, "reason": reason, "quarantined_at": time.time()}
    moved = box / "item" / stored_name
    try:
        (box / "item").mkdir(parents=True)
        atomic_write_text(box / "manifest.json", json.dumps(manifest, indent=2))
        shutil.move(str(src), str(moved))
    except OSError as exc:
        if src.exists():
            shutil.rmtree(box)  # the item never left home: only our bookkeeping is in the box
        raise ResponseRefused(f"could not move {src}: {exc}") from exc
    try:
        _neutralize(moved, run_files)
    except OSError as exc:
        # Never report an item as neutralised when it is not: put it back.
        _set_run_bits(moved, run_files)
        shutil.move(str(moved), str(src))
        shutil.rmtree(box)
        raise ResponseRefused(f"could not make {src.name} non-executable, so it was left where it was: {exc}") from exc
    return {"ok": True, **manifest, "now_at": str(moved)}


def _set_run_bits(item: Path, run_files: dict[str, int]) -> None:
    for rel, mode in run_files.items():
        target = item / rel
        if target.is_file() and not target.is_symlink():
            target.chmod(mode)


def _read_manifest(box: Path, qid: str) -> dict[str, Any]:
    """The manifest of a quarantine box, checked: its id is the box's name,
    the original path is absolute, and the name is that path's own name."""
    try:
        manifest = json.loads((box / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ResponseRefused(f"no quarantine entry {qid}") from None
    name = manifest.get("name") if isinstance(manifest, dict) else None
    original = manifest.get("original_path") if isinstance(manifest, dict) else None
    if (not isinstance(name, str) or not isinstance(original, str) or manifest.get("id") != qid
            or not os.path.isabs(original) or Path(original).name != name or name in ("", ".", "..")):
        raise ResponseRefused(f"quarantine entry {qid} has a damaged manifest, nothing restored")
    return manifest


def list_quarantine() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    root = quarantine_dir()
    if not root.is_dir():
        return out
    for m in sorted(root.glob("*/manifest.json"), reverse=True):
        try:
            manifest = _read_manifest(m.parent, m.parent.name)
        except ResponseRefused:
            continue
        out.append(manifest)
    return out


def restore(qid: str) -> dict[str, Any]:
    if not qid or "/" in qid or qid.startswith("."):
        raise ResponseRefused("not a quarantine id")
    box = quarantine_dir() / qid
    manifest = _read_manifest(box, qid)
    dest = Path(manifest["original_path"])
    if dest.exists():
        raise ResponseRefused(f"something already exists at {dest}; move it first, nothing restored")
    stored = manifest.get("stored_name") or manifest["name"]
    if stored not in (manifest["name"], manifest["name"] + QUARANTINE_SUFFIX):
        raise ResponseRefused(f"quarantine entry {qid} has a damaged manifest, nothing restored")
    item = box / "item" / stored if manifest.get("layout") == 2 else box / manifest["name"]
    run_files = manifest.get("run_files") or {}
    if not isinstance(run_files, dict) or any(
            not isinstance(rel, str) or rel.startswith("/") or ".." in Path(rel).parts or not isinstance(mode, int)
            for rel, mode in run_files.items()):
        raise ResponseRefused(f"quarantine entry {qid} has a damaged manifest, nothing restored")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if item.is_dir():
        _set_run_bits(item, {rel: mode & 0o7777 for rel, mode in run_files.items()})
    elif item.is_file():
        item.chmod(int(manifest.get("mode", 0o644)) & 0o777)
    shutil.move(str(item), str(dest))
    (box / "manifest.json").unlink()
    if manifest.get("layout") == 2:
        (box / "item").rmdir()
    box.rmdir()
    return {"ok": True, "restored_to": str(dest), "id": qid}


# --------------------------------------------------------------------------- #
# Startup items (launch agents and daemons)
# --------------------------------------------------------------------------- #

def user_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=15, check=False)


def disable_startup_item(path: str | Path, *, reason: str = "") -> dict[str, Any]:
    from . import mac_telemetry as mt

    p = Path(os.path.abspath(os.path.expanduser(str(path))))
    dirs = [d.resolve() if d.exists() else d for d in mt.persistence_dirs()]
    if p.suffix != ".plist" or p.parent.resolve() not in dirs:
        raise ResponseRefused(f"{p} is not a launch agent or daemon in a startup folder")
    if not p.exists():
        raise ResponseRefused(f"{p} does not exist")
    user_dir = p.parent.resolve() == user_agents_dir().resolve()
    if not user_dir:
        domain = "system" if "LaunchDaemons" in p.parent.name else f"gui/{os.getuid()}"
        dest = quarantine_dir() / "system-items"
        return {"ok": False, "path": str(p), "needs_root": [
            f"sudo launchctl bootout {domain} '{p}'", f"mkdir -p '{dest}'", f"sudo mv '{p}' '{dest}/'"],
            "note": "This startup item is system-wide; removing it needs root. Run these yourself if you are sure."}
    unload = _launchctl("bootout", f"gui/{os.getuid()}", str(p))
    q = quarantine_file(p, reason=reason or "startup item disabled")
    loaded = "unloaded" if unload.returncode == 0 else "was not running"
    return {"ok": True, "path": str(p), "result": f"{loaded}; plist quarantined as {q['id']} (restore undoes this)",
            "quarantine_id": q["id"]}
