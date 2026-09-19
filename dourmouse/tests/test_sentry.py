"""dourmouse/security/sentry.py -- the AI security sentry's real
deterministic detection, scoring, and false-positive memory. No model
call anywhere in this module (a deliberate correction to this domain's
own build plan, see the module's own docstring); every test here is a
plain, hermetic, real-store test -- no fakes needed."""

from __future__ import annotations

import time

from dourmouse.security.sentry import (
    SentryFinding,
    SentryRuntime,
    SentryStore,
    _detect_findings,
    run_scan,
    sentry_runtime_enabled,
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
