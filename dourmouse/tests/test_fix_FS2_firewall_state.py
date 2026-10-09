"""FS2 P3-59: any non-zero socketfilterfw global state means the firewall is on."""

from __future__ import annotations

import pytest

from dourmouse.security import mac_telemetry as mt
from dourmouse.security import platform_adapter as pa

_CASES = [
    ("Firewall is enabled. (State = 1)", True),
    ("Firewall is disabled. (State = 0)", False),
    ("Firewall is set to block all incoming connections. (State = 2)", True),
    ("Block all incoming connections (State = 2)", True),
    ("Firewall is enabled.", True),
    ("Firewall is disabled.", False),
    ("", None),
    ("something unexpected", None),
]


@pytest.mark.parametrize("text,expected", _CASES)
def test_parse_firewall_state(text, expected):
    assert pa.parse_firewall_state(text) is expected


@pytest.mark.parametrize("text,expected", _CASES)
def test_host_protections_firewall(monkeypatch, text, expected):
    def fake(cmd, timeout=0):
        if "--getglobalstate" in cmd:
            return True, text
        return True, ""

    monkeypatch.setattr(mt, "_run", fake)
    assert mt.get_host_protections()["checks"]["firewall"]["on"] is expected


def test_platform_adapter_firewall_status_for_block_all(monkeypatch):
    monkeypatch.setattr(pa, "_run", lambda *a, **k: (True, "Firewall is set to block all incoming connections. (State = 2)"))
    assert pa.get_firewall_status()["enabled"] is True
