"""FS2 P5-51 / P5-50: network-process matching by lsof command, and removed/changed baseline items."""

from __future__ import annotations

from dourmouse.security import mac_detectors as md
from dourmouse.security import sentry
from dourmouse.security.baseline import HOST, Anomaly, Observation


def _kinds(findings):
    return [f.kind for f in findings]


def test_long_named_unsigned_process_is_found_through_its_lsof_command():
    state = {"network_processes": [
        {"pid": 7, "name": "update-helper-daemon", "command": "update-he", "exe": "/opt/x/update-helper-daemon",
         "location": "other", "signature": {"kind": "unsigned"}},
    ]}
    a = Anomaly("new", Observation(HOST, "network_process", "update-he", ""))
    assert "new_unsigned_network_process" in _kinds(md.detect_mac_findings(state, [a]))


def test_spaced_name_is_found_through_the_escaped_lsof_command():
    state = {"network_processes": [
        {"pid": 8, "name": "My Sync Agent", "command": "My\\x20Sync", "exe": "/x/My Sync Agent",
         "location": "other", "signature": {"kind": "adhoc"}},
    ]}
    a = Anomaly("new", Observation(HOST, "network_process", "My\\x20Sync", ""))
    assert "new_unsigned_network_process" in _kinds(md.detect_mac_findings(state, [a]))


def test_one_unsigned_among_signed_processes_with_the_same_command_is_reported():
    state = {"network_processes": [
        {"pid": 1, "name": "Google Chrome Helper", "command": "Google\\x20C", "exe": "/a", "location": "other",
         "signature": {"kind": "signed"}},
        {"pid": 2, "name": "Google Chrome Fake", "command": "Google\\x20C", "exe": "/b", "location": "other",
         "signature": {"kind": "unsigned"}},
    ]}
    a = Anomaly("new", Observation(HOST, "network_process", "Google\\x20C", ""))
    assert "new_unsigned_network_process" in _kinds(md.detect_mac_findings(state, [a]))


def test_signed_process_still_no_finding_and_name_match_still_works():
    state = {"network_processes": [
        {"pid": 1, "name": "curl", "command": "curl", "exe": "/usr/bin/curl", "location": "system",
         "signature": {"kind": "signed"}},
        {"pid": 2, "name": "short", "exe": "/z", "location": "other", "signature": {"kind": "unsigned"}},
    ]}
    assert not md.detect_mac_findings(state, [Anomaly("new", Observation(HOST, "network_process", "curl", ""))])
    # an older snapshot without the command field still matches by name
    assert "new_unsigned_network_process" in _kinds(
        md.detect_mac_findings(state, [Anomaly("new", Observation(HOST, "network_process", "short", ""))]))


def test_collect_state_records_the_lsof_command_on_each_process(monkeypatch):
    from dourmouse.security import mac_telemetry as mt
    from dourmouse.security import platform_adapter as pa

    monkeypatch.setattr(pa, "get_system_security_state", lambda: {
        "established_connections": {"available": True, "connections": [{"command": "update-he", "pid": 7}]}})
    monkeypatch.setattr(mt, "get_wifi", lambda: {})
    monkeypatch.setattr(mt, "get_host_protections", lambda: {})
    monkeypatch.setattr(mt, "get_persistence_items", lambda: {})
    monkeypatch.setattr(mt, "process_details", lambda pid: {"pid": pid, "available": True, "name": "update-helper-daemon", "exe": "/x"})
    monkeypatch.setattr(mt, "code_signature", lambda exe: {"kind": "unsigned"})
    state = sentry.collect_state()
    assert state["network_processes"][0]["command"] == "update-he"


def test_removed_persistence_item_is_reported():
    a = Anomaly("gone", Observation(HOST, "persistence", "/Users/x/Library/LaunchAgents/com.x.plist", "abc"), previous_value="abc")
    out = md.detect_mac_findings({}, [a])
    assert _kinds(out) == ["persistence_removed"]
    assert "com.x.plist" in out[0].title and out[0].fingerprint


def test_changed_listening_port_is_not_described_as_newly_opened():
    changed = Anomaly("changed", Observation(HOST, "listening_port", "node|TCP|3000", "ALL_INTERFACES"), previous_value="LOOPBACK_ONLY")
    new = Anomaly("new", Observation(HOST, "listening_port", "node|TCP|3000", "ALL_INTERFACES"))
    c = md.detect_mac_findings({}, [changed])[0]
    n = md.detect_mac_findings({}, [new])[0]
    assert "not open before" not in c.detail and "loopback" in c.detail.lower() and "all interfaces" in c.detail.lower()
    assert "not open before" in n.detail
    assert c.fingerprint != n.fingerprint


# --- the removed-item report must be safe end to end (P5-50) -------------------

def _scan_state(items, available=True):
    from dourmouse.tests.test_security_baseline import _state

    return _state(persistence={"available": available, "items": items})


_ITEM = {"path": "/Users/me/Library/LaunchAgents/com.good.plist", "sha256": "1" * 64, "label": "com.good",
         "program": "/Applications/Good.app/Contents/MacOS/Good", "run_at_load": True}


def _learned_store(tmp_path):
    from dourmouse.security import baseline as bl

    store = sentry.SentryStore(tmp_path / "s.db")
    t = 0.0
    for _ in range(bl.LEARNING_MIN_SCANS + 1):
        sentry.run_scan(state_fn=lambda: _scan_state([_ITEM]), store=store, now=lambda t=t: t, write_alerts=False)
        t += bl.LEARNING_MIN_SECONDS
    return store, t


def test_removed_item_is_reported_once_then_forgotten(tmp_path):
    store, t = _learned_store(tmp_path)
    first = sentry.run_scan(state_fn=lambda: _scan_state([]), store=store, now=lambda: t, write_alerts=False)
    assert "persistence_removed" in [f.kind for f in first.all_findings]
    second = sentry.run_scan(state_fn=lambda: _scan_state([]), store=store, now=lambda: t + 1, write_alerts=False)
    assert "persistence_removed" not in [f.kind for f in second.all_findings]


def test_unavailable_persistence_telemetry_does_not_report_everything_gone(tmp_path):
    store, t = _learned_store(tmp_path)
    res = sentry.run_scan(state_fn=lambda: _scan_state([], available=False), store=store, now=lambda: t, write_alerts=False)
    assert "persistence_removed" not in [f.kind for f in res.all_findings]
    # and the item is still remembered, so a later real removal is still caught
    res = sentry.run_scan(state_fn=lambda: _scan_state([]), store=store, now=lambda: t + 1, write_alerts=False)
    assert "persistence_removed" in [f.kind for f in res.all_findings]


def test_an_unreadable_plist_is_not_reported_as_removed(tmp_path):
    store, t = _learned_store(tmp_path)
    res = sentry.run_scan(state_fn=lambda: _scan_state([{"path": _ITEM["path"], "error": "unreadable: bad plist"}]),
                          store=store, now=lambda: t, write_alerts=False)
    assert "persistence_removed" not in [f.kind for f in res.all_findings]
