"""dourmouse/security/sentry.py -- the AI security sentry's real
deterministic detection, scoring, and false-positive memory. No model
call anywhere in this module (a deliberate correction to this domain's
own build plan, see the module's own docstring); every test here is a
plain, hermetic, real-store test -- no fakes needed."""

from __future__ import annotations

import time

import pytest

from dourmouse.security.sentry import (
    SentryFinding,
    SentryRuntime,
    SentryStore,
    _detect_correlations,
    _detect_findings,
    _device_key,
    run_scan,
    sentry_runtime_enabled,
)

_FIREWALL_OFF = {"available": True, "enabled": False}
_FIREWALL_ON = {"available": True, "enabled": True}
_FIREWALL_UNAVAILABLE = {"available": False, "reason": "no such binary"}

_NO_PORTS = {"available": True, "listening_ports": []}

_NEIGHBOR_A = {"hostname": "laptop.local", "ip": "192.168.1.10", "mac": "aa:aa:aa:aa:aa:aa", "interface": "en0"}
_NEIGHBOR_B = {"hostname": None, "ip": "192.168.1.11", "mac": "bb:bb:bb:bb:bb:bb", "interface": "en0"}
_NEIGHBOR_INCOMPLETE = {"hostname": None, "ip": "192.168.1.12", "mac": None, "interface": "en0"}
_NO_NEIGHBORS = {"available": True, "neighbors": []}


def _state(firewall=_FIREWALL_ON, listening_ports=_NO_PORTS, arp_neighbors=_NO_NEIGHBORS):
    return {"firewall": firewall, "listening_ports": listening_ports, "arp_neighbors": arp_neighbors}


class TestDetectFindings:
    def test_disabled_firewall_is_a_high_finding(self):
        findings = _detect_findings(_state(firewall=_FIREWALL_OFF))
        assert len(findings) == 1
        assert findings[0].severity == "high"
        assert findings[0].kind == "firewall_disabled"

    def test_enabled_firewall_is_no_finding(self):
        assert _detect_findings(_state(firewall=_FIREWALL_ON)) == []

    def test_unavailable_firewall_telemetry_is_honestly_no_finding(self):
        """Unavailable is not the same as disabled -- never fabricate a
        finding from a telemetry read that simply failed."""
        assert _detect_findings(_state(firewall=_FIREWALL_UNAVAILABLE)) == []

    def test_all_interfaces_port_is_a_med_finding(self):
        ports = {"available": True, "listening_ports": [
            {"command": "sshd", "pid": 123, "protocol": "TCP", "port": 22,
             "bind_address": "*", "exposure": "ALL_INTERFACES"},
        ]}
        findings = _detect_findings(_state(listening_ports=ports))
        assert len(findings) == 1
        assert findings[0].severity == "med"
        assert findings[0].kind == "exposed_port"
        assert "sshd" in findings[0].title

    def test_loopback_and_local_network_ports_are_no_finding(self):
        ports = {"available": True, "listening_ports": [
            {"command": "a", "pid": 1, "protocol": "TCP", "port": 1,
             "bind_address": "127.0.0.1", "exposure": "LOOPBACK_ONLY"},
            {"command": "b", "pid": 2, "protocol": "TCP", "port": 2,
             "bind_address": "192.168.1.5", "exposure": "LOCAL_NETWORK"},
            {"command": "c", "pid": 3, "protocol": "TCP", "port": 3,
             "bind_address": "100.1.2.3", "exposure": "TAILSCALE"},
        ]}
        assert _detect_findings(_state(listening_ports=ports)) == []

    def test_same_condition_always_gets_the_same_fingerprint(self):
        f1 = _detect_findings(_state(firewall=_FIREWALL_OFF))[0]
        f2 = _detect_findings(_state(firewall=_FIREWALL_OFF))[0]
        assert f1.fingerprint == f2.fingerprint


class TestDeviceKey:
    def test_mac_is_the_key_when_present(self):
        assert _device_key("aa:bb:cc:dd:ee:ff", "192.168.1.1") == "aa:bb:cc:dd:ee:ff"

    def test_ip_prefixed_fallback_when_mac_is_none(self):
        assert _device_key(None, "192.168.1.1") == "ip:192.168.1.1"


class TestDetectFindingsNewDevice:
    def test_no_baseline_means_the_rule_is_skipped_entirely(self):
        """known_device_keys=None (this codebase's own real 'no baseline
        yet' convention) must never be silently treated as an empty set --
        an empty set would flag every real device as new."""
        arp = {"available": True, "neighbors": [_NEIGHBOR_A, _NEIGHBOR_B]}
        findings = _detect_findings(_state(arp_neighbors=arp), known_device_keys=None)
        assert findings == []

    def test_a_device_not_in_the_real_baseline_is_a_med_finding(self):
        arp = {"available": True, "neighbors": [_NEIGHBOR_A]}
        findings = _detect_findings(_state(arp_neighbors=arp), known_device_keys=set())
        assert len(findings) == 1
        assert findings[0].kind == "new_device"
        assert findings[0].severity == "med"
        assert "laptop.local" in findings[0].title

    def test_a_device_already_in_the_real_baseline_is_no_finding(self):
        arp = {"available": True, "neighbors": [_NEIGHBOR_A]}
        findings = _detect_findings(
            _state(arp_neighbors=arp), known_device_keys={_device_key("aa:aa:aa:aa:aa:aa", "192.168.1.10")}
        )
        assert findings == []

    def test_an_incomplete_arp_entry_falls_back_to_its_ip_key(self):
        arp = {"available": True, "neighbors": [_NEIGHBOR_INCOMPLETE]}
        findings = _detect_findings(_state(arp_neighbors=arp), known_device_keys=set())
        assert len(findings) == 1
        assert "unknown" in findings[0].detail  # honest -- no real MAC to report

    def test_unavailable_arp_telemetry_is_honestly_no_finding(self):
        arp = {"available": False, "reason": "arp not found"}
        findings = _detect_findings(_state(arp_neighbors=arp), known_device_keys=set())
        assert findings == []


_NEW_DEVICE_FINDING = SentryFinding(
    fingerprint="fp_device", kind="new_device", severity="med",
    title="New device joined the network: intruder.local",
    detail="detail", recommended_action="investigate",
)
_EXPOSED_PORT_FINDING = SentryFinding(
    fingerprint="fp_port", kind="exposed_port", severity="med",
    title="sshd listens on all interfaces",
    detail="detail", recommended_action="restrict it",
)
_FIREWALL_FINDING = SentryFinding(
    fingerprint="fp_fw", kind="firewall_disabled", severity="high",
    title="Application Firewall is disabled",
    detail="detail", recommended_action="enable it",
)


class TestDetectCorrelations:
    def test_no_correlation_with_only_one_signal(self):
        assert _detect_correlations([_NEW_DEVICE_FINDING]) == []
        assert _detect_correlations([_EXPOSED_PORT_FINDING]) == []

    def test_no_correlation_with_unrelated_signals(self):
        assert _detect_correlations([_NEW_DEVICE_FINDING, _FIREWALL_FINDING]) == []

    def test_both_signals_together_is_a_real_high_correlation(self):
        correlations = _detect_correlations([_NEW_DEVICE_FINDING, _EXPOSED_PORT_FINDING])
        assert len(correlations) == 1
        assert correlations[0].severity == "high"
        assert correlations[0].kind == "correlated_new_device_and_exposed_port"
        assert "intruder.local" in correlations[0].detail
        assert "sshd" in correlations[0].detail

    def test_correlation_fingerprint_is_stable_for_the_same_pair(self):
        c1 = _detect_correlations([_NEW_DEVICE_FINDING, _EXPOSED_PORT_FINDING])[0]
        c2 = _detect_correlations([_NEW_DEVICE_FINDING, _EXPOSED_PORT_FINDING])[0]
        assert c1.fingerprint == c2.fingerprint

    def test_empty_new_findings_is_no_correlation(self):
        assert _detect_correlations([]) == []


class TestSentryStore:
    def test_first_detection_is_new(self, tmp_path):
        store = SentryStore(tmp_path / "sentry.db")
        finding = _detect_findings(_state(firewall=_FIREWALL_OFF))[0]
        assert store.record_and_classify(finding, now=1000.0) == "new"

    def test_second_detection_of_the_same_finding_is_known_not_new(self, tmp_path):
        store = SentryStore(tmp_path / "sentry.db")
        finding = _detect_findings(_state(firewall=_FIREWALL_OFF))[0]
        store.record_and_classify(finding, now=1000.0)
        assert store.record_and_classify(finding, now=2000.0) == "known"

    def test_dismissed_finding_stays_dismissed_on_future_scans(self, tmp_path):
        store = SentryStore(tmp_path / "sentry.db")
        finding = _detect_findings(_state(firewall=_FIREWALL_OFF))[0]
        store.record_and_classify(finding, now=1000.0)
        assert store.mark_false_positive(finding.fingerprint) is True
        assert store.record_and_classify(finding, now=2000.0) == "dismissed"
        assert store.record_and_classify(finding, now=3000.0) == "dismissed"

    def test_dismissing_an_unseen_fingerprint_is_honest_false_not_a_crash(self, tmp_path):
        store = SentryStore(tmp_path / "sentry.db")
        assert store.mark_false_positive("never-seen") is False

    def test_snapshot_reflects_real_persisted_state(self, tmp_path):
        store = SentryStore(tmp_path / "sentry.db")
        finding = _detect_findings(_state(firewall=_FIREWALL_OFF))[0]
        store.record_and_classify(finding, now=1000.0)
        store.record_and_classify(finding, now=2000.0)
        rows = store.snapshot()
        assert len(rows) == 1
        assert rows[0]["times_seen"] == 2
        assert rows[0]["dismissed_false_positive"] == 0

    def test_store_persists_across_instances(self, tmp_path):
        """Same real-file-on-disk contract as every other store in this
        codebase -- a fresh SentryStore(same path) sees prior writes."""
        path = tmp_path / "sentry.db"
        finding = _detect_findings(_state(firewall=_FIREWALL_OFF))[0]
        SentryStore(path).record_and_classify(finding, now=1000.0)
        assert SentryStore(path).record_and_classify(finding, now=2000.0) == "known"

    def test_get_known_device_keys_starts_empty(self, tmp_path):
        store = SentryStore(tmp_path / "sentry.db")
        assert store.get_known_device_keys() == set()

    def test_record_devices_seeds_the_baseline(self, tmp_path):
        store = SentryStore(tmp_path / "sentry.db")
        store.record_devices([_NEIGHBOR_A, _NEIGHBOR_B], now=1000.0)
        assert store.get_known_device_keys() == {
            "aa:aa:aa:aa:aa:aa", "bb:bb:bb:bb:bb:bb",
        }

    def test_record_devices_refreshes_last_seen_not_first_seen(self, tmp_path):
        store = SentryStore(tmp_path / "sentry.db")
        store.record_devices([_NEIGHBOR_A], now=1000.0)
        store.record_devices([_NEIGHBOR_A], now=2000.0)
        rows = store.devices_snapshot()
        assert len(rows) == 1
        assert rows[0]["first_seen"] == 1000.0
        assert rows[0]["last_seen"] == 2000.0

    def test_devices_snapshot_reflects_real_persisted_state(self, tmp_path):
        store = SentryStore(tmp_path / "sentry.db")
        store.record_devices([_NEIGHBOR_A], now=1000.0)
        rows = store.devices_snapshot()
        assert len(rows) == 1
        assert rows[0]["ip"] == "192.168.1.10"
        assert rows[0]["hostname"] == "laptop.local"


class TestSentryStoreIncidents:
    def _seeded_store(self, tmp_path):
        store = SentryStore(tmp_path / "sentry.db")
        finding = _detect_findings(_state(firewall=_FIREWALL_OFF))[0]
        store.record_and_classify(finding, now=1000.0)
        return store, finding.fingerprint

    def test_opening_an_incident_for_an_unknown_fingerprint_is_honest(self, tmp_path):
        store = SentryStore(tmp_path / "sentry.db")
        assert store.open_incident("never-seen", "note", now=1000.0) == "unknown_fingerprint"

    def test_opening_a_real_incident_succeeds(self, tmp_path):
        store, fp = self._seeded_store(tmp_path)
        assert store.open_incident(fp, "investigating", now=1000.0) == "opened"
        incident = store.get_incident(fp)
        assert incident["status"] == "OPEN"
        assert incident["notes"] == [{"at": 1000.0, "text": "investigating"}]

    def test_opening_an_already_open_incident_is_idempotent(self, tmp_path):
        store, fp = self._seeded_store(tmp_path)
        store.open_incident(fp, "", now=1000.0)
        assert store.open_incident(fp, "", now=2000.0) == "already_open"

    def test_updating_an_unknown_incident_is_honest(self, tmp_path):
        store, _fp = self._seeded_store(tmp_path)
        assert store.update_incident("never-opened", "INVESTIGATING", None, now=1000.0) == "not_found"

    def test_an_invalid_status_raises(self, tmp_path):
        store, fp = self._seeded_store(tmp_path)
        store.open_incident(fp, "", now=1000.0)
        with pytest.raises(ValueError):
            store.update_incident(fp, "NOT_A_REAL_STATUS", None, now=2000.0)

    def test_a_real_status_transition_and_note_are_recorded(self, tmp_path):
        store, fp = self._seeded_store(tmp_path)
        store.open_incident(fp, "", now=1000.0)
        assert store.update_incident(fp, "INVESTIGATING", "looked into it", now=2000.0) == "updated"
        incident = store.get_incident(fp)
        assert incident["status"] == "INVESTIGATING"
        assert incident["notes"] == [{"at": 2000.0, "text": "looked into it"}]

    def test_a_note_only_update_does_not_change_status(self, tmp_path):
        store, fp = self._seeded_store(tmp_path)
        store.open_incident(fp, "", now=1000.0)
        store.update_incident(fp, "INVESTIGATING", None, now=2000.0)
        store.update_incident(fp, None, "still working on it", now=3000.0)
        incident = store.get_incident(fp)
        assert incident["status"] == "INVESTIGATING"
        assert len(incident["notes"]) == 1

    def test_a_terminal_incident_never_silently_reopens(self, tmp_path):
        store, fp = self._seeded_store(tmp_path)
        store.open_incident(fp, "", now=1000.0)
        store.update_incident(fp, "RESOLVED", "fixed", now=2000.0)
        assert store.update_incident(fp, "OPEN", "wait actually", now=3000.0) == "terminal"
        assert store.get_incident(fp)["status"] == "RESOLVED"

    def test_a_terminal_incident_can_still_be_re_set_to_the_same_terminal_status(self, tmp_path):
        """A note-only update or a no-op re-confirmation of the SAME
        terminal status must not be refused -- only an attempt to change
        AWAY from a terminal status is."""
        store, fp = self._seeded_store(tmp_path)
        store.open_incident(fp, "", now=1000.0)
        store.update_incident(fp, "RESOLVED", "fixed", now=2000.0)
        assert store.update_incident(fp, "RESOLVED", "confirmed still fixed", now=3000.0) == "updated"

    def test_get_incident_for_a_never_opened_fingerprint_is_none(self, tmp_path):
        store, _fp = self._seeded_store(tmp_path)
        assert store.get_incident("never-opened") is None

    def test_list_incidents_filters_by_status(self, tmp_path):
        store = SentryStore(tmp_path / "sentry.db")
        f1 = _detect_findings(_state(firewall=_FIREWALL_OFF))[0]
        ports = {"available": True, "listening_ports": [
            {"command": "sshd", "pid": 1, "protocol": "TCP", "port": 22,
             "bind_address": "*", "exposure": "ALL_INTERFACES"},
        ]}
        f2 = _detect_findings(_state(listening_ports=ports))[0]
        store.record_and_classify(f1, now=1000.0)
        store.record_and_classify(f2, now=1000.0)
        store.open_incident(f1.fingerprint, "", now=1000.0)
        store.open_incident(f2.fingerprint, "", now=1000.0)
        store.update_incident(f2.fingerprint, "RESOLVED", "closed", now=2000.0)
        assert {i["fingerprint"] for i in store.list_incidents("OPEN")} == {f1.fingerprint}
        assert {i["fingerprint"] for i in store.list_incidents("RESOLVED")} == {f2.fingerprint}
        assert len(store.list_incidents()) == 2


class TestRunScan:
    def test_clean_state_has_zero_findings_and_zero_risk(self, tmp_path):
        result = run_scan(
            state_fn=lambda: _state(), store=SentryStore(tmp_path / "s.db"), now=lambda: 1000.0,
        )
        assert result.all_findings == []
        assert result.new_findings == []
        assert result.risk_score == 0.0

    def test_first_scan_with_a_real_finding_is_new_and_scored(self, tmp_path):
        result = run_scan(
            state_fn=lambda: _state(firewall=_FIREWALL_OFF),
            store=SentryStore(tmp_path / "s.db"), now=lambda: 1000.0, write_alerts=False,
        )
        assert len(result.new_findings) == 1
        assert result.risk_score == 9.0  # the real HIGH weight, not a guess

    def test_second_scan_of_the_same_condition_is_not_new_but_still_scored(self, tmp_path):
        store = SentryStore(tmp_path / "s.db")
        state_fn = lambda: _state(firewall=_FIREWALL_OFF)  # noqa: E731
        run_scan(state_fn=state_fn, store=store, now=lambda: 1000.0, write_alerts=False)
        result = run_scan(state_fn=state_fn, store=store, now=lambda: 2000.0, write_alerts=False)
        assert result.new_findings == []  # not new the second time
        assert len(result.all_findings) == 1  # but still real and reported
        assert result.risk_score == 9.0  # still counted -- it is still true

    def test_dismissed_finding_drops_out_of_risk_score_and_all_findings(self, tmp_path):
        store = SentryStore(tmp_path / "s.db")
        state_fn = lambda: _state(firewall=_FIREWALL_OFF)  # noqa: E731
        run_scan(state_fn=state_fn, store=store, now=lambda: 1000.0, write_alerts=False)
        finding = _detect_findings(_state(firewall=_FIREWALL_OFF))[0]
        store.mark_false_positive(finding.fingerprint)
        result = run_scan(state_fn=state_fn, store=store, now=lambda: 2000.0, write_alerts=False)
        assert result.all_findings == []
        assert result.risk_score == 0.0
        assert len(result.suppressed_false_positives) == 1

    def test_only_high_severity_writes_a_real_alert(self, tmp_path, monkeypatch):
        written = []
        monkeypatch.setattr(
            "dourmouse.state_store.default_store",
            lambda: type("S", (), {"add_alert": lambda self, **kw: written.append(kw)})(),
        )
        ports = {"available": True, "listening_ports": [
            {"command": "sshd", "pid": 1, "protocol": "TCP", "port": 22,
             "bind_address": "*", "exposure": "ALL_INTERFACES"},
        ]}
        run_scan(
            state_fn=lambda: _state(listening_ports=ports),
            store=SentryStore(tmp_path / "s.db"), now=lambda: 1000.0,
        )
        assert written == []  # MED severity: reported, never proactively alerted

    def test_new_high_severity_finding_writes_a_real_alert(self, tmp_path, monkeypatch):
        written = []
        monkeypatch.setattr(
            "dourmouse.state_store.default_store",
            lambda: type("S", (), {"add_alert": lambda self, **kw: written.append(kw)})(),
        )
        result = run_scan(
            state_fn=lambda: _state(firewall=_FIREWALL_OFF),
            store=SentryStore(tmp_path / "s.db"), now=lambda: 1000.0,
        )
        assert len(written) == 1
        assert result.alerts_written == 1
        assert "firewall" in written[0]["title"].lower()

    def test_a_broken_alert_write_never_breaks_the_real_scan(self, tmp_path, monkeypatch):
        def _raise():
            raise RuntimeError("state store unavailable")

        monkeypatch.setattr("dourmouse.state_store.default_store", _raise)
        result = run_scan(
            state_fn=lambda: _state(firewall=_FIREWALL_OFF),
            store=SentryStore(tmp_path / "s.db"), now=lambda: 1000.0,
        )
        assert len(result.new_findings) == 1  # the real scan result is intact
        assert result.alerts_written == 0  # honest: the alert genuinely did not land

    def test_first_ever_scan_seeds_the_baseline_without_flagging_any_device(self, tmp_path):
        store = SentryStore(tmp_path / "s.db")
        arp = {"available": True, "neighbors": [_NEIGHBOR_A, _NEIGHBOR_B]}
        result = run_scan(
            state_fn=lambda: _state(arp_neighbors=arp), store=store, now=lambda: 1000.0,
        )
        assert result.all_findings == []
        assert store.get_known_device_keys() == {
            "aa:aa:aa:aa:aa:aa", "bb:bb:bb:bb:bb:bb",
        }

    def test_a_real_new_device_on_a_later_scan_is_a_med_finding(self, tmp_path):
        store = SentryStore(tmp_path / "s.db")
        run_scan(
            state_fn=lambda: _state(arp_neighbors={"available": True, "neighbors": [_NEIGHBOR_A]}),
            store=store, now=lambda: 1000.0,
        )
        result = run_scan(
            state_fn=lambda: _state(
                arp_neighbors={"available": True, "neighbors": [_NEIGHBOR_A, _NEIGHBOR_B]}
            ),
            store=store, now=lambda: 2000.0, write_alerts=False,
        )
        assert len(result.new_findings) == 1
        assert result.new_findings[0].kind == "new_device"
        assert store.get_known_device_keys() == {
            "aa:aa:aa:aa:aa:aa", "bb:bb:bb:bb:bb:bb",
        }

    def test_a_new_device_and_exposed_port_in_the_same_scan_correlate(self, tmp_path, monkeypatch):
        written = []
        monkeypatch.setattr(
            "dourmouse.state_store.default_store",
            lambda: type("S", (), {"add_alert": lambda self, **kw: written.append(kw)})(),
        )
        store = SentryStore(tmp_path / "s.db")
        ports = {"available": True, "listening_ports": [
            {"command": "sshd", "pid": 1, "protocol": "TCP", "port": 22,
             "bind_address": "*", "exposure": "ALL_INTERFACES"},
        ]}
        # Seed the device baseline with a REAL already-known device first
        # (an empty baseline is treated as "never scanned", not "zero
        # devices" -- see run_scan's own docstring) -- the correlation
        # needs a genuine new-device finding on the SECOND scan.
        run_scan(
            state_fn=lambda: _state(listening_ports=_NO_PORTS, arp_neighbors={"available": True, "neighbors": [_NEIGHBOR_A]}),
            store=store, now=lambda: 1000.0, write_alerts=False,
        )
        result = run_scan(
            state_fn=lambda: _state(
                listening_ports=ports,
                arp_neighbors={"available": True, "neighbors": [_NEIGHBOR_A, _NEIGHBOR_B]},
            ),
            store=store, now=lambda: 2000.0,
        )
        correlations = [f for f in result.new_findings if f.kind == "correlated_new_device_and_exposed_port"]
        assert len(correlations) == 1
        assert correlations[0].severity == "high"
        # The real, separate underlying findings are still reported too.
        assert any(f.kind == "new_device" for f in result.new_findings)
        assert any(f.kind == "exposed_port" for f in result.new_findings)
        # A real alert was written for the correlation (HIGH severity).
        assert any("same scan" in w["title"] for w in written)

    def test_a_lone_new_device_never_correlates(self, tmp_path):
        store = SentryStore(tmp_path / "s.db")
        run_scan(
            state_fn=lambda: _state(arp_neighbors={"available": True, "neighbors": []}),
            store=store, now=lambda: 1000.0, write_alerts=False,
        )
        result = run_scan(
            state_fn=lambda: _state(arp_neighbors={"available": True, "neighbors": [_NEIGHBOR_A]}),
            store=store, now=lambda: 2000.0, write_alerts=False,
        )
        assert not any(f.kind == "correlated_new_device_and_exposed_port" for f in result.new_findings)

    def test_telemetry_availability_is_reported_honestly(self, tmp_path):
        state = _state()
        state["dns"] = {"available": False, "reason": "scutil not found"}
        result = run_scan(
            state_fn=lambda: state, store=SentryStore(tmp_path / "s.db"), now=lambda: 1000.0,
        )
        assert result.telemetry_available["firewall"] is True
        assert result.telemetry_available.get("dns", True) is False


class TestSentryRuntimeEnabled:
    def test_default_on(self, monkeypatch):
        monkeypatch.delenv("DOURMOUSE_SECURITY_SENTRY_LOOP", raising=False)
        assert sentry_runtime_enabled() is True

    def test_explicit_off(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_SECURITY_SENTRY_LOOP", "0")
        assert sentry_runtime_enabled() is False

    def test_any_other_value_is_on(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_SECURITY_SENTRY_LOOP", "1")
        assert sentry_runtime_enabled() is True


class TestSentryRuntime:
    def test_interval_is_never_faster_than_one_minute(self, tmp_path):
        rt = SentryRuntime(interval_seconds=1.0, store=SentryStore(tmp_path / "s.db"))
        assert rt._interval == 60.0

    def test_requested_interval_above_the_floor_is_kept(self, tmp_path):
        rt = SentryRuntime(interval_seconds=300.0, store=SentryStore(tmp_path / "s.db"))
        assert rt._interval == 300.0

    def test_run_one_tick_now_is_real_and_synchronous(self, tmp_path):
        rt = SentryRuntime(store=SentryStore(tmp_path / "s.db"))
        assert rt.last_result is None
        assert rt.tick_count == 0
        result = rt.run_one_tick_now()
        assert rt.last_result is result
        assert rt.last_scan_at is not None
        assert rt.tick_count == 1
        # A real result, same shape run_scan itself returns.
        assert hasattr(result, "risk_score")

    def test_start_runs_at_least_one_real_tick_before_the_first_wait(self, tmp_path):
        """The loop calls run_one_tick_now() BEFORE its first
        threading.Event.wait(), so even a long real interval does not
        delay the first real scan -- a genuinely "always running" sentry
        must not wait 5 minutes to say anything for the first time."""
        rt = SentryRuntime(store=SentryStore(tmp_path / "s.db"))
        rt.start()
        try:
            deadline = time.time() + 5.0
            while rt.tick_count == 0 and time.time() < deadline:
                time.sleep(0.05)
            assert rt.tick_count >= 1
            assert rt.last_result is not None
        finally:
            rt.stop()

    def test_start_is_idempotent(self, tmp_path):
        rt = SentryRuntime(store=SentryStore(tmp_path / "s.db"))
        rt.start()
        try:
            first_thread = rt._thread
            rt.start()  # a second call must not spawn a second thread
            assert rt._thread is first_thread
        finally:
            rt.stop()

    def test_stop_lets_the_daemon_thread_exit_promptly(self, tmp_path):
        rt = SentryRuntime(store=SentryStore(tmp_path / "s.db"))
        rt.start()
        thread = rt._thread
        rt.stop()
        thread.join(timeout=5.0)
        assert not thread.is_alive()

    def test_a_broken_tick_never_kills_the_runtime(self, tmp_path, monkeypatch):
        """Same discipline as GoalRuntime._loop: a bug in one tick must
        never stop future ticks from happening."""
        rt = SentryRuntime(store=SentryStore(tmp_path / "s.db"))
        calls = []

        def _flaky_tick():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("simulated real scan failure")
            rt.tick_count += 1
            rt.last_scan_at = time.time()

        monkeypatch.setattr(rt, "run_one_tick_now", _flaky_tick)
        rt._interval = 0.05  # bypass the 60s floor for this one test only, post-construction
        rt.start()
        try:
            deadline = time.time() + 5.0
            while len(calls) < 2 and time.time() < deadline:
                time.sleep(0.05)
            assert len(calls) >= 2  # the loop survived the first tick's exception
        finally:
            rt.stop()
