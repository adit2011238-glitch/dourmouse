"""Finding #102 (MS-5): "am I being monitored?" indicators, honest by construction."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from dourmouse.security import monitoring as m

FIX = Path(__file__).parent / "fixtures" / "mac"


def test_no_proxy_on_this_mac_and_a_set_proxy_is_seen():
    assert m.parse_proxies("<dictionary> {\n  ExceptionsList : <array> {\n  }\n  FTPPassive : 1\n}\n") == {}
    out = ("  HTTPEnable : 1\n  HTTPProxy : 10.0.0.9\n  HTTPPort : 8080\n  HTTPSEnable : 1\n  HTTPSProxy : 10.0.0.9\n"
           "  HTTPSPort : 8080\n  ProxyAutoConfigEnable : 1\n  ProxyAutoConfigURLString : http://wpad/proxy.pac\n")
    assert m.parse_proxies(out) == {"http": "10.0.0.9:8080", "https": "10.0.0.9:8080", "pac": "http://wpad/proxy.pac"}


def test_vpn_list_from_this_mac():
    vpns = m.parse_nc_list((FIX / "nc_list.txt").read_text(encoding="utf-8"))
    assert vpns and all({"state", "provider", "name"} <= set(v) for v in vpns)


def test_system_extensions_from_this_mac():
    exts = m.parse_system_extensions((FIX / "sysext.txt").read_text(encoding="utf-8"))
    assert exts and exts[0]["category"] == "network_extension" and exts[0]["active"]


def test_remote_control_software_by_process_and_by_startup_item():
    found = m.remote_control_apps(
        ["/usr/sbin/cfprefsd", "/Applications/AnyDesk.app/Contents/MacOS/AnyDesk"],
        ["/Library/Application Support/TeamViewer/TeamViewer_Service"],
    )
    assert {f["app"]: f["where"] for f in found} == {"AnyDesk": "running", "TeamViewer": "starts automatically"}
    assert m.remote_control_apps(["/usr/bin/login"], []) == []


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS tools")
def test_live_analysis_is_complete_and_names_its_unknowns():
    r = m.analyze()
    names = {i["name"] for i in r["indicators"]}
    assert names == {"proxy", "mdm_enrollment", "configuration_profiles", "extra_trusted_roots", "vpn",
                     "system_extensions", "remote_access_services", "remote_control_software"}
    assert all(i["status"] in ("present", "absent", "unknown") and i["confidence"] for i in r["indicators"])
    assert any("Full Disk Access" in u for u in r["unknowns"])
