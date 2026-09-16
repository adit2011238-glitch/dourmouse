"""The ``security`` subagent's tools (dourmouse/security/tools.py).
Every platform_adapter call is monkeypatched — these tests are about the
tool layer's formatting and filtering, not the parsers (see
test_security_platform_adapter.py for those, against real captured data).
"""

from __future__ import annotations

from dourmouse.security import platform_adapter as pa
from dourmouse.security import tools as sec_tools


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
    def test_registers_both_tools(self):
        subagent = sec_tools.build_security_subagent()
        assert {t.name for t in subagent.tools} == {"security_status", "list_exposed_services"}

    def test_neither_tool_requires_confirmation(self):
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
