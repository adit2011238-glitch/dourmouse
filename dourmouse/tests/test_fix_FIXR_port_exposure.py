"""FIX-R R-2: a reclassified exposure value is not reported as a change; only a wider reach is."""

from __future__ import annotations

import pytest

from dourmouse.security import baseline as bl
from dourmouse.security import mac_detectors as md


def _port_state(exposure: str, addr: str = "[::1]"):
    return {"listening_ports": {"available": True, "listening_ports": [
        {"command": "cupsd", "pid": 5, "protocol": "TCP", "port": 631, "bind_address": addr, "exposure": exposure}]}}


def _kinds(known_value: str, now_value: str) -> list[str]:
    """Run the real baseline comparison, then the real detector, like sentry does."""
    state = _port_state(now_value)
    obs = bl.observations(state, "net")
    known = {(o.scope, o.category, o.key): known_value for o in obs}
    anomalies = bl.compare(obs, known)
    return [f.kind for f in md.detect_mac_findings(state, anomalies)]


@pytest.mark.parametrize("old,new", [
    ("UNKNOWN", "LOOPBACK_ONLY"),       # [::1] before the classifier learned IPv6
    ("UNKNOWN", "LOCAL_NETWORK"),       # fe80::1
    ("UNKNOWN", "ALL_INTERFACES"),      # [::]
    ("", "LOOPBACK_ONLY"),
    ("ALL_INTERFACES", "LOOPBACK_ONLY"),  # narrower
    ("LOCAL_NETWORK", "LOOPBACK_ONLY"),
    ("LOOPBACK_ONLY", "LOOPBACK_ONLY"),
])
def test_no_changed_finding_unless_the_port_became_reachable_from_more_places(old, new):
    assert "listening_port_exposure_changed" not in _kinds(old, new)


@pytest.mark.parametrize("old,new", [
    ("LOOPBACK_ONLY", "ALL_INTERFACES"),
    ("LOOPBACK_ONLY", "LOCAL_NETWORK"),
    ("LOCAL_NETWORK", "ALL_INTERFACES"),
    ("TAILSCALE", "ALL_INTERFACES"),
])
def test_a_real_widening_is_still_reported(old, new):
    assert "listening_port_exposure_changed" in _kinds(old, new)
