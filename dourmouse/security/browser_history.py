"""Browser history and typed searches on this Mac (MS-11, spec SEC-E1;
finding #111).

Reads the browsers' own history databases, locally, and nothing else:
Chrome and the Chromium family (Arc, Brave, Edge; the ``urls``, ``visits``
and ``keyword_search_terms`` tables, the last of which holds the exact
search terms typed, so no network interception is needed), Safari
(``History.db``) and Firefox (``places.sqlite``). A running Chromium browser
locks its file, so each database is copied (with its WAL) before reading.

Safari's history is protected by macOS: reading it needs Full Disk Access
for the app running Dourmouse. That is reported per source as ``no_access``
with the fix, never silently skipped. Nothing here is uploaded; the
security use is local: visits to domains the owner blocked for good.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any

#: Chromium stores microseconds since 1601-01-01; Safari seconds since
#: 2001-01-01; Firefox microseconds since 1970-01-01.
_CHROMIUM_EPOCH = 11_644_473_600
_SAFARI_EPOCH = 978_307_200

APP_SUPPORT = Path.home() / "Library" / "Application Support"
CHROMIUM_ROOTS = {
    "Chrome": APP_SUPPORT / "Google" / "Chrome",
    "Arc": APP_SUPPORT / "Arc" / "User Data",
    "Brave": APP_SUPPORT / "BraveSoftware" / "Brave-Browser",
    "Edge": APP_SUPPORT / "Microsoft Edge",
}
SAFARI_DB = Path.home() / "Library" / "Safari" / "History.db"
FIREFOX_PROFILES = APP_SUPPORT / "Firefox" / "Profiles"


def domain_of(url: str) -> str:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _copy_and_open(db: Path, tmp: Path) -> sqlite3.Connection:
    dest = tmp / (db.parent.name.replace(" ", "_") + "-" + db.name)
    shutil.copy2(db, dest)
    for suffix in ("-wal", "-shm"):
        side = db.with_name(db.name + suffix)
        if side.exists():
            shutil.copy2(side, dest.with_name(dest.name + suffix))
    conn = sqlite3.connect(dest)
    conn.row_factory = sqlite3.Row
    return conn


def _read_chromium(browser: str, db: Path, since: float, limit: int, tmp: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    conn = _copy_and_open(db, tmp)
    try:
        cutoff = int((since + _CHROMIUM_EPOCH) * 1_000_000)
        visits = [
            {"browser": browser, "profile": db.parent.name, "url": r["url"], "title": r["title"] or "",
             "domain": domain_of(r["url"]), "at": r["visit_time"] / 1_000_000 - _CHROMIUM_EPOCH}
            for r in conn.execute(
                "SELECT urls.url, urls.title, visits.visit_time FROM visits JOIN urls ON urls.id = visits.url "
                "WHERE visits.visit_time >= ? ORDER BY visits.visit_time DESC LIMIT ?", (cutoff, limit))
        ]
        searches = [
            {"browser": browser, "profile": db.parent.name, "term": r["term"],
             "at": r["last_visit_time"] / 1_000_000 - _CHROMIUM_EPOCH}
            for r in conn.execute(
                "SELECT k.term, u.last_visit_time FROM keyword_search_terms k JOIN urls u ON u.id = k.url_id "
                "WHERE u.last_visit_time >= ? ORDER BY u.last_visit_time DESC LIMIT ?", (cutoff, limit))
        ]
    finally:
        conn.close()
    return visits, searches


def _read_safari(db: Path, since: float, limit: int, tmp: Path) -> list[dict[str, Any]]:
    conn = _copy_and_open(db, tmp)
    try:
        return [
            {"browser": "Safari", "profile": "", "url": r["url"], "title": r["title"] or "",
             "domain": domain_of(r["url"]), "at": r["visit_time"] + _SAFARI_EPOCH}
            for r in conn.execute(
                "SELECT i.url, v.title, v.visit_time FROM history_visits v JOIN history_items i ON i.id = v.history_item "
                "WHERE v.visit_time >= ? ORDER BY v.visit_time DESC LIMIT ?", (since - _SAFARI_EPOCH, limit))
        ]
    finally:
        conn.close()


def _read_firefox(db: Path, since: float, limit: int, tmp: Path) -> list[dict[str, Any]]:
    conn = _copy_and_open(db, tmp)
    try:
        return [
            {"browser": "Firefox", "profile": db.parent.name, "url": r["url"], "title": r["title"] or "",
             "domain": domain_of(r["url"]), "at": r["visit_date"] / 1_000_000}
            for r in conn.execute(
                "SELECT p.url, p.title, v.visit_date FROM moz_historyvisits v JOIN moz_places p ON p.id = v.place_id "
                "WHERE v.visit_date >= ? ORDER BY v.visit_date DESC LIMIT ?", (int(since * 1_000_000), limit))
        ]
    finally:
        conn.close()


def _sources() -> list[tuple[str, str, Path]]:
    out: list[tuple[str, str, Path]] = []
    for browser, root in CHROMIUM_ROOTS.items():
        if root.is_dir():
            for db in sorted(root.glob("*/History")):
                out.append(("chromium", browser, db))
    out.append(("safari", "Safari", SAFARI_DB))
    if FIREFOX_PROFILES.is_dir():
        for db in sorted(FIREFOX_PROFILES.glob("*/places.sqlite")):
            out.append(("firefox", "Firefox", db))
    return out


def recent_history(hours: float = 24.0, limit: int = 500, now: float | None = None) -> dict[str, Any]:
    since = (now or time.time()) - hours * 3600
    visits: list[dict[str, Any]] = []
    searches: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="dourmouse-history-") as t:
        tmp = Path(t)
        for kind, browser, db in _sources():
            label = f"{browser} {db.parent.name}" if kind != "safari" else "Safari"
            if not db.exists():
                sources.append({"source": label, "status": "not_found", "detail": str(db)})
                continue
            try:
                if kind == "chromium":
                    v, s = _read_chromium(browser, db, since, limit, tmp)
                    searches.extend(s)
                elif kind == "safari":
                    v = _read_safari(db, since, limit, tmp)
                else:
                    v = _read_firefox(db, since, limit, tmp)
            except PermissionError:
                sources.append({"source": label, "status": "no_access", "detail": (
                    "macOS protects this history. Grant Full Disk Access to the app running Dourmouse in "
                    "System Settings > Privacy & Security > Full Disk Access, then restart it.")})
                continue
            except (OSError, sqlite3.Error) as exc:
                sources.append({"source": label, "status": "unreadable", "detail": str(exc)})
                continue
            visits.extend(v)
            sources.append({"source": label, "status": "read", "detail": f"{len(v)} visit(s)"})
    visits.sort(key=lambda x: x["at"], reverse=True)
    searches.sort(key=lambda x: x["at"], reverse=True)
    return {"since": since, "visits": visits[:limit], "searches": searches[:limit], "sources": sources}


def top_domains(visits: list[dict[str, Any]], n: int = 15) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for v in visits:
        if v["domain"]:
            counts[v["domain"]] = counts.get(v["domain"], 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]


def blocked_visits(visits: list[dict[str, Any]], blocked_domains: set[str]) -> list[dict[str, Any]]:
    """Visits to a blocked domain or any of its subdomains."""
    out = []
    for v in visits:
        d = v["domain"]
        if any(d == b or d.endswith("." + b) for b in blocked_domains):
            out.append(v)
    return out
