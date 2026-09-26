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

Honest limits, surfaced in status: hosts blocks exactly the names it lists
(the domain and its www form), not other subdomains such as m. or old., and
not URL paths (those are blocked by the browser extension in extension/lockdown,
fed by extension_rules, and only in browsers that have it installed); a browser with
its own DNS-over-HTTPS or an app using a hard-coded IP can bypass hosts; an
already open connection can outlive the block until the page reloads.
Whether a site is really blocked is checked by resolving it, never assumed.

The enforcer never closes anything macOS or Dourmouse needs to run (the
Finder, Dock, terminals, Dourmouse's own processes and the Python interpreter
running the server), and matches an app by bundle id or exact executable
name, never a fragment. A damaged blocklist file is set aside and read as an
empty, inactive lockdown, so ending a lockdown always works.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import logging
import os
import plistlib
import re
import signal
import socket
import sys
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from dourmouse.config import user_config_dir

# The hosts format and the domain rule come from the root helper itself, so
# the Mac side and the helper can never disagree about what gets written.
from .lockdown_helper import INSTALLED as HELPER_PATH
from .lockdown_helper import PLIST as HELPER_PLIST
from .lockdown_helper import is_reserved as _is_reserved
from .lockdown_helper import is_valid_name as _is_valid_name
from .privacy import atomic_write_text
from .response import PROTECTED_NAMES

_log = logging.getLogger(__name__)
#: One lock for every load, change, save of the blocklist, so two threads
#: (chat tool, console, enforcer) cannot overwrite each other's edit.
_LOCK = threading.RLock()


def config_path() -> Path:
    return user_config_dir() / "lockdown.json"


def hosts_request_path() -> Path:
    """The only file the root helper reads. Owned by the user; its content is
    validated by the helper, so it cannot be used for anything but blocking."""
    return user_config_dir() / "lockdown-hosts.json"


# --------------------------------------------------------------------------- #
# the blocklist
# --------------------------------------------------------------------------- #

def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def normalize_site(entry: str) -> dict[str, str]:
    """A typed URL or domain to the host that can be blocked. Paths are kept
    for display but cannot be blocked by hosts; `path_ignored` says so."""
    raw = entry.strip()
    url = raw if "://" in raw else f"https://{raw}"
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if _is_ip(host):
        raise ValueError(f"{entry!r} is an IP address; the hosts file blocks names, not addresses")
    if not _is_valid_name(host):
        raise ValueError(f"not a website address: {entry!r}")
    if _is_reserved(host):
        raise ValueError(f"{host} is needed by macOS (updates, certificate checks or iCloud) and is never blocked")
    path = parts.path if parts.path not in ("", "/") else ""
    return {"entry": raw, "domain": host, "path_ignored": path}


MAX_URL_PATH = 200
MAX_URLS = 200
#: Characters that mean something in a browser URL filter. A typed path may
#: not carry them, so an entry can never widen itself into a wildcard.
_FILTER_SPECIALS = frozenset("*^|")


def normalize_url(entry: str) -> dict[str, str]:
    """A typed URL to a host plus path prefix that a browser extension can
    block ("reddit.com/r/all"). The query and fragment are dropped and www. is
    folded away (the filter also covers other subdomains of the host). A URL
    with no path is a whole site: add it as a website instead."""
    raw = entry.strip()
    if not raw or any(c.isspace() for c in raw):
        raise ValueError(f"not a URL: {entry!r}")
    url = raw if "://" in raw else f"https://{raw}"
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"not a URL: {entry!r} ({exc})") from exc
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"only http and https URLs can be blocked: {entry!r}")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ValueError(f"a URL with a username or password is refused: {entry!r}")
    if port is not None:
        raise ValueError(f"a URL with a port number is not supported: {entry!r}")
    host = (parts.hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if _is_ip(host):
        raise ValueError(f"{entry!r} is an IP address; URL blocking works on website names")
    if not _is_valid_name(host):
        raise ValueError(f"not a website address: {entry!r}")
    if _is_reserved(host):
        raise ValueError(f"{host} is needed by macOS (updates, certificate checks or iCloud) and is never blocked")
    path = parts.path
    if path in ("", "/"):
        raise ValueError(f"{entry!r} has no path; add it as a website to block the whole site")
    if len(path) > MAX_URL_PATH:
        raise ValueError(f"the URL path is longer than {MAX_URL_PATH} characters")
    if _FILTER_SPECIALS & set(path):
        raise ValueError("the characters * ^ | are not allowed in a URL path")
    return {"entry": raw, "host": host, "path": path, "url": host + path}


def hosts_names(domain: str) -> list[str]:
    """Block the bare domain and its www twin (the two a person types). No
    other subdomain is blocked: /etc/hosts has no wildcards."""
    return [domain, f"www.{domain}"]


@dataclass
class Blocklist:
    apps: list[dict[str, str]] = field(default_factory=list)  # {"name", "bundle_id"?, "path"?}
    sites: list[dict[str, str]] = field(default_factory=list)  # normalize_site() rows
    urls: list[dict[str, str]] = field(default_factory=list)  # normalize_url() rows, blocked by the browser extension
    active: bool = False
    started_at: float | None = None
    # Security blocks (finding #106): domains blocked for good, whether or
    # not a lockdown is on, e.g. a malware or phishing domain.
    always: list[dict[str, str]] = field(default_factory=list)
    # Set when the file on disk was unreadable and this is the empty stand-in.
    # Never saved.
    load_warning: str = field(default="", repr=False)

    @classmethod
    def load(cls, path: Path | None = None) -> Blocklist:
        """Read the blocklist. A missing file is an empty, inactive lockdown.
        A damaged one (truncated write, wrong type) is set aside as
        <name>.corrupt and also read as empty and inactive, with `load_warning`
        saying so: lockdown must always be endable, never wedged by a bad file."""
        p = path or config_path()
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(d, dict):
                raise ValueError("the file is not a JSON object")
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError) as exc:
            bad = p.with_name(p.name + ".corrupt")
            with contextlib.suppress(OSError):  # if it cannot be moved, the warning repeats: still honest
                os.replace(p, bad)
            return cls(load_warning=f"The lockdown state file was unreadable ({exc}); it was set aside as "
                                    f"{bad.name} and lockdown is off. Add the entries you need again.")
        started = d.get("started_at")
        return cls(apps=_rows(d.get("apps"), "name"), sites=_rows(d.get("sites"), "domain"),
                   urls=_valid_url_rows(d.get("urls")), active=bool(d.get("active")),
                   started_at=started if isinstance(started, (int, float)) and not isinstance(started, bool) else None,
                   always=_rows(d.get("always"), "domain"))

    def save(self, path: Path | None = None) -> None:
        data = asdict(self)
        del data["load_warning"]
        atomic_write_text(path or config_path(), json.dumps(data, indent=2))

    def add_site(self, entry: str) -> dict[str, str]:
        row = normalize_site(entry)
        if all(s["domain"] != row["domain"] for s in self.sites):
            self.sites.append(row)
        return row

    def add_url(self, entry: str) -> dict[str, str]:
        row = normalize_url(entry)
        if all(u["url"] != row["url"] for u in self.urls):
            if len(self.urls) >= MAX_URLS:
                raise ValueError(f"the blocklist already holds {MAX_URLS} URLs")
            self.urls.append(row)
        return row

    def add_app(self, entry: str) -> dict[str, str]:
        row = resolve_app(entry)
        reason = protected_app_reason(row)
        if reason:
            raise ValueError(reason)
        if all(a.get("bundle_id") != row.get("bundle_id") or a["name"] != row["name"] for a in self.apps):
            self.apps.append(row)
        return row

    def remove(self, entry: str) -> bool:
        low = entry.strip().lower()
        before = len(self.apps) + len(self.sites) + len(self.urls)
        self.apps = [a for a in self.apps if low not in (a["name"].lower(), (a.get("bundle_id") or "").lower())]
        try:
            url = normalize_url(entry)["url"]
        except ValueError:
            url = ""
        if url:
            # A URL with a path names only that URL: removing it must leave the
            # whole-site block for the same host alone.
            self.urls = [u for u in self.urls if u["url"] != url]
        else:
            try:
                dom = normalize_site(entry)["domain"]
            except ValueError:
                dom = low
            self.sites = [s for s in self.sites if s["domain"] != dom]
        return len(self.apps) + len(self.sites) + len(self.urls) < before


def _rows(value: Any, key: str) -> list[dict[str, str]]:
    """The dict rows of a stored list that carry a text `key`; anything else
    in a hand-edited or damaged file is dropped."""
    if not isinstance(value, list):
        return []
    return [r for r in value if isinstance(r, dict) and isinstance(r.get(key), str)]


def _valid_url_rows(value: Any) -> list[dict[str, str]]:
    """Stored URL rows that still pass normalize_url, rebuilt from their own
    entry, so a hand-edited file can never put an unchecked filter into the
    extension's rules."""
    out: list[dict[str, str]] = []
    for r in _rows(value, "entry"):
        try:
            row = normalize_url(r["entry"])
        except ValueError:
            continue
        if all(u["url"] != row["url"] for u in out) and len(out) < MAX_URLS:
            out.append(row)
    return out


@contextlib.contextmanager
def locked_blocklist() -> Any:
    """Load the blocklist for one change under the module lock. The caller
    saves inside the `with`, so a concurrent start, stop or edit is never
    overwritten with a stale copy."""
    with _LOCK:
        yield Blocklist.load()


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

#: Apps and processes the enforcer never closes, whatever the blocklist says:
#: killing them logs the owner out, hangs the display, removes the way to run
#: the uninstall command, or takes Dourmouse itself down.
PROTECTED_APP_NAMES = frozenset(n.lower() for n in PROTECTED_NAMES) | frozenset({
    "terminal", "iterm", "iterm2", "dourmouse", "electron", "python", "python3",
})
PROTECTED_BUNDLE_IDS = frozenset({
    "com.apple.finder", "com.apple.dock", "com.apple.loginwindow", "com.apple.systemuiserver",
    "com.apple.terminal", "com.googlecode.iterm2", "org.python.python", "com.github.electron",
})
#: Executables under these belong to macOS itself. /System/Applications is
#: deliberately not here: Safari, Music and the other Apple apps are ordinary
#: apps an owner may block.
PROTECTED_EXE_PREFIXES = ("/usr/", "/bin/", "/sbin/", "/Library/Apple/", "/System/Library/", "/private/var/db/")
_PYTHON_NAME_RE = re.compile(r"python[0-9.]*(-config)?")


def _bundle_of(exe: str | None) -> str | None:
    return exe.split(".app/")[0] + ".app" if exe and ".app/" in exe else None


def _bundle_id_of(bundle: str) -> str | None:
    info = Path(bundle) / "Contents" / "Info.plist"
    with contextlib.suppress(OSError, plistlib.InvalidFileException, ValueError):
        return plistlib.loads(info.read_bytes()).get("CFBundleIdentifier")
    return None


def _protected_identity(name: str, bundle_id: str | None, exe: str | None) -> str | None:
    low = name.lower()
    if low in PROTECTED_APP_NAMES or _PYTHON_NAME_RE.fullmatch(low):
        return f"{name} is part of macOS or Dourmouse's own environment"
    bid = (bundle_id or "").lower()
    if bid in PROTECTED_BUNDLE_IDS or bid.startswith("com.dourmouse"):
        return f"{name} ({bundle_id}) is part of macOS or Dourmouse's own environment"
    if exe and not exe.startswith("/usr/local/") and exe.startswith(PROTECTED_EXE_PREFIXES):
        return f"{name} is a macOS system program"
    return None


def protected_app_reason(app: dict[str, str]) -> str | None:
    """Why this blocklist entry can never be enforced, or None."""
    reason = _protected_identity(app.get("name", ""), app.get("bundle_id"), app.get("path"))
    return f"{reason} and cannot be blocked" if reason else None


def _interpreter_paths() -> tuple[str, str | None]:
    real = os.path.realpath(sys.executable)
    return real, _bundle_of(real)


def _is_dourmouse_interpreter(exe: str | None) -> bool:
    """True for the Python interpreter running this server, or any process
    from the same bundle (Dourmouse's helper modules run under it)."""
    if not exe:
        return False
    real, bundle = _interpreter_paths()
    real_exe = os.path.realpath(exe)
    return real_exe == real or (bundle is not None and _bundle_of(real_exe) == bundle)


def process_matches(app: dict[str, str], exe: str | None, name: str) -> bool:
    """A process belongs to the app by its bundle path, its bundle id, or its
    exact executable name. Never by a fragment of a name."""
    if app.get("path") and exe and exe.startswith(app["path"].rstrip("/") + "/"):
        return True
    bundle = _bundle_of(exe)
    if bundle:
        if app.get("bundle_id"):
            return _bundle_id_of(bundle) == app["bundle_id"]
        return Path(bundle).name.removesuffix(".app").lower() == app["name"].lower()
    return name.lower() == app["name"].lower()


def _never_close(pid: int, name: str, exe: str | None, own_tree: set[int]) -> bool:
    if pid in own_tree or _is_dourmouse_interpreter(exe):
        return True
    bundle = _bundle_of(exe)
    return _protected_identity(name, _bundle_id_of(bundle) if bundle else None, exe) is not None


class AppEnforcer:
    def __init__(self, blocklist_loader: Any = None, interval: float = 0.5, notify: Any = None) -> None:
        self._load = blocklist_loader or Blocklist.load
        self.interval = interval
        self.notify = notify if notify is not None else notify_user
        self.killed: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._warned = ""

    def sweep(self) -> list[dict[str, Any]]:
        """One pass: close every running blocklisted app. Returns what it closed."""
        import psutil

        bl = self._load()
        if bl.load_warning and bl.load_warning != self._warned:
            self._warned = bl.load_warning
            # The state file was damaged: enforcement is off, so say so and
            # clear any hosts block that the lost state can no longer end.
            write_hosts_request(bl)
            self.notify("Lockdown state was unreadable, so lockdown is off. Open the Security console to set it up again.")
        if not bl.active or not bl.apps:
            return []
        closed = []
        me = psutil.Process()
        own_tree = {me.pid, *(a.pid for a in me.parents())}
        for p in psutil.process_iter(["pid", "name", "exe"]):
            name = p.info.get("name") or ""
            exe = p.info.get("exe")
            for app in bl.apps:
                if process_matches(app, exe, name):
                    if _never_close(p.info["pid"], name, exe, own_tree):
                        break
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
                try:
                    self.sweep()
                except Exception:  # noqa: BLE001 -- one bad pass must never stop enforcement, but is never silent
                    _log.exception("lockdown app sweep failed; enforcement continues")

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
    names = {s["domain"] for s in (bl.sites if bl.active else [])} | {s["domain"] for s in bl.always}
    domains = sorted(d for d in names if not _is_reserved(d))
    atomic_write_text(p, json.dumps({"domains": domains, "written_at": time.time()}))
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
    with _LOCK:
        bl = bl or Blocklist.load()
        bl.active, bl.started_at = True, time.time()
        bl.save()
        write_hosts_request(bl)
        return status(bl)


def stop(bl: Blocklist | None = None) -> dict[str, Any]:
    """End lockdown. Works even when the state file was damaged: it then
    loads as empty (with a warning in the result) and the hosts request is
    rewritten without the lockdown sites."""
    with _LOCK:
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
        row["blocks"] = hosts_names(s["domain"])
        if bl.active and check_sites:
            row["blocked_now"] = all(site_is_blocked(n) for n in row["blocks"])
        sites.append(row)
    limits = ["A website block covers exactly the listed name and its www. form; other subdomains such as m. "
              "or old. are not blocked, and a URL's path cannot be blocked this way."]
    if bl.urls:
        limits.append("URL paths are blocked only in browsers that have the Dourmouse lockdown extension installed "
                      "(and, for private windows, allowed there); other browsers are not covered.")
    if ((bl.active and bl.sites) or bl.always) and not helper:
        limits.insert(0, "Websites are NOT blocked yet: the lockdown helper is not installed. Run once: "
                         + install_command())
    limits.append("A browser using its own DNS-over-HTTPS, or an app using a fixed IP address, can bypass a hosts block.")
    warnings = [bl.load_warning] if bl.load_warning else []
    warnings += [f"{a['name']} will never be closed: {protected_app_reason(a)}" for a in bl.apps if protected_app_reason(a)]
    return {"active": bl.active, "started_at": bl.started_at, "apps": bl.apps, "sites": sites,
            "urls": bl.urls, "always_blocked": bl.always, "helper_installed": helper, "limits": limits, "warnings": warnings}


def block_domain_always(entry: str, reason: str = "", bl: Blocklist | None = None) -> dict[str, Any]:
    """Block a domain for good (a security response, not a lockdown): it
    stays blocked when lockdown is off, until unblock_domain_always."""
    with _LOCK:
        bl = bl or Blocklist.load()
        row = {**normalize_site(entry), "reason": reason, "blocked_at": str(int(time.time()))}
        if all(s["domain"] != row["domain"] for s in bl.always):
            bl.always.append(row)
        bl.save()
        write_hosts_request(bl)
        return status(bl, check_sites=False)


def unblock_domain_always(entry: str, bl: Blocklist | None = None) -> dict[str, Any]:
    with _LOCK:
        bl = bl or Blocklist.load()
        try:
            dom = normalize_site(entry)["domain"]
        except ValueError:
            dom = entry.strip().lower()
        bl.always = [s for s in bl.always if s["domain"] != dom]
        bl.save()
        write_hosts_request(bl)
        return status(bl, check_sites=False)


def lockdown_enforcer_enabled() -> bool:
    return os.environ.get("DOURMOUSE_LOCKDOWN_ENFORCER", "1").strip() != "0"
