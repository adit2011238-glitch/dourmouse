"""Finding 205: the Wi-Fi "what can this network see" summary is fixed sentences chosen from facts read off the Mac."""

from __future__ import annotations

from dourmouse.os_api import security_wifi as sw

HOME = {"available": True, "connected": True, "ssid": "Home", "ssid_redacted": False, "security": "wpa2_personal"}


def test_dns_kind():
    assert sw.dns_kind(["192.168.1.1"]) == "router"
    assert sw.dns_kind(["1.1.1.1", "1.0.0.1"]) == "public"
    assert sw.dns_kind(["192.168.1.1", "8.8.8.8"]) == "mixed"
    assert sw.dns_kind([]) == "unknown" and sw.dns_kind(["fe80::1%en0"]) == "router"


def test_home_router_dns_sees_site_names_and_hides_page_content():
    out = sw.can_see(HOME, ["192.168.1.1"], False)
    text = " ".join(out["sees"])
    assert out["state"] == "connected" and "router answers" in text
    assert any("HTTPS" in h for h in out["hidden"]) and out["unknown"] == []


def test_vpn_connected_hides_the_sites_from_the_network():
    out = sw.can_see(HOME, ["192.168.1.1"], True)
    assert any("VPN is in use" in s for s in out["sees"])
    assert "Which sites and servers you connect to." in out["hidden"]
    assert not any("Every site name" in s for s in out["sees"])


def test_open_network_says_anyone_in_range_can_capture_and_never_claims_others_cannot_read():
    out = sw.can_see({**HOME, "security": "open"}, ["1.1.1.1"], False)
    assert any("Anyone in range" in s for s in out["sees"])
    assert not any("without the password" in h for h in out["hidden"])


def test_a_fact_that_could_not_be_read_is_listed_as_unknown_not_as_fine():
    out = sw.can_see({**HOME, "security": "unknown"}, [], None)
    assert len(out["unknown"]) == 3
    assert sw.can_see({"available": False}, [], None)["state"] == "unavailable"
    assert sw.can_see({"available": True, "connected": False}, [], None)["state"] == "offline"


def test_build_reads_dns_and_vpn_only_while_connected():
    calls = []
    out = sw.build(lambda: {"available": True, "connected": False}, lambda: calls.append("dns") or [], lambda: calls.append("vpn") or False)
    assert out["connected"] is False and calls == [] and out["security_word"] is None
    out = sw.build(lambda: HOME, lambda: ["192.168.1.1"], lambda: False)
    assert out["name"] == "Home" and out["security_word"] == "WPA2 password" and out["weak"] is False and out["dns_kind"] == "router"


def test_the_route_is_registered_read_only():
    from dourmouse import os_api

    os_api._ensure_loaded() if hasattr(os_api, "_ensure_loaded") else None
    assert ("GET", "/api/os/security/wifi") in os_api.routes()
    assert ("POST", "/api/os/security/wifi") not in os_api.routes()
