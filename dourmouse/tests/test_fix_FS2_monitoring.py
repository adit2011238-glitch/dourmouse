"""FS2 P4-55: a monitoring check that could not run is UNKNOWN, never ABSENT."""

from __future__ import annotations

from dourmouse.security import mac_telemetry as mt
from dourmouse.security import monitoring as m

_FAIL_TIMEOUT = (False, "security timed out after 30s")


def _install(monkeypatch, replies: dict[str, tuple[bool, str]]):
    def fake(cmd, timeout=0):
        key = " ".join(cmd)
        for prefix, reply in replies.items():
            if key.startswith(prefix):
                return reply
        return (True, "")

    monkeypatch.setattr(mt, "_run", fake)


def _ind(report, name):
    return next(i for i in report["indicators"] if i["name"] == name)


def _analyze():
    return m.analyze(host_protections={"checks": {}}, persistence={"items": []})


def test_trust_settings_timeout_is_unknown_not_absent(monkeypatch):
    _install(monkeypatch, {"security dump-trust-settings": _FAIL_TIMEOUT})
    i = _ind(_analyze(), "extra_trusted_roots")
    assert i["status"] == "unknown" and "timed out" in i["evidence"] and i["confidence"].startswith("none")


def test_trust_settings_none_found_is_still_absent(monkeypatch):
    _install(monkeypatch, {"security dump-trust-settings": (False, "SecTrustSettingsCopyCertificates: No Trust Settings were found")})
    assert _ind(_analyze(), "extra_trusted_roots")["status"] == "absent"


def test_trust_settings_found_in_one_domain_is_present_even_if_the_other_failed(monkeypatch):
    def fake(cmd, timeout=0):
        if cmd[:2] == ["security", "dump-trust-settings"]:
            return (True, "Cert 0: Evil Root CA\n") if "-d" in cmd else _FAIL_TIMEOUT
        return (True, "")

    monkeypatch.setattr(mt, "_run", fake)
    i = _ind(_analyze(), "extra_trusted_roots")
    assert i["status"] == "present" and "Evil Root CA" in i["evidence"]


def test_system_extensions_failure_is_unknown(monkeypatch):
    _install(monkeypatch, {"systemextensionsctl": (False, "systemextensionsctl is not available on this system")})
    assert _ind(_analyze(), "system_extensions")["status"] == "unknown"


def test_vpn_failure_is_unknown_not_no_vpn(monkeypatch):
    _install(monkeypatch, {"scutil --nc list": _FAIL_TIMEOUT})
    i = _ind(_analyze(), "vpn")
    assert i["status"] == "unknown" and "no VPN configured" not in i["evidence"]


def test_ps_failure_is_unknown_for_remote_control_software(monkeypatch):
    _install(monkeypatch, {"ps ": (False, "ps timed out after 30s")})
    assert _ind(_analyze(), "remote_control_software")["status"] == "unknown"


def test_ps_failure_still_reports_a_startup_item_match(monkeypatch):
    _install(monkeypatch, {"ps ": (False, "ps timed out after 30s")})
    r = m.analyze(host_protections={"checks": {}},
                  persistence={"items": [{"program": "/Library/Application Support/TeamViewer/TeamViewer_Service"}]})
    assert _ind(r, "remote_control_software")["status"] == "present"


def test_healthy_run_is_unchanged(monkeypatch):
    _install(monkeypatch, {"ps ": (True, "COMM\n/usr/bin/login\n"), "scutil --nc list": (True, "")})
    r = _analyze()
    assert _ind(r, "remote_control_software")["status"] == "absent"
    assert _ind(r, "vpn")["status"] == "absent"
