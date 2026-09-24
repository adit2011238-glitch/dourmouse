"""The ``security`` subagent's tools (dourmouse/security/tools.py).
Every platform_adapter call is monkeypatched — these tests are about the
tool layer's formatting and filtering, not the parsers (see
test_security_platform_adapter.py for those, against real captured data).
"""

from __future__ import annotations

from dourmouse.security import platform_adapter as pa
from dourmouse.security import tools as sec_tools
from dourmouse.security.sentry import SentryFinding, SentryStore


def _tool(name: str):
    subagent = sec_tools.build_security_subagent()
    for tool in subagent.tools:
        if tool.name == name:
            return tool
    raise AssertionError(f"no tool named {name!r} on the security subagent")


_FAKE_SNAPSHOT = {
    "interfaces": {"available": True, "interfaces": [
        {"name": "en0", "flags": ["UP", "BROADCAST"], "ipv4": [{"address": "192.168.1.240"}], "ipv6": [], "status": None, "mac": "aa:bb", "mtu": 1500},
        {"name": "en3", "flags": [], "ipv4": [], "ipv6": [], "status": "inactive", "mac": "cc:dd", "mtu": 1500},
    ]},
    "default_gateway": {"available": True, "gateway": "192.168.1.1", "interface": "en0"},
    "dns": {"available": True, "resolvers": [{"nameservers": ["192.168.1.1"], "search_domains": []}]},
    "arp_neighbors": {"available": True, "neighbors": [{"hostname": "router", "ip": "192.168.1.1", "mac": "e8:9f", "interface": "en0"}]},
    "listening_ports": {"available": True, "listening_ports": [
        {"command": "ollama", "pid": 1852, "protocol": "TCP", "bind_address": "127.0.0.1", "port": 11434, "exposure": "LOOPBACK_ONLY"},
        {"command": "Python", "pid": 7502, "protocol": "TCP", "bind_address": "*", "port": 8793, "exposure": "ALL_INTERFACES"},
    ]},
    "firewall": {"available": True, "enabled": False, "raw": "Firewall is disabled. (State = 0)"},
}


class TestBuildSecuritySubagent:
    def test_registers_all_tools(self):
        subagent = sec_tools.build_security_subagent()
        assert {t.name for t in subagent.tools} == {
            "security_status", "list_exposed_services",
            "security_sentry_scan", "security_external_peers", "security_check_reputation",
            "security_known_devices", "security_sentry_dismiss", "security_downloads", "security_monitoring_check",
            "security_incident_open", "security_incident_update", "security_incidents",
        }

    def test_no_tool_requires_confirmation(self):
        from dourmouse.dispatch import Permission

        subagent = sec_tools.build_security_subagent()
        assert all(t.permission == Permission.REGULAR for t in subagent.tools)


class TestSecurityStatus:
    def test_summarizes_every_real_signal(self, monkeypatch):
        monkeypatch.setattr(pa, "get_system_security_state", lambda: _FAKE_SNAPSHOT)
        result = _tool("security_status").handler({})
        assert "en0: 192.168.1.240" in result
        assert "en3" not in result  # inactive/down interface is filtered from the summary
        assert "Default gateway: 192.168.1.1 via en0" in result
        assert "DNS: 1 resolver(s), nameservers: 192.168.1.1" in result
        assert "Application Firewall: disabled" in result
        assert "1 known neighbor(s)" in result

    def test_flags_a_service_reachable_beyond_this_machine(self, monkeypatch):
        monkeypatch.setattr(pa, "get_system_security_state", lambda: _FAKE_SNAPSHOT)
        result = _tool("security_status").handler({})
        assert "1 reachable beyond this machine" in result
        assert "Python (pid 7502) on *:8793 — ALL_INTERFACES" in result
        assert "ollama" not in result.split("reachable beyond this machine")[1]

    def test_an_unavailable_signal_is_reported_honestly_not_omitted(self, monkeypatch):
        broken = dict(_FAKE_SNAPSHOT, firewall={"available": False, "reason": "socketfilterfw is not installed on this system"})
        monkeypatch.setattr(pa, "get_system_security_state", lambda: broken)
        result = _tool("security_status").handler({})
        assert "Firewall: unavailable (socketfilterfw is not installed on this system)" in result


class TestListExposedServices:
    def test_lists_every_service_with_no_filter(self, monkeypatch):
        monkeypatch.setattr(pa, "get_listening_ports", lambda: _FAKE_SNAPSHOT["listening_ports"])
        result = _tool("list_exposed_services").handler({})
        assert "2 listening service(s)" in result
        assert "ollama" in result and "Python" in result

    def test_filters_by_exposure(self, monkeypatch):
        monkeypatch.setattr(pa, "get_listening_ports", lambda: _FAKE_SNAPSHOT["listening_ports"])
        result = _tool("list_exposed_services").handler({"exposure": "all_interfaces"})
        assert "1 listening service(s)" in result
        assert "Python" in result
        assert "ollama" not in result

    def test_no_matches_is_an_honest_empty_message(self, monkeypatch):
        monkeypatch.setattr(pa, "get_listening_ports", lambda: _FAKE_SNAPSHOT["listening_ports"])
        result = _tool("list_exposed_services").handler({"exposure": "tailscale"})
        assert result == "No listening services with exposure TAILSCALE."

    def test_unavailable_is_a_clean_error(self, monkeypatch):
        monkeypatch.setattr(pa, "get_listening_ports", lambda: {"available": False, "reason": "lsof timed out after 10.0s"})
        result = _tool("list_exposed_services").handler({})
        assert result == "ERROR: could not read listening ports: lsof timed out after 10.0s"


_FAKE_ESTABLISHED = {"available": True, "connections": [
    {"command": "AvidLink", "pid": 1, "protocol": "TCP", "local_address": "192.168.1.95",
     "local_port": 1, "remote_address": "104.18.42.13", "remote_port": 443},
    {"command": "Tailscale", "pid": 2, "protocol": "TCP", "local_address": "127.0.0.1",
     "local_port": 2, "remote_address": "127.0.0.1", "remote_port": 3},
    {"command": "Router", "pid": 3, "protocol": "TCP", "local_address": "192.168.1.95",
     "local_port": 4, "remote_address": "192.168.1.1", "remote_port": 5},
]}


class TestSecurityExternalPeers:
    def test_lists_only_real_public_peers(self, monkeypatch):
        monkeypatch.setattr(pa, "get_established_connections", lambda: _FAKE_ESTABLISHED)
        result = _tool("security_external_peers").handler({})
        assert "1 real external peer(s)" in result
        assert "104.18.42.13" in result
        assert "AvidLink" in result
        assert "127.0.0.1" not in result
        assert "192.168.1.1" not in result

    def test_zero_real_external_peers_is_an_honest_message(self, monkeypatch):
        monkeypatch.setattr(pa, "get_established_connections", lambda: {"available": True, "connections": []})
        result = _tool("security_external_peers").handler({})
        assert result == "No real external (public-internet) peers currently connected."

    def test_unavailable_is_a_clean_error(self, monkeypatch):
        monkeypatch.setattr(pa, "get_established_connections", lambda: {"available": False, "reason": "lsof timed out"})
        result = _tool("security_external_peers").handler({})
        assert result == "ERROR: could not read established connections: lsof timed out"


class TestSecurityCheckReputation:
    def test_empty_ip_is_an_honest_error(self):
        assert "ERROR" in _tool("security_check_reputation").handler({"ip": "  "})

    def test_not_configured_is_reported_honestly(self, monkeypatch):
        from dourmouse.security import reputation as rep

        monkeypatch.delenv(rep.REPUTATION_API_KEY_ENV, raising=False)
        result = _tool("security_check_reputation").handler({"ip": "8.8.8.8"})
        assert "ERROR" in result and "NOT CONFIGURED" in result

    def test_a_real_result_is_formatted(self, monkeypatch):
        from dourmouse.security import reputation as rep

        monkeypatch.setattr(rep, "check_ip_reputation", lambda ip, timeout=10.0: {
            "available": True, "ip": ip, "abuse_confidence_score": 5, "total_reports": 2,
            "country_code": "US", "isp": "Example ISP", "is_tor": False,
        })
        result = _tool("security_check_reputation").handler({"ip": "8.8.8.8"})
        assert "abuse confidence 5/100" in result
        assert "Example ISP" in result


_FAKE_FINDING = SentryFinding(
    fingerprint="fp1", kind="test_finding", severity="med",
    title="a real test finding", detail="detail", recommended_action="do something",
)


class TestSecurityIncidentTools:
    def _seeded_db(self, tmp_path, monkeypatch):
        db = tmp_path / "sentry.db"
        monkeypatch.setattr(sec_tools, "_sentry_db", lambda: db)
        SentryStore(db).record_and_classify(_FAKE_FINDING, now=1000.0)
        return db

    def test_opening_an_incident_for_an_unknown_fingerprint_is_honest(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sec_tools, "_sentry_db", lambda: tmp_path / "sentry.db")
        result = _tool("security_incident_open").handler({"fingerprint": "never-seen"})
        assert "ERROR" in result

    def test_opening_a_real_incident_succeeds(self, tmp_path, monkeypatch):
        self._seeded_db(tmp_path, monkeypatch)
        result = _tool("security_incident_open").handler({"fingerprint": "fp1", "note": "looking into it"})
        assert "opened" in result.lower()

    def test_empty_fingerprint_is_an_honest_error(self, tmp_path, monkeypatch):
        self._seeded_db(tmp_path, monkeypatch)
        assert "ERROR" in _tool("security_incident_open").handler({"fingerprint": "  "})

    def test_updating_a_real_incident_reports_the_new_status(self, tmp_path, monkeypatch):
        self._seeded_db(tmp_path, monkeypatch)
        _tool("security_incident_open").handler({"fingerprint": "fp1"})
        result = _tool("security_incident_update").handler({"fingerprint": "fp1", "status": "investigating"})
        assert "INVESTIGATING" in result

    def test_updating_an_unknown_incident_is_honest(self, tmp_path, monkeypatch):
        self._seeded_db(tmp_path, monkeypatch)
        result = _tool("security_incident_update").handler({"fingerprint": "fp1", "status": "investigating"})
        assert "ERROR" in result

    def test_an_invalid_status_is_an_honest_error(self, tmp_path, monkeypatch):
        self._seeded_db(tmp_path, monkeypatch)
        _tool("security_incident_open").handler({"fingerprint": "fp1"})
        result = _tool("security_incident_update").handler({"fingerprint": "fp1", "status": "not-a-real-status"})
        assert "ERROR" in result

    def test_a_terminal_incident_refuses_to_reopen(self, tmp_path, monkeypatch):
        self._seeded_db(tmp_path, monkeypatch)
        _tool("security_incident_open").handler({"fingerprint": "fp1"})
        _tool("security_incident_update").handler({"fingerprint": "fp1", "status": "resolved"})
        result = _tool("security_incident_update").handler({"fingerprint": "fp1", "status": "open"})
        assert "ERROR" in result and "closed" in result

    def test_listing_incidents_reports_real_counts(self, tmp_path, monkeypatch):
        self._seeded_db(tmp_path, monkeypatch)
        assert "No real incidents" in _tool("security_incidents").handler({})
        _tool("security_incident_open").handler({"fingerprint": "fp1"})
        result = _tool("security_incidents").handler({})
        assert "1 real incident(s)" in result
        assert "fp1" in result

    def test_listing_incidents_filters_by_status(self, tmp_path, monkeypatch):
        self._seeded_db(tmp_path, monkeypatch)
        _tool("security_incident_open").handler({"fingerprint": "fp1"})
        assert "No real incidents" in _tool("security_incidents").handler({"status": "resolved"})
        _tool("security_incident_update").handler({"fingerprint": "fp1", "status": "resolved"})
        result = _tool("security_incidents").handler({"status": "resolved"})
        assert "1 real incident(s)" in result


class TestSecurityKnownDevices:
    def test_empty_baseline_is_an_honest_message(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sec_tools, "_sentry_db", lambda: tmp_path / "sentry.db")
        result = _tool("security_known_devices").handler({})
        assert "No real device baseline" in result

    def test_a_real_baseline_is_listed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sec_tools, "_sentry_db", lambda: tmp_path / "sentry.db")
        SentryStore(tmp_path / "sentry.db").record_devices(
            [{"hostname": "router", "ip": "192.168.1.1", "mac": "e8:9f", "interface": "en0"}],
            now=1000.0,
        )
        result = _tool("security_known_devices").handler({})
        assert "1 real known device(s)" in result
        assert "router" in result and "e8:9f" in result and "192.168.1.1" in result
