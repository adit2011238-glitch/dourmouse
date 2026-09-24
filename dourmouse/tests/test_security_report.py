"""Finding #105 (MS-7): multi-dimensional posture and the one-button report.
A dimension that could not be checked is unknown, never good; evidence is
never hidden; nothing is dropped."""

from __future__ import annotations

from dourmouse.security import report as rp
from dourmouse.security.sentry import SentryFinding, SentryScanResult

ALL_AVAILABLE = dict.fromkeys(("interfaces", "default_gateway", "dns", "arp_neighbors", "listening_ports", "firewall", "wifi", "host_protections", "persistence"), True)


def _f(kind, sev, detail="d"):
    return SentryFinding(fingerprint=kind + detail, kind=kind, severity=sev, title=kind.replace("_", " "),
                         detail=detail, recommended_action="do x")


def _scan(findings, available=None):
    return SentryScanResult(all_findings=findings, new_findings=[], suppressed_false_positives=[], risk_score=0.0,
                            telemetry_available=available or ALL_AVAILABLE, alerts_written=0)


MON = {"summary": "s", "indicators": [{"name": "vpn", "status": "present", "evidence": "utun4"},
                                      {"name": "mdm_profile", "status": "absent", "evidence": "none"}],
       "unknowns": ["keyloggers need Input Monitoring access to list"]}
LOCK = {"active": False, "apps": [], "sites": [{"domain": "a.com"}], "helper_installed": False}


def _report(findings, available=None, monitoring=MON):
    return rp.build_report(scan=_scan(findings, available), monitoring=monitoring, downloads=[], lockdown=LOCK,
                           now=1_790_000_000.0)


def _dim(r, key):
    return next(d for d in r["posture"] if d["dimension"] == key)


def test_each_dimension_is_rated_from_its_own_evidence():
    r = _report([_f("firewall_disabled", "high"), _f("remote_login_on", "med"), _f("stealth_mode_off", "low")])
    assert _dim(r, "device_hardening")["rating"] == rp.AT_RISK
    assert [e["kind"] for e in _dim(r, "device_hardening")["evidence"]] == ["firewall_disabled", "stealth_mode_off"]
    assert _dim(r, "exposure")["rating"] == rp.ATTENTION
    assert _dim(r, "network_trust")["rating"] == rp.GOOD
    assert _dim(r, "monitoring")["rating"] == rp.ATTENTION
    assert r["headline"] == "1 area(s) at risk: Device hardening"


def test_what_could_not_be_read_is_unknown_not_good():
    available = dict(ALL_AVAILABLE, wifi=False)
    r = _report([], available)
    d = _dim(r, "network_trust")
    assert d["rating"] == rp.UNKNOWN and d["not_checked"] == ["wifi"]
    assert "Trust in the current network: wifi could not be read" in r["unknowns"]


def test_the_same_socket_twice_is_one_finding():
    r = _report([_f("exposed_port", "med", "rapportd 49152"), _f("exposed_port", "med", "rapportd 49152")])
    assert len(r["findings"]) == 1


def test_an_unmapped_finding_is_never_dropped():
    r = _report([_f("brand_new_rule", "high")])
    assert _dim(r, "other")["rating"] == rp.AT_RISK


def test_markdown_and_save(tmp_path):
    r = _report([_f("firewall_disabled", "high")])
    md = rp.to_markdown(r)
    for section in ("## Posture, by area", "## Findings and what to do", "## Monitoring indicators",
                    "## Lockdown", "## Unknowns (what this report could not see)"):
        assert section in md
    assert "website blocking NOT installed" in md and "keyloggers" in md
    path = rp.save_report(r, tmp_path)
    assert path.read_text(encoding="utf-8") == md and path.with_suffix(".json").exists()


def test_a_known_problem_is_never_hidden_behind_unknown():
    """Found by the console route test (#110): firewall off, but host
    protections unreadable, rated the whole area unknown."""
    available = dict(ALL_AVAILABLE, host_protections=False)
    r = _report([_f("firewall_disabled", "high")], available)
    d = _dim(r, "device_hardening")
    assert d["rating"] == rp.AT_RISK and d["not_checked"] == ["host_protections"]
    assert _dim(_report([], available), "device_hardening")["rating"] == rp.UNKNOWN
