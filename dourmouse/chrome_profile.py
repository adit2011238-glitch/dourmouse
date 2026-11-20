"""The Dourmouse Chrome: a real Google Chrome with its own profile, driven over the DevTools protocol
(plan 4.6, T5 step 1, finding 214; owner decision 2026-11-13, option A).

* The profile folder is ``~/Library/Application Support/Dourmouse/Chrome Profile`` (private, 0700), outside the repo and
  the code backups, so Google session cookies never land in a backup. ``DOURMOUSE_CHROME_PROFILE_DIR`` moves it (tests).
* Before every launch the profile's preferences are written: password saving, automatic sign-in and address and card
  autofill off, continue where you left off. Dourmouse never types, reads or exports a password; the owner signs in by hand.
* Chrome is started as its own process (not through Playwright, whose launch closes Chrome when Dourmouse exits) with
  a random local control port. Its ``DevToolsActivePort`` file in the profile folder tells Dourmouse the port, and the
  port is trusted only when ``lsof`` says the process that owns it is the Chrome that holds that profile's lock.
* ``attach()`` connects Playwright over CDP, the call the Electron pane already uses, and works again after a Dourmouse
  restart because Chrome keeps running.
* ``status()`` reports what is true and says ``unknown`` where Chrome gives no answer.

Nothing here touches the owner's everyday Chrome profile.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

PROFILE_ENV = "DOURMOUSE_CHROME_PROFILE_DIR"
BINARY_ENV = "DOURMOUSE_CHROME_BINARY"
DEFAULT_BINARY = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
SIGNIN_URL = "https://accounts.google.com/"

#: Preferences written before each launch. Dotted keys are nested dictionaries in Chrome's ``Preferences`` file.
WANTED_PREFERENCES: dict[str, Any] = {
    "credentials_enable_service": False,
    "credentials_enable_autosignin": False,
    "profile.password_manager_enabled": False,
    "autofill.profile_enabled": False,
    "autofill.credit_card_enabled": False,
    "session.restore_on_startup": 1,
}

_LAST_ERROR = ""


class ChromeProfileError(RuntimeError):
    """A failure with a sentence the owner can read."""


def _set_error(text: str) -> None:
    global _LAST_ERROR
    _LAST_ERROR = text


# ---- where things are ---------------------------------------------------------------------------------------


def profile_dir() -> Path:
    raw = os.environ.get(PROFILE_ENV, "").strip()
    return Path(raw).expanduser() if raw else Path.home() / "Library" / "Application Support" / "Dourmouse" / "Chrome Profile"


def ensure_profile_dir() -> Path:
    """The folder, created private. A folder that already exists is tightened to 0700, never loosened."""
    path = profile_dir()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def chrome_binary() -> Path | None:
    raw = os.environ.get(BINARY_ENV, "").strip() or DEFAULT_BINARY
    path = Path(raw).expanduser()
    return path if path.is_file() and os.access(path, os.X_OK) else None


_VERSIONS: dict[str, str] = {}


def chrome_version() -> str:
    binary = chrome_binary()
    if binary is None:
        return ""
    if str(binary) in _VERSIONS:
        return _VERSIONS[str(binary)]
    try:
        out = subprocess.run([str(binary), "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    found = re.search(r"\d+(?:\.\d+){1,3}", out)
    version = found.group(0) if found else ""
    if version:
        _VERSIONS[str(binary)] = version
    return version


# ---- preferences -------------------------------------------------------------------------------------------


def _set_dotted(data: dict[str, Any], dotted: str, value: Any) -> None:
    node = data
    *parents, leaf = dotted.split(".")
    for key in parents:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    node[leaf] = value


def _get_dotted(data: Any, dotted: str, default: Any = None) -> Any:
    node = data
    for key in dotted.split("."):
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def preferences_path(folder: Path | None = None) -> Path:
    return (folder or profile_dir()) / "Default" / "Preferences"


def write_preferences(folder: Path | None = None) -> Path:
    """Merge ``WANTED_PREFERENCES`` into the profile's ``Preferences``, keeping every other setting. Chrome rewrites
    this file while it runs, so it is written only while the profile is not in use."""
    path = preferences_path(folder)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = _read_json(path)
    for key, value in WANTED_PREFERENCES.items():
        _set_dotted(data, key, value)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return path


def password_saving() -> str:
    """``off``, ``on`` or ``unknown``, read from the profile's preferences."""
    data = _read_json(preferences_path())
    values = [_get_dotted(data, "credentials_enable_service"), _get_dotted(data, "profile.password_manager_enabled")]
    known = [v for v in values if isinstance(v, bool)]
    if not known:
        return "unknown"
    return "on" if any(known) else "off"


def google_account() -> str:
    """The Google account the profile shows, or ``""``."""
    data = _read_json(preferences_path())
    info = data.get("account_info")
    if isinstance(info, list):
        for item in info:
            email = item.get("email") if isinstance(item, dict) else None
            if isinstance(email, str) and "@" in email:
                return email
    return ""


def password_sync() -> str:
    """Whether Chrome sync includes Passwords: ``on``, ``off``, ``unknown`` or ``not signed in``.

    Chrome keeps the sync choices in ``Preferences`` under ``sync``; the key names differ between versions, so
    anything that is not clearly there reads ``unknown`` rather than a guess."""
    data = _read_json(preferences_path())
    if not google_account():
        return "not signed in"
    everything = _get_dotted(data, "sync.keep_everything_synced")
    if everything is True:
        return "on"
    passwords = _get_dotted(data, "sync.passwords")
    if isinstance(passwords, bool):
        return "on" if passwords else "off"
    return "unknown"


# ---- the process and its port ------------------------------------------------------------------------------


def parse_port_file(text: str) -> tuple[int, str] | None:
    """``(port, browser websocket path)`` from the text of ``DevToolsActivePort``, or None."""
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    if not lines or not lines[0].isdigit():
        return None
    port = int(lines[0])
    if not 1 <= port <= 65535:
        return None
    return port, (lines[1] if len(lines) > 1 else "")


def read_port_file(folder: Path | None = None) -> tuple[int, str] | None:
    try:
        return parse_port_file(((folder or profile_dir()) / "DevToolsActivePort").read_text(encoding="utf-8"))
    except OSError:
        return None


def _pid_alive(pid: int) -> bool:
    # A Chrome this process started stays a zombie (and still answers signal 0) until it is reaped.
    with contextlib.suppress(ChildProcessError, OSError):
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def lock_pid(folder: Path | None = None) -> int | None:
    """The pid of the Chrome that holds the profile, from its ``SingletonLock`` link (``host-pid``), when alive."""
    lock = (folder or profile_dir()) / "SingletonLock"
    try:
        target = os.readlink(lock)
    except OSError:
        return None
    found = re.search(r"-(\d+)$", target)
    if not found:
        return None
    pid = int(found.group(1))
    return pid if _pid_alive(pid) else None


def _lsof(args: list[str]) -> str:
    try:
        return subprocess.run(["lsof", "-nP", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _parse_lsof_pids(text: str) -> dict[int, str]:
    out: dict[int, str] = {}
    pid = 0
    for line in text.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
            out.setdefault(pid, "")
        elif line.startswith("c") and pid:
            out[pid] = line[1:]
    return out


def port_owner(port: int) -> int | None:
    """The pid that listens on the loopback ``port``, or None."""
    found = _parse_lsof_pids(_lsof([f"-iTCP@127.0.0.1:{int(port)}", "-sTCP:LISTEN", "-Fpc"]))
    return next(iter(found), None)


def connected_processes(port: int) -> list[dict[str, Any]]:
    """Every process other than Chrome itself that holds a connection to the control port."""
    owner = port_owner(port)
    found = _parse_lsof_pids(_lsof([f"-iTCP@127.0.0.1:{int(port)}", "-sTCP:ESTABLISHED", "-Fpc"]))
    return [{"pid": pid, "name": name} for pid, name in sorted(found.items()) if pid != owner]


def live_port(folder: Path | None = None) -> tuple[int, int] | None:
    """``(port, chrome pid)`` when the port file names a port that the Chrome holding this profile really owns.
    A stale file (Chrome gone, or the port taken by another program) is never trusted."""
    entry = read_port_file(folder)
    pid = lock_pid(folder)
    if entry is None or pid is None:
        return None
    owner = port_owner(entry[0])
    if owner is None or owner != pid:
        # Chrome's helper processes never listen on it; the browser process is the lock holder.
        return None
    return entry[0], pid


# ---- launch, attach, quit ----------------------------------------------------------------------------------


def launch(*, remote_control: bool = True, headless: bool = False, url: str = "", wait: float = 20.0) -> dict[str, Any]:
    """Start the Dourmouse Chrome as its own process and wait for its control port.

    ``remote_control=False`` starts it without any control port, for the owner to sign in with nothing attached.
    Returns ``{"pid", "port"}`` (port 0 when there is none). A Chrome already running on the profile is reused."""
    binary = chrome_binary()
    if binary is None:
        raise ChromeProfileError("Google Chrome is not installed in Applications, so the Dourmouse Chrome cannot start.")
    folder = ensure_profile_dir()
    running = lock_pid(folder)
    if running is not None:
        live = live_port(folder)
        if live is not None and remote_control:
            return {"pid": live[1], "port": live[0], "reused": True}
        if not remote_control and live is None:
            return {"pid": running, "port": 0, "reused": True}
        raise ChromeProfileError(
            "The Dourmouse Chrome is already open in a different mode. Quit it first, then open it again.")
    write_preferences(folder)
    with contextlib.suppress(OSError):
        (folder / "DevToolsActivePort").unlink()
    args = [str(binary), f"--user-data-dir={folder}", "--no-first-run", "--no-default-browser-check",
            "--password-store=basic", "--disable-features=PasswordManagerOnboarding"]
    if remote_control:
        args += ["--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1"]
    if headless:
        args.append("--headless=new")
    args.append(url or "about:blank")
    try:
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
    except OSError as exc:
        _set_error(f"Chrome did not start: {exc}")
        raise ChromeProfileError(_LAST_ERROR) from exc
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            _set_error("Chrome closed straight after starting (another Chrome may already hold this profile).")
            raise ChromeProfileError(_LAST_ERROR)
        if not remote_control:
            if lock_pid(folder) is not None:
                return {"pid": proc.pid, "port": 0}
        else:
            live = live_port(folder)
            if live is not None:
                _set_error("")
                return {"pid": live[1], "port": live[0]}
        time.sleep(0.2)
    _set_error("Chrome started but its control port did not appear in time.")
    raise ChromeProfileError(_LAST_ERROR)


async def attach(playwright: Any) -> Any:
    """A Playwright ``Browser`` connected to the running Dourmouse Chrome. ``playwright`` is a started
    ``async_playwright()`` object, the same one the pane uses."""
    live = live_port()
    if live is None:
        raise ChromeProfileError("The Dourmouse Chrome is not running with a control port. Open it first.")
    port, _pid = live
    try:
        return await playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=15_000)
    except Exception as exc:  # noqa: BLE001 - a connection failure, said in words
        _set_error(f"Could not connect to the Dourmouse Chrome: {type(exc).__name__}")
        raise ChromeProfileError(_LAST_ERROR) from exc


def quit_chrome(*, wait: float = 15.0) -> bool:
    """Close the Dourmouse Chrome cleanly (it saves its tabs). True when it is gone."""
    pid = lock_pid()
    if pid is None:
        return True
    if pid == os.getpid():
        raise ChromeProfileError("The profile lock names Dourmouse itself, not a Chrome, so nothing was closed.")
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.2)
    _set_error("Chrome did not close in time.")
    return False


def window_is_visible() -> bool:
    """True when the running Chrome was started with a window (not ``--headless``), so the owner may have it open."""
    pid = lock_pid()
    if pid is None:
        return False
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
                             check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    return "--headless" not in out


# ---- status ------------------------------------------------------------------------------------------------


def status(attached: bool = False) -> dict[str, Any]:
    """Everything the status chip and ``/api/browser/status`` say about the Dourmouse Chrome. Never starts it."""
    binary = chrome_binary()
    pid = lock_pid()
    live = live_port() if pid is not None else None
    port = live[0] if live else 0
    return {
        "installed": binary is not None,
        "version": chrome_version() if binary is not None else "",
        "profile_dir": str(profile_dir()),
        "running": pid is not None,
        "pid": pid or 0,
        "control_port": port,
        "attached": bool(attached and live),
        "connected_processes": connected_processes(port) if port else [],
        "google_account": google_account(),
        "password_saving": password_saving(),
        "password_sync": password_sync(),
        "last_error": _LAST_ERROR,
    }
