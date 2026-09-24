"""Response actions (MS-8, spec items 30-33; finding #106).

Detection says what is wrong; these act on it. Every action here changes
the machine, so every chat tool that calls one is REQUIRES_CONFIRMATION
(the owner approves each one), and every action is built to be undoable
or refused:

- kill_process: SIGTERM, then SIGKILL after a grace period. Refuses the
  processes whose death would take the Mac or Dourmouse down with them,
  and refuses when the pid now belongs to a different program than the one
  approved (pids are reused).
- quarantine_file: MOVES the file (never deletes it) into Dourmouse's
  quarantine folder with a manifest (original path, sha256, why), and strips
  its execute permission. restore() puts it back.
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

def kill_process(pid: int, *, expect_name: str | None = None, grace: float = 3.0) -> dict[str, Any]:
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
    if expect_name and name != expect_name:
        raise ResponseRefused(f"pid {pid} is now {name!r}, not {expect_name!r}: the pid was reused, nothing killed")
    if name in PROTECTED_NAMES:
        raise ResponseRefused(f"{name} is part of macOS; killing it would log you out or hang the Mac")
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


def quarantine_file(path: str | Path, *, reason: str = "") -> dict[str, Any]:
    src = Path(os.path.abspath(os.path.expanduser(str(path))))
    _check_quarantinable(src)
    qid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    from .privacy import private_dir

    box = quarantine_dir() / qid
    private_dir(quarantine_dir())
    box.mkdir()
    manifest = {"id": qid, "original_path": str(src), "name": src.name, "is_dir": src.is_dir(),
                "sha256": _sha256(src), "size": src.stat().st_size if src.is_file() else None,
                "mode": stat.S_IMODE(src.stat().st_mode), "reason": reason, "quarantined_at": time.time()}
    try:
        shutil.move(str(src), str(box / src.name))
    except OSError as exc:
        box.rmdir()
        raise ResponseRefused(f"could not move {src}: {exc}") from exc
    moved = box / src.name
    if moved.is_file():
        moved.chmod(0o400)  # readable for inspection, no longer runnable
    (box / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {"ok": True, **manifest, "now_at": str(moved)}


def list_quarantine() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    root = quarantine_dir()
    if not root.is_dir():
        return out
    for m in sorted(root.glob("*/manifest.json"), reverse=True):
        try:
            out.append(json.loads(m.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def restore(qid: str) -> dict[str, Any]:
    if not qid or "/" in qid or qid.startswith("."):
        raise ResponseRefused("not a quarantine id")
    box = quarantine_dir() / qid
    try:
        manifest = json.loads((box / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ResponseRefused(f"no quarantine entry {qid}") from None
    dest = Path(manifest["original_path"])
    if dest.exists():
        raise ResponseRefused(f"something already exists at {dest}; move it first, nothing restored")
    dest.parent.mkdir(parents=True, exist_ok=True)
    item = box / manifest["name"]
    if item.is_file():
        item.chmod(manifest.get("mode", 0o644))
    shutil.move(str(item), str(dest))
    (box / "manifest.json").unlink()
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
