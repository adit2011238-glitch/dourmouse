"""dourmouse/security/sentry.py -- the AI security sentry's real
deterministic detection, scoring, and false-positive memory. No model
call anywhere in this module (a deliberate correction to this domain's
own build plan, see the module's own docstring); every test here is a
plain, hermetic, real-store test -- no fakes needed."""

from __future__ import annotations

from dourmouse.security.sentry import (
    SentryFinding,
    SentryStore,
    _detect_findings,
    run_scan,
)

_FIREWALL_OFF = {"available": True, "enabled": False}
_FIREWALL_ON = {"available": True, "enabled": True}
_FIREWALL_UNAVAILABLE = {"available": False, "reason": "no such binary"}

_NO_PORTS = {"available": True, "listening_ports": []}


def _state(firewall=_FIREWALL_ON, listening_ports=_NO_PORTS):
    return {"firewall": firewall, "listening_ports": listening_ports}


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

    def test_telemetry_availability_is_reported_honestly(self, tmp_path):
        state = _state()
        state["dns"] = {"available": False, "reason": "scutil not found"}
        result = run_scan(
            state_fn=lambda: state, store=SentryStore(tmp_path / "s.db"), now=lambda: 1000.0,
        )
        assert result.telemetry_available["firewall"] is True
        assert result.telemetry_available.get("dns", True) is False
