"""FS2 P4-53: each history copy gets its own temp folder, so one browser's -wal never reaches another."""

from __future__ import annotations

import sqlite3

from dourmouse.security import browser_history as bh


def _db(path, rows=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE t (x)")
    for i in range(rows):
        c.execute("INSERT INTO t VALUES (?)", (i,))
    c.commit()
    c.close()


def test_stale_wal_of_an_earlier_browser_is_not_applied(tmp_path):
    a = tmp_path / "chrome" / "Default" / "History"
    b = tmp_path / "brave" / "Default" / "History"
    _db(a, 3)
    _db(b, 5)
    a.with_name("History-wal").write_bytes(b"\x37\x7f\x06\x82" + b"foreign wal pages" * 50)
    a.with_name("History-shm").write_bytes(b"\0" * 32768)
    work = tmp_path / "work"
    work.mkdir()
    ca = bh._copy_and_open(a, work)
    ca.close()
    cb = bh._copy_and_open(b, work)
    try:
        assert cb.execute("SELECT count(*) FROM t").fetchone()[0] == 5
    finally:
        cb.close()
    dests = [p for p in work.rglob("*") if p.is_file()]
    wal_owners = {p.parent for p in dests if p.name.endswith("-wal")}
    b_dirs = {p.parent for p in dests if not p.name.endswith(("-wal", "-shm"))}
    assert len(b_dirs) == 2 and len(wal_owners) == 1
    # the copy made for brave sits in a folder with no wal at all
    brave_dir = next(d for d in b_dirs if d not in wal_owners)
    assert not list(brave_dir.glob("*-wal")) and not list(brave_dir.glob("*-shm"))
