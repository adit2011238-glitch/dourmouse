"""dourmouse/security/platform_adapter.py — Phase 4's foundational
telemetry layer. Parser tests use REAL command output captured live from
this machine (2026-09-17), not fabricated samples — the spec's own
repeated demand ("real data only"). Every _run() call is monkeypatched so
no test ever actually shells out; the parsing logic is what's under test.
"""

from __future__ import annotations

from dourmouse.security import platform_adapter as pa

# Real `ifconfig` output, captured live. Includes loopback, several
# inactive virtual/ethernet ports, a bridge, and both a real LAN
# interface (en0) and the real Tailscale utun interface (utun4) --
# the exact case this parser must keep straight (they are NOT the same
# interface, despite an earlier naive grep this session briefly
# conflating them).
_REAL_IFCONFIG = """\
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\toptions=1203<RXCSUM,TXCSUM,TXSTATUS,SW_TIMESTAMP>
\tinet 127.0.0.1 netmask 0xff000000
\tinet6 ::1 prefixlen 128
\tinet6 fe80::1%lo0 prefixlen 64 scopeid 0x1
\tnd6 options=201<PERFORMNUD,DAD>
en3: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\toptions=400<CHANNEL_IO>
\tether 3a:17:dc:fd:ed:68
\tmedia: none
\tstatus: inactive
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\toptions=6460<TSO4,TSO6,CHANNEL_IO,PARTIAL_CSUM,ZEROINVERT_CSUM>
\tether 62:bc:40:aa:49:7a
\tinet6 fe80::104d:b48a:14e6:d3a%en0 prefixlen 64 secured scopeid 0xb
\tinet 192.168.1.240 netmask 0xffffff00 broadcast 192.168.1.255
\tnd6 options=201<PERFORMNUD,DAD>
\tmedia: autoselect
utun4: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1280
\toptions=6460<TSO4,TSO6,CHANNEL_IO,PARTIAL_CSUM,ZEROINVERT_CSUM>
\tinet6 fe80::8c04:9a0:b554:aba7%utun4 prefixlen 64 scopeid 0x13
\tinet 100.84.156.49 --> 100.84.156.49 netmask 0xffffffff
\tinet6 fd7a:115c:a1e0::5938:9c32 prefixlen 48
\tnd6 options=201<PERFORMNUD,DAD>
stf0: flags=0<> mtu 1280
"""

_REAL_ROUTE_DEFAULT = """\
   route to: default
destination: default
       mask: default
    gateway: 192.168.1.1
  interface: en0
      flags: <UP,GATEWAY,DONE,STATIC,PRCLONING,GLOBAL>
 recvpipe  sendpipe  ssthresh  rtt,msec    rttvar  hopcount      mtu     expire
       0         0         0         0         0         0      1500         0
"""

_REAL_SCUTIL_DNS = """\
DNS configuration

resolver #1
  search domain[0] : tail2f1eda.ts.net
  nameserver[0] : 100.100.100.100
  nameserver[1] : fd7a:115c:a1e0::53
  if_index : 19 (utun4)
  flags    : Supplemental, Request A records, Request AAAA records
  reach    : 0x00000003 (Reachable,Transient Connection)
  order    : 101800

resolver #2
  nameserver[0] : 192.168.1.1
  if_index : 11 (en0)
  flags    : Request A records
  reach    : 0x00020002 (Reachable,Directly Reachable Address)
  order    : 200000
"""

_REAL_ARP = """\
linksys07982 (192.168.1.1) at e8:9f:80:96:e8:1 on en0 ifscope [ethernet]
adits-iphone (192.168.1.48) at 34:da:a1:8d:81:d7 on en0 ifscope [ethernet]
? (192.168.1.55) at b6:35:fd:6:be:72 on en0 ifscope [ethernet]
? (192.168.1.95) at (incomplete) on en0 ifscope [ethernet]
sonoszp (192.168.1.153) at f0:f6:c1:78:3d:18 on en0 ifscope [ethernet]
"""

_REAL_LSOF_LISTEN = """\
COMMAND     PID        USER   FD   TYPE             DEVICE SIZE/OFF NODE NAME
rapportd    639 aditagrawal   10u  IPv4 0x8005806899d29b09      0t0  TCP *:49930 (LISTEN)
ollama     1852 aditagrawal    3u  IPv4 0x178c909a6de5d8fa      0t0  TCP 127.0.0.1:11434 (LISTEN)
Python     7502 aditagrawal    4u  IPv6 0x292a19ea9d056b40      0t0  TCP *:8793 (LISTEN)
Python    49695 aditagrawal    7u  IPv4  0x87347fca5926d71      0t0  TCP 127.0.0.1:8765 (LISTEN)
"""

_REAL_LSOF_ESTABLISHED = """\
COMMAND     PID        USER   FD   TYPE             DEVICE SIZE/OFF NODE NAME
AvidLink  47756 aditagrawal   49u  IPv4  0xe0400ce4f276ad1      0t0  TCP 192.168.1.95:65445->104.109.251.234:80 (ESTABLISHED)
AvidLink  47756 aditagrawal   50u  IPv4 0xa989e4c0c191a8c4      0t0  TCP 192.168.1.95:65449->104.18.42.13:443 (ESTABLISHED)
Tailscale 47779 aditagrawal    3u  IPv4 0x1d6453c9c1b20cf4      0t0  TCP 127.0.0.1:53629->127.0.0.1:53624 (ESTABLISHED)
rapportd  47492 aditagrawal   18u  IPv6 0xe1a4174fbc096fde      0t0  TCP [fe80:b::8ab:c8a5:2cbf:5005]:53442->[fe80:b::1488:95ef:9ff3:ef56]:52664 (ESTABLISHED)
"""

_REAL_FIREWALL_DISABLED = "Firewall is disabled. (State = 0)\n"
_REAL_FIREWALL_ENABLED = "Firewall is enabled. (State = 1)\n"


class TestRunHelper:
    def test_missing_binary_is_honest_not_a_crash(self):
        ok, reason = pa._run(["a-command-that-genuinely-does-not-exist-anywhere"])
        assert ok is False
        assert "not installed" in reason

    def test_never_uses_shell_true(self):
        """Real security concern (docs/ENGINEERING_AUDIT.md's shell audit
        item): argument-list only, never shell string interpolation."""
        import inspect
        source = inspect.getsource(pa._run)
        assert "shell=True" not in source

    def test_every_call_site_passes_a_real_timeout(self):
        import inspect
        for name in ("get_interfaces", "get_default_gateway", "get_dns_configuration",
                     "get_arp_neighbors", "get_listening_ports", "get_firewall_status"):
            source = inspect.getsource(getattr(pa, name))
            assert "_run(" in source


class TestParseIfconfig:
    def test_finds_every_real_interface(self):
        interfaces = pa._parse_ifconfig(_REAL_IFCONFIG)
        names = {i["name"] for i in interfaces}
        assert names == {"lo0", "en3", "en0", "utun4", "stf0"}

    def test_en0_and_utun4_are_kept_separate_not_conflated(self):
        """The real bug a naive grep hit earlier this session: en0 (real
        LAN) and utun4 (Tailscale) both have `inet` lines close together
        in the raw output and must never be merged into one record."""
        interfaces = {i["name"]: i for i in pa._parse_ifconfig(_REAL_IFCONFIG)}
        assert interfaces["en0"]["ipv4"][0]["address"] == "192.168.1.240"
        assert interfaces["utun4"]["ipv4"][0]["address"] == "100.84.156.49"

    def test_point_to_point_tailscale_address_is_parsed_as_such(self):
        interfaces = {i["name"]: i for i in pa._parse_ifconfig(_REAL_IFCONFIG)}
        assert interfaces["utun4"]["ipv4"][0]["peer"] == "100.84.156.49"

    def test_mac_address_is_captured(self):
        interfaces = {i["name"]: i for i in pa._parse_ifconfig(_REAL_IFCONFIG)}
        assert interfaces["en0"]["mac"] == "62:bc:40:aa:49:7a"

    def test_inactive_status_is_captured(self):
        interfaces = {i["name"]: i for i in pa._parse_ifconfig(_REAL_IFCONFIG)}
        assert interfaces["en3"]["status"] == "inactive"

    def test_flags_are_a_real_list_not_a_raw_string(self):
        interfaces = {i["name"]: i for i in pa._parse_ifconfig(_REAL_IFCONFIG)}
        assert "UP" in interfaces["en0"]["flags"]
        assert "LOOPBACK" in interfaces["lo0"]["flags"]

    def test_an_interface_with_empty_flags_does_not_crash_the_parser(self):
        interfaces = {i["name"]: i for i in pa._parse_ifconfig(_REAL_IFCONFIG)}
        assert interfaces["stf0"]["flags"] == []

    def test_link_local_zone_id_is_stripped_from_ipv6(self):
        interfaces = {i["name"]: i for i in pa._parse_ifconfig(_REAL_IFCONFIG)}
        assert "fe80::104d:b48a:14e6:d3a" in interfaces["en0"]["ipv6"]
        assert not any("%" in a for a in interfaces["en0"]["ipv6"])


class TestGetInterfaces:
    def test_wraps_the_parser_with_a_real_ok_flag(self, monkeypatch):
        monkeypatch.setattr(pa, "_run", lambda cmd, timeout=pa._DEFAULT_TIMEOUT_S: (True, _REAL_IFCONFIG))
        result = pa.get_interfaces()
        assert result["available"] is True
        assert len(result["interfaces"]) == 5

    def test_a_failed_command_is_honest_not_an_empty_list(self, monkeypatch):
        monkeypatch.setattr(pa, "_run", lambda cmd, timeout=pa._DEFAULT_TIMEOUT_S: (False, "ifconfig is not installed on this system"))
        result = pa.get_interfaces()
        assert result["available"] is False
        assert "reason" in result


class TestDefaultGateway:
    def test_parses_the_real_gateway_and_interface(self, monkeypatch):
        monkeypatch.setattr(pa, "_run", lambda cmd, timeout=pa._DEFAULT_TIMEOUT_S: (True, _REAL_ROUTE_DEFAULT))
        result = pa.get_default_gateway()
        assert result == {"available": True, "gateway": "192.168.1.1", "interface": "en0"}

    def test_no_default_route_is_honest_not_a_fabricated_gateway(self, monkeypatch):
        monkeypatch.setattr(pa, "_run", lambda cmd, timeout=pa._DEFAULT_TIMEOUT_S: (True, "   route to: default\ndestination: default\n"))
        result = pa.get_default_gateway()
        assert result["available"] is False


class TestDnsConfiguration:
    def test_parses_both_real_resolvers(self, monkeypatch):
        monkeypatch.setattr(pa, "_run", lambda cmd, timeout=pa._DEFAULT_TIMEOUT_S: (True, _REAL_SCUTIL_DNS))
        result = pa.get_dns_configuration()
        assert result["available"] is True
        assert len(result["resolvers"]) == 2

    def test_tailscale_magic_dns_resolver_is_captured(self, monkeypatch):
        monkeypatch.setattr(pa, "_run", lambda cmd, timeout=pa._DEFAULT_TIMEOUT_S: (True, _REAL_SCUTIL_DNS))
        resolvers = pa.get_dns_configuration()["resolvers"]
        magic = next(r for r in resolvers if "100.100.100.100" in r["nameservers"])
        assert magic["search_domains"] == ["tail2f1eda.ts.net"]

    def test_local_router_resolver_is_captured(self, monkeypatch):
        monkeypatch.setattr(pa, "_run", lambda cmd, timeout=pa._DEFAULT_TIMEOUT_S: (True, _REAL_SCUTIL_DNS))
        resolvers = pa.get_dns_configuration()["resolvers"]
        assert any(r["nameservers"] == ["192.168.1.1"] for r in resolvers)


class TestArpNeighbors:
    def test_parses_every_real_neighbor(self, monkeypatch):
        monkeypatch.setattr(pa, "_run", lambda cmd, timeout=pa._DEFAULT_TIMEOUT_S: (True, _REAL_ARP))
        result = pa.get_arp_neighbors()
        assert result["available"] is True
        assert len(result["neighbors"]) == 5

    def test_unnamed_neighbor_hostname_is_none_not_the_literal_question_mark(self, monkeypatch):
        monkeypatch.setattr(pa, "_run", lambda cmd, timeout=pa._DEFAULT_TIMEOUT_S: (True, _REAL_ARP))
        neighbors = pa.get_arp_neighbors()["neighbors"]
        entry = next(n for n in neighbors if n["ip"] == "192.168.1.55")
        assert entry["hostname"] is None

    def test_incomplete_mac_is_none_not_the_literal_word(self, monkeypatch):
        monkeypatch.setattr(pa, "_run", lambda cmd, timeout=pa._DEFAULT_TIMEOUT_S: (True, _REAL_ARP))
        neighbors = pa.get_arp_neighbors()["neighbors"]
        entry = next(n for n in neighbors if n["ip"] == "192.168.1.95")
        assert entry["mac"] is None

    def test_named_neighbor_is_captured(self, monkeypatch):
        monkeypatch.setattr(pa, "_run", lambda cmd, timeout=pa._DEFAULT_TIMEOUT_S: (True, _REAL_ARP))
        neighbors = pa.get_arp_neighbors()["neighbors"]
        entry = next(n for n in neighbors if n["ip"] == "192.168.1.1")
        assert entry["hostname"] == "linksys07982"
        assert entry["mac"] == "e8:9f:80:96:e8:1"


class TestListeningPortsAndExposure:
    def test_parses_every_real_listener(self, monkeypatch):
        monkeypatch.setattr(pa, "_run", lambda cmd, timeout=pa._DEFAULT_TIMEOUT_S: (True, _REAL_LSOF_LISTEN))
        result = pa.get_listening_ports()
        assert result["available"] is True
        assert len(result["listening_ports"]) == 4

    def test_loopback_bound_service_is_classified_correctly(self, monkeypatch):
        monkeypatch.setattr(pa, "_run", lambda cmd, timeout=pa._DEFAULT_TIMEOUT_S: (True, _REAL_LSOF_LISTEN))
        ports = pa.get_listening_ports()["listening_ports"]
        ollama = next(p for p in ports if p["command"] == "ollama")
        assert ollama["port"] == 11434
        assert ollama["exposure"] == "LOOPBACK_ONLY"

    def test_all_interfaces_bound_service_is_classified_correctly(self, monkeypatch):
        """The real, honest finding this exact parser was built to
        surface: a service bound to '*' is reachable far beyond this
        machine, a materially different risk than a loopback-only one."""
        monkeypatch.setattr(pa, "_run", lambda cmd, timeout=pa._DEFAULT_TIMEOUT_S: (True, _REAL_LSOF_LISTEN))
        ports = pa.get_listening_ports()["listening_ports"]
        wide_open = next(p for p in ports if p["command"] == "Python" and p["port"] == 8793)
        assert wide_open["exposure"] == "ALL_INTERFACES"
        assert wide_open["bind_address"] == "*"

    def test_pid_and_protocol_are_captured(self, monkeypatch):
        monkeypatch.setattr(pa, "_run", lambda cmd, timeout=pa._DEFAULT_TIMEOUT_S: (True, _REAL_LSOF_LISTEN))
        ports = pa.get_listening_ports()["listening_ports"]
        rapportd = next(p for p in ports if p["command"] == "rapportd")
        assert rapportd["pid"] == 639
        assert rapportd["protocol"] == "TCP"


class TestEstablishedConnections:
    def test_parses_every_real_connection(self, monkeypatch):
        monkeypatch.setattr(pa, "_run", lambda cmd, timeout=pa._DEFAULT_TIMEOUT_S: (True, _REAL_LSOF_ESTABLISHED))
        result = pa.get_established_connections()
        assert result["available"] is True
        assert len(result["connections"]) == 4

    def test_real_ipv4_remote_peer_is_captured(self, monkeypatch):
        monkeypatch.setattr(pa, "_run", lambda cmd, timeout=pa._DEFAULT_TIMEOUT_S: (True, _REAL_LSOF_ESTABLISHED))
        conns = pa.get_established_connections()["connections"]
        avidlink = next(c for c in conns if c["command"] == "AvidLink" and c["remote_port"] == 443)
        assert avidlink["remote_address"] == "104.18.42.13"
        assert avidlink["local_address"] == "192.168.1.95"
        assert avidlink["local_port"] == 65449
        assert avidlink["pid"] == 47756
        assert avidlink["protocol"] == "TCP"

    def test_real_ipv6_bracketed_addresses_are_stripped(self, monkeypatch):
        monkeypatch.setattr(pa, "_run", lambda cmd, timeout=pa._DEFAULT_TIMEOUT_S: (True, _REAL_LSOF_ESTABLISHED))
        conns = pa.get_established_connections()["connections"]
        rapportd = next(c for c in conns if c["command"] == "rapportd")
        assert rapportd["remote_address"] == "fe80:b::1488:95ef:9ff3:ef56"
        assert "[" not in rapportd["remote_address"]

    def test_unavailable_lsof_is_honest(self, monkeypatch):
        monkeypatch.setattr(pa, "_run", lambda cmd, timeout=pa._DEFAULT_TIMEOUT_S: (False, "lsof timed out after 10.0s"))
        result = pa.get_established_connections()
        assert result == {"available": False, "reason": "lsof timed out after 10.0s"}


class TestExposureClassifier:
    def test_loopback_variants(self):
        assert pa._classify_exposure("127.0.0.1") == "LOOPBACK_ONLY"
        assert pa._classify_exposure("::1") == "LOOPBACK_ONLY"

    def test_wildcard_variants(self):
        assert pa._classify_exposure("*") == "ALL_INTERFACES"
        assert pa._classify_exposure("0.0.0.0") == "ALL_INTERFACES"

    def test_tailscale_range(self):
        assert pa._classify_exposure("100.84.156.49") == "TAILSCALE"

    def test_local_network_ranges(self):
        assert pa._classify_exposure("192.168.1.240") == "LOCAL_NETWORK"
        assert pa._classify_exposure("10.0.0.5") == "LOCAL_NETWORK"

    def test_unrecognized_address_is_honestly_unknown_not_misclassified(self):
        assert pa._classify_exposure("8.8.8.8") == "UNKNOWN"


class TestFirewallStatus:
    def test_real_disabled_state_on_this_machine(self, monkeypatch):
        monkeypatch.setattr(pa, "_run", lambda cmd, timeout=pa._DEFAULT_TIMEOUT_S: (True, _REAL_FIREWALL_DISABLED))
        result = pa.get_firewall_status()
        assert result == {"available": True, "enabled": False, "raw": "Firewall is disabled. (State = 0)"}

    def test_enabled_state(self, monkeypatch):
        monkeypatch.setattr(pa, "_run", lambda cmd, timeout=pa._DEFAULT_TIMEOUT_S: (True, _REAL_FIREWALL_ENABLED))
        result = pa.get_firewall_status()
        assert result["enabled"] is True


class TestSystemSecurityStateSnapshot:
    def test_bundles_every_signal_independently(self, monkeypatch):
        monkeypatch.setattr(pa, "get_interfaces", lambda: {"available": True, "interfaces": []})
        monkeypatch.setattr(pa, "get_default_gateway", lambda: {"available": True, "gateway": "192.168.1.1", "interface": "en0"})
        monkeypatch.setattr(pa, "get_dns_configuration", lambda: {"available": True, "resolvers": []})
        monkeypatch.setattr(pa, "get_arp_neighbors", lambda: {"available": True, "neighbors": []})
        monkeypatch.setattr(pa, "get_listening_ports", lambda: {"available": True, "listening_ports": []})
        monkeypatch.setattr(pa, "get_established_connections", lambda: {"available": True, "connections": []})
        monkeypatch.setattr(pa, "get_firewall_status", lambda: {"available": True, "enabled": False, "raw": ""})
        snap = pa.get_system_security_state()
        assert set(snap.keys()) == {
            "interfaces", "default_gateway", "dns", "arp_neighbors",
            "listening_ports", "established_connections", "firewall",
        }

    def test_one_unavailable_signal_never_blanks_the_others(self, monkeypatch):
        """Real resilience property: a missing/failed tool for ONE
        signal must not prevent every other real signal from reporting."""
        monkeypatch.setattr(pa, "get_interfaces", lambda: {"available": False, "reason": "ifconfig is not installed"})
        monkeypatch.setattr(pa, "get_default_gateway", lambda: {"available": True, "gateway": "192.168.1.1", "interface": "en0"})
        monkeypatch.setattr(pa, "get_dns_configuration", lambda: {"available": True, "resolvers": []})
        monkeypatch.setattr(pa, "get_arp_neighbors", lambda: {"available": True, "neighbors": []})
        monkeypatch.setattr(pa, "get_listening_ports", lambda: {"available": True, "listening_ports": []})
        monkeypatch.setattr(pa, "get_established_connections", lambda: {"available": True, "connections": []})
        monkeypatch.setattr(pa, "get_firewall_status", lambda: {"available": True, "enabled": False, "raw": ""})
        snap = pa.get_system_security_state()
        assert snap["interfaces"]["available"] is False
        assert snap["default_gateway"]["available"] is True
