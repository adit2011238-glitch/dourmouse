"""FS2 P3-60: exposure classification understands bracketed IPv6 and real network ranges."""

from __future__ import annotations

import pytest

from dourmouse.security import platform_adapter as pa

_CASES = [
    ("[::1]", "LOOPBACK_ONLY"),
    ("::1", "LOOPBACK_ONLY"),
    ("127.0.0.1", "LOOPBACK_ONLY"),
    ("127.0.0.2", "LOOPBACK_ONLY"),
    ("localhost", "LOOPBACK_ONLY"),
    ("[::]", "ALL_INTERFACES"),
    ("::", "ALL_INTERFACES"),
    ("*", "ALL_INTERFACES"),
    ("0.0.0.0", "ALL_INTERFACES"),
    ("[::ffff:127.0.0.1]", "LOOPBACK_ONLY"),
    ("100.84.156.49", "TAILSCALE"),
    ("100.64.0.1", "TAILSCALE"),
    ("100.127.255.254", "TAILSCALE"),
    ("100.200.1.1", "UNKNOWN"),
    ("100.63.0.1", "UNKNOWN"),
    ("[fd7a:115c:a1e0::1]", "TAILSCALE"),
    ("172.16.0.5", "LOCAL_NETWORK"),
    ("172.31.255.1", "LOCAL_NETWORK"),
    ("172.32.0.1", "UNKNOWN"),
    ("172.217.0.1", "UNKNOWN"),
    ("192.168.1.240", "LOCAL_NETWORK"),
    ("10.0.0.5", "LOCAL_NETWORK"),
    ("[fe80::1%en0]", "LOCAL_NETWORK"),
    ("[fd00::5]", "LOCAL_NETWORK"),
    ("8.8.8.8", "UNKNOWN"),
    ("not-an-address", "UNKNOWN"),
]


@pytest.mark.parametrize("addr,expected", _CASES)
def test_classify_exposure(addr, expected):
    assert pa._classify_exposure(addr) == expected


def test_lsof_line_with_bracketed_wildcard_is_all_interfaces(monkeypatch):
    out = ("COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n"
           "node 4242 me 20u IPv6 0x1 0t0 TCP [::]:9000 (LISTEN)\n"
           "ollama 99 me 5u IPv6 0x2 0t0 TCP [::1]:11434 (LISTEN)\n")
    monkeypatch.setattr(pa, "_run", lambda *a, **k: (True, out))
    ports = {p["command"]: p["exposure"] for p in pa.get_listening_ports()["listening_ports"]}
    assert ports == {"node": "ALL_INTERFACES", "ollama": "LOOPBACK_ONLY"}
