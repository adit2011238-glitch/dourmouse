"""Lockdown (MS-13, finding #103). Owner spec, 2026-09-24: "a list of urls
and apps and websites that can't be opened when initiated."

The owner keeps a blocklist; starting a lockdown makes everything on it
unopenable until the lockdown ends.

Apps (no admin rights needed): an enforcer checks running processes twice a
second and closes any blocklisted app the moment it appears (relaunches
included), then tells the user why.

Websites (system-wide, every browser and app): /etc/hosts entries that point
each blocked name at 0.0.0.0 and ::, followed by a DNS cache flush. Writing
/etc/hosts needs root, so a tiny helper runs as root under launchd and does
exactly one thing: rewrite a clearly marked block of /etc/hosts from a list
of names this module writes to a file the user owns. The helper validates
every name, so the most a compromised Dourmouse could ever do through it is
block websites; there is no path to broader root access. The owner installs
the helper once, with one sudo command (Dourmouse never handles a password).

Honest limits, surfaced in status: hosts blocks whole domains, not URL
paths (blocking one page needs a browser extension); a browser with its own
DNS-over-HTTPS or an app using a hard-coded IP can bypass hosts; an already
open connection can outlive the block until the page reloads. Whether a site
is really blocked is checked by resolving it, never assumed.
"""

from __future__ import annotations

import contextlib
import json
import os
import plistlib
import re
import signal
import socket
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from dourmouse.config import user_config_dir

# The hosts format and the domain rule come from the root helper itself, so
# the Mac side and the helper can never disagree about what gets written.
from .lockdown_helper import DOMAIN_RE as _DOMAIN_RE
from .lockdown_helper import INSTALLED as HELPER_PATH
from .lockdown_helper import PLIST as HELPER_PLIST


def config_path() -> Path:
    return user_config_dir() / "lockdown.json"


def hosts_request_path() -> Path:
    """The only file the root helper reads. Owned by the user; its content is
    validated by the helper, so it cannot be used for anything but blocking."""
    return user_config_dir() / "lockdown-hosts.json"


# --------------------------------------------------------------------------- #
# the blocklist
# --------------------------------------------------------------------------- #

def normalize_site(entry: str) -> dict[str, str]:
    """A typed URL or domain to the host that can be blocked. Paths are kept
    for display but cannot be blocked by hosts; `path_ignored` says so."""
    raw = entry.strip()
    url = raw if "://" in raw else f"https://{raw}"
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if not _DOMAIN_RE.match(host):
        raise ValueError(f"not a website address: {entry!r}")
    path = parts.path if parts.path not in ("", "/") else ""
    return {"entry": raw, "domain": host, "path_ignored": path}


def hosts_names(domain: str) -> list[str]:
    """Block the bare domain and its www twin (the two a person types)."""
    return [domain, f"www.{domain}"]


@dataclass
class Blocklist:
    apps: list[dict[str, str]] = field(default_factory=list)  # {"name", "bundle_id"?, "path"?}
    sites: list[dict[str, str]] = field(default_factory=list)  # normalize_site() rows
    active: bool = False
    started_at: float | None = None

    @classmethod
    def load(cls, path: Path | None = None) -> Blocklist:
        p = path or config_path()
        if not p.exists():
            return cls()
        d = json.loads(p.read_text(encoding="utf-8"))
        return cls(apps=d.get("apps", []), sites=d.get("sites", []), active=bool(d.get("active")),
                   started_at=d.get("started_at"))

    def save(self, path: Path | None = None) -> None:
        p = path or config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    def add_site(self, entry: str) -> dict[str, str]:
        row = normalize_site(entry)
        if all(s["domain"] != row["domain"] for s in self.sites):
            self.sites.append(row)
        return row

    def add_app(self, entry: str) -> dict[str, str]:
        row = resolve_app(entry)
        if all(a.get("bundle_id") != row.get("bundle_id") or a["name"] != row["name"] for a in self.apps):
            self.apps.append(row)
        return row

    def remove(self, entry: str) -> bool:
        low = entry.strip().lower()
        before = len(self.apps) + len(self.sites)
        self.apps = [a for a in self.apps if low not in (a["name"].lower(), (a.get("bundle_id") or "").lower())]
        try:
            dom = normalize_site(entry)["domain"]
        except ValueError:
            dom = low
        self.sites = [s for s in self.sites if s["domain"] != dom]
        return len(self.apps) + len(self.sites) < before


def resolve_app(entry: str) -> dict[str, str]:
    """An app by name ("Discord"), bundle id ("com.hnc.Discord") or path.
    Resolved to its bundle when installed, so a renamed copy is still caught."""
    raw = entry.strip()
    candidates = []
    if raw.endswith(".app") or "/" in raw:
        candidates.append(Path(raw).expanduser())
    name = raw.removesuffix(".app").split("/")[-1]
    candidates += [Path("/Applications") / f"{name}.app", Path.home() / "Applications" / f"{name}.app",
                   Path("/System/Applications") / f"{name}.app"]
    for c in candidates:
        info = c / "Contents" / "Info.plist"
        if info.exists():
            with contextlib.suppress(OSError, plistlib.InvalidFileException, ValueError):
                pl = plistlib.loads(info.read_bytes())
                return {"name": pl.get("CFBundleName") or name, "bundle_id": pl.get("CFBundleIdentifier", ""),
                        "path": str(c)}
    if re.fullmatch(r"[A-Za-z0-9-]+(\.[A-Za-z0-9-]+){2,}", raw):
        return {"name": raw.split(".")[-1], "bundle_id": raw, "path": ""}
    return {"name": name, "bundle_id": "", "path": ""}


# --------------------------------------------------------------------------- #
# apps: the enforcer
# --------------------------------------------------------------------------- #

def process_matches(app: dict[str, str], exe: str | None, name: str) -> bool:
    if app.get("path") and exe and exe.startswith(app["path"].rstrip("/") + "/"):
        return True
    if exe and ".app/" in exe:
        bundle = exe.split(".app/")[0] + ".app"
        if Path(bundle).name.removesuffix(".app").lower() == app["name"].lower():
            return True
        if app.get("bundle_id"):
            info = Path(bundle) / "Contents" / "Info.plist"
            with contextlib.suppress(OSError, plistlib.InvalidFileException, ValueError):
                if plistlib.loads(info.read_bytes()).get("CFBundleIdentifier") == app["bundle_id"]:
                    return True
    return name.lower() == app["name"].lower()


class AppEnforcer:
    def __init__(self, blocklist_loader: Any = None, interval: float = 0.5, notify: Any = None) -> None:
        self._load = blocklist_loader or Blocklist.load
        self.interval = interval
        self.notify = notify if notify is not None else notify_user
        self.killed: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def sweep(self) -> list[dict[str, Any]]:
        """One pass: close every running blocklisted app. Returns what it closed."""
        import psutil

        bl = self._load()
        if not bl.active or not bl.apps:
            return []
        closed = []
        me = os.getpid()
        for p in psutil.process_iter(["pid", "name", "exe"]):
            if p.info["pid"] == me:
                continue
            for app in bl.apps:
                if process_matches(app, p.info.get("exe"), p.info.get("name") or ""):
                    with contextlib.suppress(psutil.Error):
                        p.send_signal(signal.SIGTERM)
                        try:
                            p.wait(2)
                        except psutil.TimeoutExpired:
                            p.kill()
                        row = {"app": app["name"], "pid": p.info["pid"], "at": time.time()}
                        closed.append(row)
                        self.killed.append(row)
                        self.notify(f"{app['name']} is blocked while lockdown is on.")
                    break
        return closed

    def start(self) -> None:
        if self._thread is not None:
            return

        def loop() -> None:
            while not self._stop.wait(self.interval):
                with contextlib.suppress(Exception):  # one bad pass must never stop enforcement
                    self.sweep()

        self._thread = threading.Thread(target=loop, daemon=True, name="dourmouse-lockdown-apps")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


def notify_user(message: str) -> None:
    import subprocess

    safe = message.replace("\\", "").replace('"', "'")
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(  # noqa: S603, S607 -- fixed osascript call, message escaped
            ["osascript", "-e", f'display notification "{safe}" with title "Dourmouse lockdown"'],
            capture_output=True, timeout=5, check=False,
        )


# --------------------------------------------------------------------------- #
# websites: the hosts block, written by the root helper
# --------------------------------------------------------------------------- #

def write_hosts_request(bl: Blocklist, path: Path | None = None) -> Path:
    p = path or hosts_request_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    domains = [s["domain"] for s in bl.sites] if bl.active else []
    p.write_text(json.dumps({"domains": domains, "written_at": time.time()}), encoding="utf-8")
    return p


def site_is_blocked(domain: str) -> bool:
    """Resolve it the way apps do: blocked means it now resolves to 0.0.0.0/::."""
    try:
        addrs = {i[4][0] for i in socket.getaddrinfo(domain, 443)}
    except socket.gaierror:
        return True
    return bool(addrs) and addrs <= {"0.0.0.0", "::"}  # noqa: S104 -- the block address, compared not bound


def helper_installed() -> bool:
    return Path(HELPER_PLIST).exists() and Path(HELPER_PATH).exists()


def install_command(request_file: Path | None = None) -> str:
    """The one command the owner runs (it asks for their password itself)."""
    from importlib import resources

    src = resources.files("dourmouse.security").joinpath("lockdown_helper.py")
    req = request_file or hosts_request_path()
    return (
        f"sudo /usr/bin/python3 '{src}' --install --request-file '{req}'"
    )


# --------------------------------------------------------------------------- #
# lockdown on / off
# --------------------------------------------------------------------------- #

def start(bl: Blocklist | None = None) -> dict[str, Any]:
    bl = bl or Blocklist.load()
    bl.active, bl.started_at = True, time.time()
    bl.save()
    write_hosts_request(bl)
    return status(bl)


def stop(bl: Blocklist | None = None) -> dict[str, Any]:
    bl = bl or Blocklist.load()
    bl.active, bl.started_at = False, None
    bl.save()
    write_hosts_request(bl)
    return status(bl)


def status(bl: Blocklist | None = None, *, check_sites: bool = True) -> dict[str, Any]:
    bl = bl or Blocklist.load()
    helper = helper_installed()
    sites = []
    for s in bl.sites:
        row: dict[str, Any] = dict(s)
        if bl.active and check_sites:
            row["blocked_now"] = site_is_blocked(s["domain"])
        sites.append(row)
    limits = ["Websites are blocked by domain; a URL's path cannot be blocked this way."]
    if bl.active and bl.sites and not helper:
        limits.insert(0, "Websites are NOT blocked yet: the lockdown helper is not installed. Run once: "
                         + install_command())
    limits.append("A browser using its own DNS-over-HTTPS, or an app using a fixed IP address, can bypass a hosts block.")
    return {"active": bl.active, "started_at": bl.started_at, "apps": bl.apps, "sites": sites,
            "helper_installed": helper, "limits": limits}


def lockdown_enforcer_enabled() -> bool:
    return os.environ.get("DOURMOUSE_LOCKDOWN_ENFORCER", "1").strip() != "0"
