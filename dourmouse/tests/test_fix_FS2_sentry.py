"""FS2 P4-57 (a recurrence is new again) and P4-58 (a closed case can be reopened as a new incident)."""

from __future__ import annotations

import sqlite3

from dourmouse.security.sentry import SentryFinding, SentryStore, run_scan

_OFF = {"available": True, "enabled": False, "raw": "State = 0"}
_ON = {"available": True, "enabled": True, "raw": "State = 1"}


def _state(firewall):
    return {"firewall": firewall, "listening_ports": {"available": True, "listening_ports": []},
            "arp_neighbors": {"available": True, "neighbors": []},
            "interfaces": {"available": True}, "default_gateway": {"available": True}, "dns": {"available": True}}


def _scan(store, firewall, t, **kw):
    return run_scan(state_fn=lambda: _state(firewall), store=store, now=lambda: t, write_alerts=False, **kw)


def test_firewall_switched_off_again_is_new_again(tmp_path):
    store = SentryStore(tmp_path / "s.db")
    first = _scan(store, _OFF, 1000.0)
    assert [f.kind for f in first.new_findings] == ["firewall_disabled"]
    again = _scan(store, _OFF, 1300.0)
    assert again.new_findings == []  # still the same ongoing condition
    cleared = _scan(store, _ON, 1600.0)
    assert cleared.all_findings == []
    back = _scan(store, _OFF, 1900.0)
    assert [f.kind for f in back.new_findings] == ["firewall_disabled"]
    assert _scan(store, _OFF, 2200.0).new_findings == []


def test_an_incomplete_scan_does_not_count_as_cleared(tmp_path):
    store = SentryStore(tmp_path / "s.db")
    _scan(store, _OFF, 1000.0)
    blind = _state({"available": False})
    run_scan(state_fn=lambda: blind, store=store, now=lambda: 1300.0, write_alerts=False)
    assert _scan(store, _OFF, 1600.0).new_findings == []


def test_a_dismissed_finding_stays_dismissed_when_it_comes_back(tmp_path):
    store = SentryStore(tmp_path / "s.db")
    f = _scan(store, _OFF, 1000.0).new_findings[0]
    store.mark_false_positive(f.fingerprint)
    _scan(store, _ON, 1300.0)
    back = _scan(store, _OFF, 1600.0)
    assert back.all_findings == [] and len(back.suppressed_false_positives) == 1


def test_old_database_without_the_column_is_migrated(tmp_path):
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE seen_findings (fingerprint TEXT PRIMARY KEY, kind TEXT NOT NULL, severity TEXT NOT NULL, "
              "title TEXT NOT NULL, first_seen REAL NOT NULL, last_seen REAL NOT NULL, times_seen INTEGER NOT NULL, "
              "dismissed_false_positive INTEGER NOT NULL DEFAULT 0)")
    c.execute("INSERT INTO seen_findings VALUES ('abc','firewall_disabled','high','t',1,1,1,0)")
    c.commit()
    c.close()
    store = SentryStore(db)
    f = SentryFinding("abc", "firewall_disabled", "high", "t", "d", "a")
    assert store.record_and_classify(f, 5.0) == "known"


def test_fresh_arrival_event_of_a_known_download_finding_is_new(tmp_path):
    store = SentryStore(tmp_path / "s.db")
    f = SentryFinding("dl1", "risky_download", "high", "Downloaded dmg", "d", "a")
    assert store.record_and_classify(f, 1.0) == "new"
    assert store.record_and_classify(f, 2.0) == "known"
    assert store.record_and_classify(f, 3.0, fresh_event=True) == "new"
    store.mark_false_positive("dl1")
    assert store.record_and_classify(f, 4.0, fresh_event=True) == "dismissed"


def test_risky_download_findings_are_not_cleared_by_a_scan(tmp_path):
    store = SentryStore(tmp_path / "s.db")
    f = SentryFinding("dl1", "risky_download", "high", "Downloaded dmg", "d", "a")
    store.record_and_classify(f, 1.0)
    _scan(store, _ON, 1300.0)
    assert store.record_and_classify(f, 1600.0) == "known"


# ---- P4-58 -------------------------------------------------------------------

def _seeded(tmp_path):
    store = SentryStore(tmp_path / "s.db")
    f = _scan(store, _OFF, 1000.0).new_findings[0]
    return store, f.fingerprint


def test_closed_case_can_be_followed_by_a_new_one(tmp_path):
    store, fp = _seeded(tmp_path)
    assert store.open_incident(fp, "first", now=1000.0) == "opened"
    assert store.open_incident(fp, "", now=1100.0) == "already_open"
    assert store.update_incident(fp, "RESOLVED", "fixed", now=2000.0) == "updated"
    assert store.open_incident(fp, "came back", now=3000.0) == "opened"
    current = store.get_incident(fp)
    assert current["status"] == "OPEN" and current["notes"] == [{"at": 3000.0, "text": "came back"}]
    # the new case is the one that is updated; the old record is kept
    assert store.update_incident(fp, "INVESTIGATING", None, now=3100.0) == "updated"
    rows = [i for i in store.list_incidents() if i["fingerprint"] == fp]
    assert sorted(i["status"] for i in rows) == ["INVESTIGATING", "RESOLVED"]
    assert store.open_incident(fp, "", now=3200.0) == "already_open"


def test_a_closed_case_still_refuses_to_reopen_in_place(tmp_path):
    store, fp = _seeded(tmp_path)
    store.open_incident(fp, "", now=1000.0)
    store.update_incident(fp, "ACCEPTED_RISK", "ok", now=2000.0)
    assert store.update_incident(fp, "OPEN", None, now=3000.0) == "terminal"


def test_existing_incidents_table_is_carried_over(tmp_path):
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE seen_findings (fingerprint TEXT PRIMARY KEY, kind TEXT NOT NULL, severity TEXT NOT NULL, "
              "title TEXT NOT NULL, first_seen REAL NOT NULL, last_seen REAL NOT NULL, times_seen INTEGER NOT NULL, "
              "dismissed_false_positive INTEGER NOT NULL DEFAULT 0)")
    c.execute("INSERT INTO seen_findings VALUES ('abc','firewall_disabled','high','t',1,1,1,0)")
    c.execute("CREATE TABLE incidents (fingerprint TEXT PRIMARY KEY, status TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '[]', "
              "opened_at REAL NOT NULL, updated_at REAL NOT NULL)")
    c.execute("INSERT INTO incidents VALUES ('abc','RESOLVED','[{\"at\": 1.0, \"text\": \"old\"}]',1.0,2.0)")
    c.commit()
    c.close()
    store = SentryStore(db)
    assert store.get_incident("abc")["status"] == "RESOLVED"
    assert store.open_incident("abc", "again", now=9.0) == "opened"
    assert len(store.list_incidents()) == 2
    store2 = SentryStore(db)  # re-opening the store must not copy the old rows a second time
    assert len(store2.list_incidents()) == 2
