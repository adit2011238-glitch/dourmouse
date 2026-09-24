"""Finding #100 (MS-2 baseline engine, MS-3 Mac detectors), including the
full scan path: learning silently, then reporting only what changed."""

from __future__ import annotations

import copy

from dourmouse.security import baseline as bl
from dourmouse.security.mac_detectors import detect_mac_findings
from dourmouse.security.sentry import SentryStore, run_scan

NET = "net1"


def _state(**over):
    s = {
        "default_gateway": {"available": True, "gateway": "192.168.1.1"},
        "arp_neighbors": {"available": True, "neighbors": [
            {"ip": "192.168.1.1", "mac": "aa:aa:aa:aa:aa:aa"}, {"ip": "192.168.1.20", "mac": "bb:bb:bb:bb:bb:bb"}]},
        "dns": {"available": True, "resolvers": [{"nameservers": ["192.168.1.1"], "search_domains": ["home.lan"]}]},
        "listening_ports": {"available": True, "listening_ports": [
            {"command": "sshd", "protocol": "TCP", "port": 22, "exposure": "ALL_INTERFACES", "pid": 1, "bind_address": "*"}]},
        "established_connections": {"available": True, "connections": [{"command": "Safari", "pid": 10}]},
        "firewall": {"available": True, "enabled": True},
        "wifi": {"available": True, "connected": True, "security": "wpa2_personal"},
        "host_protections": {"available": True, "checks": {
            "filevault": {"on": True}, "sip": {"on": True}, "gatekeeper": {"on": True}, "stealth_mode": {"on": True},
            "remote_login": {"on": False, "accepting": False}, "screen_sharing": {"on": False, "accepting": False},
            "firewall": {"on": True}}},
        "persistence": {"available": True, "items": [
            {"path": "/Users/me/Library/LaunchAgents/com.good.plist", "sha256": "1" * 64, "label": "com.good",
             "program": "/Applications/Good.app/Contents/MacOS/Good", "run_at_load": True}]},
        "network_processes": [{"pid": 10, "name": "Safari", "exe": "/Applications/Safari.app/Contents/MacOS/Safari",
                               "location": "applications", "parent_name": "launchd",
                               "signature": {"kind": "apple", "gatekeeper": {"verdict": "accepted"}}}],
    }
    s.update(over)
    return s


class TestBaselineEngine:
    def test_observations_cover_host_and_network_scopes(self):
        obs = bl.observations(_state(), NET)
        cats = {(o.scope, o.category) for o in obs}
        assert ("host", "listening_port") in cats and ("host", "persistence") in cats
        assert (NET, "gateway_mac") in cats and (NET, "dns_resolvers") in cats and (NET, "wifi_security") in cats
        assert len(obs) == len(set(obs))

    def test_compare_reports_new_changed_and_gone(self):
        known = {("host", "listening_port", "sshd|TCP|22"): "ALL_INTERFACES",
                 (NET, "gateway_mac", "192.168.1.1"): "aa:aa:aa:aa:aa:aa",
                 ("host", "persistence", "/old.plist"): "x"}
        now = [bl.Observation("host", "listening_port", "sshd|TCP|22", "ALL_INTERFACES"),
               bl.Observation(NET, "gateway_mac", "192.168.1.1", "cc:cc:cc:cc:cc:cc"),
               bl.Observation("host", "listening_port", "nc|TCP|4444", "ALL_INTERFACES")]
        kinds = {(a.kind, a.observation.key) for a in bl.compare(now, known)}
        assert kinds == {("changed", "192.168.1.1"), ("new", "nc|TCP|4444"), ("gone", "/old.plist")}

    def test_learning_needs_scans_and_time(self):
        assert bl.is_learning(None, 0)
        assert bl.is_learning({"first_seen": 0, "scans": 10}, 60)  # plenty of scans, too little time
        assert bl.is_learning({"first_seen": 0, "scans": 1}, 10_000)  # time but too few scans
        assert not bl.is_learning({"first_seen": 0, "scans": 3}, bl.LEARNING_MIN_SECONDS)


class TestPostureDetectors:
    def test_a_clean_mac_has_no_posture_findings(self):
        assert detect_mac_findings(_state(), []) == []

    def test_each_weak_protection_is_reported_once(self):
        s = _state()
        checks = s["host_protections"]["checks"]
        checks["filevault"]["on"] = False
        checks["remote_login"] = {"on": True, "accepting": True}
        kinds = sorted(f.kind for f in detect_mac_findings(s, []))
        assert kinds == ["filevault_off", "remote_login_on"]

    def test_a_check_that_could_not_run_is_not_called_off(self):
        s = _state()
        s["host_protections"]["checks"]["sip"]["on"] = None
        assert detect_mac_findings(s, []) == []

    def test_open_wifi_is_high(self):
        (f,) = detect_mac_findings(_state(wifi={"connected": True, "security": "open"}), [])
        assert f.kind == "weak_wifi" and f.severity == "high"

    def test_another_device_with_the_routers_mac_is_arp_spoofing(self):
        s = _state()
        s["arp_neighbors"]["neighbors"].append({"ip": "192.168.1.66", "mac": "aa:aa:aa:aa:aa:aa"})
        (f,) = detect_mac_findings(s, [])
        assert f.kind == "arp_gateway_duplicate" and f.severity == "high" and "192.168.1.66" in f.detail

    def test_a_network_process_in_downloads_is_high(self):
        s = _state(network_processes=[{"pid": 7, "name": "update", "exe": "/Users/me/Downloads/update",
                                       "location": "downloads", "parent_name": "bash", "signature": {"kind": "unsigned"}}])
        (f,) = detect_mac_findings(s, [])
        assert f.kind == "network_process_suspicious_location" and f.severity == "high"


class TestFullScanPath:
    def _clock(self):
        t = {"now": 1_000_000.0}

        def now():
            return t["now"]
        return t, now

    def test_learns_silently_then_reports_only_what_changed(self, tmp_path):
        store = SentryStore(tmp_path / "s.db")
        t, now = self._clock()
        state = _state()
        for _ in range(3):  # learning: 3 scans over 30+ minutes
            r = run_scan(state_fn=lambda: state, store=store, now=now, write_alerts=False)
            assert not [f for f in r.new_findings if f.kind.startswith(("new_", "gateway", "dns_"))]
            t["now"] += 20 * 60

        spoofed = copy.deepcopy(state)
        spoofed["arp_neighbors"]["neighbors"][0]["mac"] = "ee:ee:ee:ee:ee:ee"
        spoofed["listening_ports"]["listening_ports"].append(
            {"command": "nc", "protocol": "TCP", "port": 4444, "exposure": "ALL_INTERFACES", "pid": 9, "bind_address": "*"})
        spoofed["persistence"]["items"].append(
            {"path": "/Users/me/Library/LaunchAgents/com.x.plist", "sha256": "9" * 64, "label": "com.x",
             "program": "/Users/me/.cache/x", "run_at_load": True})
        r = run_scan(state_fn=lambda: spoofed, store=store, now=now, write_alerts=False)
        kinds = {f.kind: f for f in r.new_findings}
        assert kinds["gateway_mac_changed"].severity == "high"
        assert "new_listening_port" in kinds
        assert kinds["new_persistence"].severity == "high"  # runs from a hidden folder

        # The next identical scan reports nothing new: the baseline learned it.
        t["now"] += 300
        r2 = run_scan(state_fn=lambda: spoofed, store=store, now=now, write_alerts=False)
        assert not [f for f in r2.new_findings if f.kind in kinds]

    def test_a_new_network_learns_on_its_own(self, tmp_path):
        store = SentryStore(tmp_path / "s.db")
        t, now = self._clock()
        for _ in range(3):
            run_scan(state_fn=lambda: _state(), store=store, now=now, write_alerts=False)
            t["now"] += 20 * 60
        cafe = _state(default_gateway={"available": True, "gateway": "10.0.0.1"},
                      dns={"available": True, "resolvers": [{"nameservers": ["10.0.0.1"], "search_domains": ["cafe"]}]})
        cafe["arp_neighbors"]["neighbors"] = [{"ip": "10.0.0.1", "mac": "12:34:56:78:9a:bc"}]
        r = run_scan(state_fn=lambda: cafe, store=store, now=now, write_alerts=False)
        # A different router MAC on a different network is not an anomaly.
        assert "gateway_mac_changed" not in {f.kind for f in r.new_findings}
