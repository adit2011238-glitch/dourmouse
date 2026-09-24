"""Finding #111 (MS-11): browser history and typed searches read locally
from each browser's own database schema (synthetic databases here, never the
developer's real history)."""

from __future__ import annotations

import shutil
import sqlite3

from dourmouse.security import browser_history as bh

NOW = 1_790_000_000.0


def _chromium(path, rows, terms):
    path.parent.mkdir(parents=True)
    c = sqlite3.connect(path)
    c.executescript("CREATE TABLE urls(id INTEGER PRIMARY KEY, url TEXT, title TEXT, last_visit_time INTEGER);"
                    "CREATE TABLE visits(id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER);"
                    "CREATE TABLE keyword_search_terms(keyword_id INTEGER, url_id INTEGER, term TEXT);")
    for i, (url, ago) in enumerate(rows, 1):
        t = int((NOW - ago + bh._CHROMIUM_EPOCH) * 1_000_000)
        c.execute("INSERT INTO urls VALUES(?,?,?,?)", (i, url, f"t{i}", t))
        c.execute("INSERT INTO visits(url, visit_time) VALUES(?,?)", (i, t))
    for url_id, term in terms:
        c.execute("INSERT INTO keyword_search_terms VALUES(1,?,?)", (url_id, term))
    c.commit()
    c.close()


def _safari(path, rows):
    path.parent.mkdir(parents=True)
    c = sqlite3.connect(path)
    c.executescript("CREATE TABLE history_items(id INTEGER PRIMARY KEY, url TEXT);"
                    "CREATE TABLE history_visits(id INTEGER PRIMARY KEY, history_item INTEGER, visit_time REAL, title TEXT);")
    for i, (url, ago) in enumerate(rows, 1):
        c.execute("INSERT INTO history_items VALUES(?,?)", (i, url))
        c.execute("INSERT INTO history_visits(history_item, visit_time, title) VALUES(?,?,?)",
                  (i, NOW - ago - bh._SAFARI_EPOCH, "s"))
    c.commit()
    c.close()


def _firefox(path, rows):
    path.parent.mkdir(parents=True)
    c = sqlite3.connect(path)
    c.executescript("CREATE TABLE moz_places(id INTEGER PRIMARY KEY, url TEXT, title TEXT);"
                    "CREATE TABLE moz_historyvisits(id INTEGER PRIMARY KEY, place_id INTEGER, visit_date INTEGER);")
    for i, (url, ago) in enumerate(rows, 1):
        c.execute("INSERT INTO moz_places VALUES(?,?,?)", (i, url, "f"))
        c.execute("INSERT INTO moz_historyvisits(place_id, visit_date) VALUES(?,?)", (i, int((NOW - ago) * 1_000_000)))
    c.commit()
    c.close()


def _setup(tmp_path, monkeypatch):
    chrome = tmp_path / "Chrome"
    _chromium(chrome / "Default" / "History",
              [("https://www.example.com/a", 60), ("https://www.google.com/search?q=mac+firewall", 120),
               ("https://old.example.org/", 3 * 86400)],
              [(2, "mac firewall")])
    _safari(tmp_path / "Safari" / "History.db", [("https://news.example.net/x", 30)])
    _firefox(tmp_path / "FF" / "abc.default" / "places.sqlite", [("https://login.evil-phish.example/", 10)])
    monkeypatch.setattr(bh, "CHROMIUM_ROOTS", {"Chrome": chrome, "Arc": tmp_path / "missing"})
    monkeypatch.setattr(bh, "SAFARI_DB", tmp_path / "Safari" / "History.db")
    monkeypatch.setattr(bh, "FIREFOX_PROFILES", tmp_path / "FF")


def test_every_browser_is_read_in_its_own_time_base(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    r = bh.recent_history(hours=24, now=NOW)
    assert [v["domain"] for v in r["visits"]] == ["login.evil-phish.example", "news.example.net", "example.com",
                                                   "google.com"]  # newest first; the 3-day-old one is out of range
    assert abs(r["visits"][0]["at"] - (NOW - 10)) < 1 and abs(r["visits"][1]["at"] - (NOW - 30)) < 1
    assert [s["term"] for s in r["searches"]] == ["mac firewall"]
    assert {s["source"]: s["status"] for s in r["sources"]} == {
        "Chrome Default": "read", "Safari": "read", "Firefox abc.default": "read"}


def test_protected_safari_history_is_reported_with_the_fix(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    real_copy = shutil.copy2

    def copy(src, dst, *a, **k):
        if str(src).endswith("History.db"):
            raise PermissionError(1, "Operation not permitted")
        return real_copy(src, dst, *a, **k)

    monkeypatch.setattr(shutil, "copy2", copy)
    r = bh.recent_history(hours=24, now=NOW)
    safari = next(s for s in r["sources"] if s["source"] == "Safari")
    assert safari["status"] == "no_access" and "Full Disk Access" in safari["detail"]
    assert "news.example.net" not in [v["domain"] for v in r["visits"]]


def test_a_locked_chrome_database_is_read_through_a_copy(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    db = tmp_path / "Chrome" / "Default" / "History"
    holder = sqlite3.connect(db)
    holder.execute("BEGIN EXCLUSIVE")  # what a running Chrome does to its file
    try:
        assert bh.recent_history(hours=24, now=NOW)["sources"][0]["status"] == "read"
    finally:
        holder.rollback()
        holder.close()


def test_blocked_visits_and_top_domains():
    visits = [{"domain": d} for d in ("a.com", "x.evil.com", "evil.com", "notevil.com", "a.com")]
    assert [v["domain"] for v in bh.blocked_visits(visits, {"evil.com"})] == ["x.evil.com", "evil.com"]
    assert bh.top_domains(visits, 2) == [("a.com", 2), ("evil.com", 1)]
    assert bh.domain_of("https://WWW.Example.COM:8443/p") == "example.com"
