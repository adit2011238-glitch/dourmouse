"""Downloads watch and file assessment (MS-4, finding #101).

Every file that lands in ~/Downloads is assessed: what it really is (by its
bytes, not its name), where it came from and which app fetched it (the
quarantine flag and kMDItemWhereFroms), whether it is signed and what
Gatekeeper says, and the tricks malware relies on (a double extension like
"invoice.pdf.app", a stripped quarantine flag, an app hidden in a zip).

Honesty (spec items 25, 50): this is not an antivirus. With no malware
scanner installed, document contents are NOT scanned for exploits, and every
verdict says so with an explicit confidence. ClamAV is used when present
(clamdscan preferred, it keeps signatures resident); it is never implied.

The watcher polls the folder every 2 s (no extra dependency) and waits for a
file's size to settle, and skips in-progress browser downloads
(.crdownload, .download, .part), before assessing it.
"""

from __future__ import annotations

import contextlib
import hashlib
import plistlib
import re
import shutil
import subprocess
import threading
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import mac_telemetry as mt

EXECUTABLE_KINDS = {"macho", "app", "pkg", "dmg", "script", "jar"}
_IN_PROGRESS = (".crdownload", ".download", ".part", ".partial", ".tmp")
_SCRIPT_EXT = {".sh", ".command", ".py", ".pl", ".rb", ".scpt", ".applescript", ".tool", ".zsh", ".bash"}
_MACRO_EXT = {".docm", ".xlsm", ".pptm", ".dotm", ".xltm"}
_DECOY_EXT = {".pdf", ".doc", ".docx", ".jpg", ".jpeg", ".png", ".txt", ".xls", ".xlsx", ".mp4", ".mov", ".zip"}
_MACHO_MAGIC = {b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe"}


def default_downloads_dir() -> Path:
    return Path.home() / "Downloads"


def classify_bytes(name: str, head: bytes, tail: bytes) -> str:
    """What a file really is, from its bytes first and its name second.
    `tail` is the last 512 bytes (a DMG's "koly" trailer lives there)."""
    ext = Path(name).suffix.lower()
    if head[:4] in _MACHO_MAGIC and not (head[:4] == b"\xca\xfe\xba\xbe" and ext == ".class"):
        return "macho"
    if head[:4] == b"xar!":
        return "pkg"
    if tail[-512:].startswith(b"koly") or ext == ".dmg":
        return "dmg"
    if head[:2] == b"#!" or ext in _SCRIPT_EXT:
        return "script"
    if head[:4] == b"PK\x03\x04":
        if ext == ".jar":
            return "jar"
        if ext in _MACRO_EXT:
            return "office_macro"
        return "zip"
    if head[:5] == b"%PDF-":
        return "pdf"
    if ext in _MACRO_EXT:
        return "office_macro"
    return "document" if ext else "unknown"


def parse_quarantine(value: str | None) -> dict[str, Any] | None:
    """`com.apple.quarantine`: flags;hex-timestamp;agent;uuid. Bit 0x40 set
    means the user already approved opening it through Gatekeeper."""
    if not value:
        return None
    parts = value.strip().split(";")
    try:
        flags = int(parts[0], 16)
        ts = int(parts[1], 16) if len(parts) > 1 and parts[1] else None
    except ValueError:
        return {"raw": value, "parse_error": True}
    return {"flags": flags, "downloaded_at": ts, "agent": parts[2] if len(parts) > 2 else None,
            "user_approved": bool(flags & 0x40)}


def double_extension(name: str) -> bool:
    """"invoice.pdf.app", "photo.jpg .command": a harmless-looking extension
    placed before the real one, a classic trick."""
    p = Path(name.rstrip())
    real = p.suffix.lower()
    inner = Path(p.stem.rstrip()).suffix.lower()
    # Padding spaces before the real extension hide it in narrow Finder
    # columns ("photo.jpg      .app"); before a document extension they are
    # just an odd name.
    padded = bool(re.search(r"\s{2,}\.\w+$", name)) and real not in _DECOY_EXT
    return bool(inner in _DECOY_EXT and real and real not in _DECOY_EXT) or padded


def _xattr(path: Path, attr: str) -> str | None:
    ok, out = mt._run(["xattr", "-p", attr, str(path)])
    return out.strip() if ok else None


def where_from(path: Path) -> list[str]:
    ok, out = mt._run(["xattr", "-px", "com.apple.metadata:kMDItemWhereFroms", str(path)])
    if not ok:
        return []
    try:
        urls = plistlib.loads(bytes.fromhex("".join(out.split())))
    except (ValueError, plistlib.InvalidFileException):
        return []
    return [str(u) for u in urls] if isinstance(urls, list) else []


def zip_contents_flags(path: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError) as exc:
        return {"readable": False, "reason": str(exc)}
    apps = sorted({n.split(".app/")[0] + ".app" for n in names if ".app/" in n})
    scripts = [n for n in names if Path(n).suffix.lower() in _SCRIPT_EXT]
    return {"readable": True, "entries": len(names), "apps": apps[:10], "scripts": scripts[:10]}


def malware_scan(path: Path) -> dict[str, Any]:
    """ClamAV when it is really installed; otherwise an explicit 'not scanned'."""
    for tool in ("clamdscan", "clamscan"):
        exe = shutil.which(tool)
        if not exe:
            continue
        try:
            proc = subprocess.run(  # noqa: S603 -- resolved scanner binary, file path argument
                [exe, "--no-summary", str(path)], capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=300, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"scanned": False, "engine": tool, "reason": str(exc)}
        infected = proc.returncode == 1
        return {"scanned": proc.returncode in (0, 1), "engine": tool, "infected": infected,
                "detail": proc.stdout.strip()[-300:]}
    return {"scanned": False, "engine": None, "reason": "no malware scanner is installed (ClamAV not found)"}


@dataclass
class Assessment:
    path: str
    name: str
    size: int
    sha256: str
    kind: str
    risk: str  # "high" | "med" | "low" | "info"
    reasons: list[str] = field(default_factory=list)
    confidence: str = ""
    quarantine: dict[str, Any] | None = None
    where_from: list[str] = field(default_factory=list)
    signature: dict[str, Any] | None = None
    scan: dict[str, Any] | None = None
    archive: dict[str, Any] | None = None


def assess(path: Path) -> Assessment:
    data_head = b""
    size = path.stat().st_size if path.is_file() else 0
    digest = hashlib.sha256()
    tail = b""
    if path.is_file():
        with path.open("rb") as fh:
            data_head = fh.read(4096)
            digest.update(data_head)
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
                tail = (tail + chunk)[-512:]
            if size <= 4096:
                tail = data_head[-512:]
    kind = "app" if path.is_dir() and path.suffix == ".app" else classify_bytes(path.name, data_head, tail)
    quarantine = parse_quarantine(_xattr(path, "com.apple.quarantine"))
    a = Assessment(path=str(path), name=path.name, size=size, sha256=digest.hexdigest() if path.is_file() else "",
                   kind=kind, risk="info", quarantine=quarantine, where_from=where_from(path))
    reasons = a.reasons

    if double_extension(path.name):
        reasons.append(f"double extension: '{path.name}' pretends to be a document")
    if kind in ("macho", "app", "pkg", "script", "jar"):
        target = str(path)
        sig = mt.code_signature(target) if kind in ("macho", "app") else None
        if kind == "pkg":
            _, out = mt._run(["spctl", "--assess", "--type", "install", "-vv", target])
            sig = {"kind": "installer", "gatekeeper": mt.parse_spctl(out)}
        a.signature = sig
        verdict = ((sig or {}).get("gatekeeper") or {}).get("verdict")
        if sig and sig.get("kind") in ("unsigned", "adhoc"):
            reasons.append(f"{kind} is {sig['kind']}: no developer is accountable for it")
        if verdict == "rejected":
            reasons.append("Gatekeeper rejects it")
        if kind in ("script", "jar"):
            reasons.append(f"{kind} files run with your full permissions and carry no signature check")
        if quarantine is None:
            reasons.append("the quarantine flag is missing, so Gatekeeper will not check it on first open")
    elif kind == "dmg":
        reasons.append("disk image: its contents are assessed when mounted, not here")
    elif kind == "office_macro":
        reasons.append("Office document with macros enabled format")
    elif kind == "zip":
        a.archive = zip_contents_flags(path)
        if a.archive.get("apps") or a.archive.get("scripts"):
            reasons.append("archive contains " + ", ".join(a.archive.get("apps", []) + a.archive.get("scripts", []))[:200])

    a.scan = malware_scan(path) if path.is_file() else {"scanned": False, "reason": "bundles are not scanned"}
    if a.scan.get("infected"):
        reasons.insert(0, f"{a.scan['engine']} reports it infected: {a.scan.get('detail', '')}")

    high = a.scan.get("infected") or double_extension(path.name) or (
        kind in ("macho", "app", "pkg") and ((a.signature or {}).get("kind") in ("unsigned", "adhoc")
                                              or ((a.signature or {}).get("gatekeeper") or {}).get("verdict") == "rejected"))
    if high:
        a.risk = "high"
    elif kind in EXECUTABLE_KINDS or kind == "office_macro" or (a.archive or {}).get("apps") or (a.archive or {}).get("scripts"):
        a.risk = "med"
    elif reasons:
        a.risk = "low"
    a.confidence = (
        f"Malware scan: {a.scan['engine']}, a signature engine with well below commercial detection rates."
        if a.scan.get("scanned") else
        "No malware scanner ran, so the file's contents were not checked for known malware or document "
        "exploits. The verdict rests on its type, signature, Gatekeeper and origin only."
    )
    return a


class DownloadsWatcher:
    """Polls a folder and hands each settled new file to ``on_file``."""

    def __init__(self, folder: Path | None = None, on_file: Any = None, interval: float = 2.0) -> None:
        self.folder = folder or default_downloads_dir()
        self.on_file = on_file
        self.interval = interval
        self._seen: set[str] = set()
        self._sizes: dict[str, int] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._primed = False

    def _entries(self) -> dict[str, Path]:
        try:
            return {p.name: p for p in self.folder.iterdir()
                    if not p.name.startswith(".") and not p.name.endswith(_IN_PROGRESS)}
        except OSError:
            return {}

    def poll_once(self) -> list[Path]:
        """Returns the files that became ready this poll. The first poll only
        records what was already there: the watcher reports new arrivals."""
        entries = self._entries()
        if not self._primed:
            self._seen = set(entries)
            self._primed = True
            return []
        ready = []
        for name, p in entries.items():
            if name in self._seen:
                continue
            try:
                size = p.stat().st_size if p.is_file() else -1
            except OSError:
                continue
            if self._sizes.get(name) == size:
                self._seen.add(name)
                self._sizes.pop(name, None)
                ready.append(p)
            else:
                self._sizes[name] = size  # still growing: check again next poll
        return ready

    def start(self) -> None:
        if self._thread is not None:
            return

        def loop() -> None:
            while not self._stop.wait(self.interval):
                for p in self.poll_once():
                    if self.on_file is not None:
                        # One bad file must never stop the watcher.
                        with contextlib.suppress(Exception):
                            self.on_file(p)

        self.poll_once()
        self._thread = threading.Thread(target=loop, daemon=True, name="dourmouse-downloads-watch")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


def downloads_watch_enabled() -> bool:
    import os

    return os.environ.get("DOURMOUSE_DOWNLOADS_WATCH", "1").strip() != "0"



def handle_new_file(path: Path, store: Any, now: float, write_alerts: bool = True) -> Assessment:
    """Assess a newly arrived file, record it, and raise a sentry finding for
    anything worth a look (high: alert; med: finding only)."""
    import dataclasses

    from .sentry import SentryFinding, _fingerprint, _write_alert

    a = assess(path)
    store.record_download(dataclasses.asdict(a), now)
    if a.risk in ("high", "med"):
        finding = SentryFinding(
            fingerprint=_fingerprint("risky_download", a.sha256 or a.path), kind="risky_download",
            severity=a.risk, title=f"Downloaded {a.kind} needs a look: {a.name}",
            detail="; ".join(a.reasons) + (f". Came from {a.where_from[0]}" if a.where_from else "")
            + (f" via {a.quarantine.get('agent')}" if a.quarantine and a.quarantine.get("agent") else "") + f". {a.confidence}",
            recommended_action="Do not open it until you know what it is. Quarantining it (moved out of Downloads, "
                               "execute permission removed) is available through the approval gate.",
        )
        if store.record_and_classify(finding, now) == "new" and write_alerts and a.risk == "high":
            _write_alert(finding)
    return a
