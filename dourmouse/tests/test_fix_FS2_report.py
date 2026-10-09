"""FS2 P3-61: the downloads area is not 'good' unless downloads were assessed; the ClamAV line is true."""

from __future__ import annotations

import shutil

from dourmouse.security import report as rp
from dourmouse.security.sentry import SentryScanResult

ALL = dict.fromkeys(("interfaces", "default_gateway", "dns", "arp_neighbors", "listening_ports", "firewall", "wifi",
                     "host_protections", "persistence"), True)
MON = {"summary": "s", "indicators": [], "unknowns": []}
LOCK = {"active": False, "apps": [], "sites": [], "helper_installed": False}


def _report(downloads):
    scan = SentryScanResult(all_findings=[], new_findings=[], suppressed_false_positives=[], risk_score=0.0,
                            telemetry_available=ALL, alerts_written=0)
    return rp.build_report(scan=scan, monitoring=MON, downloads=downloads, lockdown=LOCK, now=1_790_000_000.0)


def _dim(r, key):
    return next(d for d in r["posture"] if d["dimension"] == key)


def test_no_assessed_downloads_means_unknown_not_good():
    r = _report([])
    d = _dim(r, "downloads")
    assert d["rating"] == rp.UNKNOWN
    assert "Downloads: no download has been assessed yet" in r["unknowns"]
    assert not any("downloads_assessed" in u for u in r["unknowns"])


def test_assessed_clean_downloads_are_good():
    assert _dim(_report([{"name": "a.pdf", "risk": "none", "reasons": []}]), "downloads")["rating"] == rp.GOOD


def test_a_risky_download_rates_the_area_and_is_listed_as_evidence():
    r = _report([{"name": "evil.dmg", "risk": "high", "reasons": ["unsigned"], "sha256": "x"}])
    d = _dim(r, "downloads")
    assert d["rating"] == rp.AT_RISK and d["evidence"][0]["title"].endswith("evil.dmg")
    assert r["headline"].startswith("1 area(s) at risk: Downloads")


def test_clamav_sentence_follows_reality(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda t: None)
    assert any("none is" in u for u in _report([]) ["unknowns"])
    monkeypatch.setattr(shutil, "which", lambda t: "/usr/local/bin/clamscan" if t == "clamscan" else None)
    unknowns = _report([])["unknowns"]
    assert not any("none is" in u for u in unknowns)
    assert any("ClamAV" in u and "detection rates" in u for u in unknowns)
