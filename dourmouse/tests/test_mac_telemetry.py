"""Finding #099 (MS-1): Mac security telemetry. Parsers run against output
captured from this Mac (dourmouse/tests/fixtures/mac, nearby networks and
hardware addresses stripped); a few checks run live and skip off macOS."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from dourmouse.security import mac_telemetry as mt

FIX = Path(__file__).parent / "fixtures" / "mac"
on_mac = pytest.mark.skipif(sys.platform != "darwin", reason="macOS tools")


def _fx(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


class TestWifi:
    def test_the_live_link_is_parsed_and_the_redacted_ssid_is_not_guessed(self):
        w = mt.parse_wifi(json.loads(_fx("airport.json")))
        assert w["connected"] and w["interface"] == "en0"
        assert w["security"] == "wpa2_personal"
        assert w["ssid"] is None and w["ssid_redacted"] is True
        assert w["rssi_dbm"] == -24 and w["noise_dbm"] == -92
        assert w["channel"].startswith("40")

    def test_open_and_wep_networks_are_classed_weak(self):
        for mode, name in (("spairport_security_mode_none", "open"), ("spairport_security_mode_wep", "wep")):
            profile = {"SPAirPortDataType": [{"spairport_airport_interfaces": [{"_name": "en0",
                       "spairport_current_network_information": {"_name": "Cafe", "spairport_security_mode": mode}}]}]}
            w = mt.parse_wifi(profile)
            assert w["security"] == name and name in mt.WEAK_WIFI and w["ssid"] == "Cafe"

    def test_no_current_network_means_not_connected(self):
        assert mt.parse_wifi({"SPAirPortDataType": [{"spairport_airport_interfaces": [{"_name": "en0"}]}]}) == {
            "available": True, "connected": False}


class TestCodeSigning:
    def test_apple_signed(self):
        sig = mt.parse_codesign(_fx("codesign_apple.txt"), True)
        assert sig["signed"] and sig["kind"] == "apple"

    def test_adhoc_signed(self):
        sig = mt.parse_codesign(_fx("codesign_adhoc.txt"), True)
        assert sig["signed"] and sig["kind"] == "adhoc"

    def test_unsigned(self):
        assert mt.parse_codesign(_fx("codesign_unsigned.txt"), False) == {
            "signed": False, "kind": "unsigned", "authorities": [], "team_id": None}

    def test_developer_id(self):
        out = "Authority=Developer ID Application: Example Ltd (ABCDE12345)\nAuthority=Developer ID Certification Authority\nTeamIdentifier=ABCDE12345\n"
        sig = mt.parse_codesign(out, True)
        assert sig["kind"] == "developer_id" and sig["team_id"] == "ABCDE12345"

    def test_gatekeeper_verdicts(self):
        assert mt.parse_spctl(_fx("spctl_accepted.txt"))["verdict"] == "accepted"
        assert mt.parse_spctl(_fx("spctl_accepted.txt"))["source"] == "Apple System"
        assert mt.parse_spctl(_fx("spctl_rejected.txt"))["verdict"] == "rejected"

    def test_a_bare_command_line_binary_is_not_an_app_not_rejected(self):
        out = "/tmp/echo: rejected (the code is valid but does not seem to be an app)\norigin=macOS Software Signing\n"
        assert mt.parse_spctl(out)["verdict"] == "not_an_app"

    def test_an_app_binary_is_assessed_as_its_bundle(self):
        assert mt._bundle_for("/Applications/Safari.app/Contents/MacOS/Safari") == "/Applications/Safari.app"
        assert mt._bundle_for("/usr/bin/curl") is None


class TestHostProtections:
    def test_launchctl_disabled_list(self):
        services = mt.parse_disabled_services(_fx("launchctl_disabled.txt"))
        assert services  # real capture from this Mac
        assert all(isinstance(v, bool) for v in services.values())

    @on_mac
    def test_every_check_reports_a_real_value_or_none(self):
        checks = mt.get_host_protections()["checks"]
        assert set(checks) == {"firewall", "stealth_mode", "filevault", "sip", "gatekeeper", "remote_login", "screen_sharing"}
        for name, c in checks.items():
            assert c["on"] in (True, False, None), name


class TestProcesses:
    @pytest.mark.parametrize(("path", "cls"), [
        ("/System/Applications/Mail.app/Contents/MacOS/Mail", "system"),
        ("/usr/bin/curl", "system"),
        ("/Applications/Safari.app/Contents/MacOS/Safari", "applications"),
        (str(Path.home() / "Downloads/tool"), "downloads"),
        ("/tmp/x", "temporary"),
        ("/private/var/folders/ab/cd/T/x", "temporary"),
        ("/opt/homebrew/bin/python3", "other"),
        (None, "unknown"),
    ])
    def test_location_class(self, path, cls):
        assert mt.location_class(path) == cls

    def test_this_process_is_described(self):
        d = mt.process_details(os.getpid())
        assert d["available"] and d["exe"] and d["parent_name"]

    def test_a_gone_process_is_reported_not_raised(self):
        assert mt.process_details(2**22 + 12345)["available"] is False


class TestPersistence:
    def test_a_launch_agent_plist_is_parsed(self):
        import plistlib

        data = plistlib.dumps({"Label": "com.example.agent", "ProgramArguments": ["/Users/x/.hidden/run", "--quiet"],
                               "RunAtLoad": True})
        assert mt.parse_launchd_plist(data) == {"label": "com.example.agent", "program": "/Users/x/.hidden/run",
                                                 "run_at_load": True, "keep_alive": False}

    def test_items_are_listed_with_a_hash_and_the_root_gap_is_named(self, tmp_path, monkeypatch):
        import plistlib

        (tmp_path / "a.plist").write_bytes(plistlib.dumps({"Label": "a", "Program": "/bin/true"}))
        (tmp_path / "bad.plist").write_bytes(b"not a plist")
        monkeypatch.setattr(mt, "persistence_dirs", lambda: [tmp_path])
        out = mt.get_persistence_items()
        good = [i for i in out["items"] if "error" not in i]
        assert good[0]["label"] == "a" and len(good[0]["sha256"]) == 64
        assert any("error" in i for i in out["items"])
        assert "sfltool" in out["gaps"][0]


class TestDiagnostics:
    def test_ping_stats(self):
        p = mt.parse_ping(_fx("ping.txt"))
        assert p["sent"] == 5 and p["loss_pct"] == 0.0 and p["avg_ms"] > 0 and p["jitter_ms"] is not None

    def test_total_loss(self):
        out = "PING 10.9.9.9 (10.9.9.9): 56 data bytes\n\n--- 10.9.9.9 ping statistics ---\n5 packets transmitted, 0 packets received, 100.0% packet loss\n"
        p = mt.parse_ping(out)
        assert p["loss_pct"] == 100.0 and p["avg_ms"] is None

    def test_network_id_is_stable_and_reveals_nothing(self):
        wifi = {"connected": True, "security": "wpa2_personal"}
        a = mt.network_id("192.168.1.1", wifi, ["home.lan"])
        assert a == mt.network_id("192.168.1.1", wifi, ["home.lan"]) and len(a) == 16
        assert "192.168" not in a
        assert a != mt.network_id("192.168.1.1", wifi, ["cafe.example"])
        assert a != mt.network_id("192.168.1.1", {"connected": False}, ["home.lan"])


class TestFastWifi:
    def test_ipconfig_summary_from_this_mac(self):
        w = mt.parse_ipconfig_summary(_fx("ipconfig_summary.txt"))
        assert w["connected"] and w["security"] == "wpa2_personal" and w["ssid"] is None and w["ssid_redacted"]

    def test_open_network_and_ethernet(self):
        open_net = "InterfaceType : WiFi\nLinkStatusActive : TRUE\nSSID : Cafe\nSecurity : NONE\n"
        assert mt.parse_ipconfig_summary(open_net)["security"] == "open"
        assert mt.parse_ipconfig_summary("InterfaceType : Ethernet\nLinkStatusActive : TRUE\n")["connected"] is False
