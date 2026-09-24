"""Security posture and the one-button report (MS-7, spec items 25, 49, 50;
finding #105).

Spec item 25: a network trust score must be multi-dimensional, "explicitly
not a single magic number". Item 49: a score must never hide evidence. Item
50: no security theater. So posture is six dimensions (plus "other" when a finding fits none), each rated from real
findings with the evidence attached, and a dimension with nothing to go on
is UNKNOWN, never "good". The report adds what could not be checked as its
own section.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from dourmouse.config import workspace_dir

GOOD, ATTENTION, AT_RISK, UNKNOWN = "good", "attention", "at_risk", "unknown"

#: Which findings speak to which dimension of posture.
DIMENSIONS: dict[str, dict[str, Any]] = {
    "device_hardening": {
        "label": "Device hardening",
        "kinds": {"firewall_disabled", "filevault_off", "sip_off", "gatekeeper_off", "stealth_mode_off",
                  "protection_turned_off"},
        "needs": ("host_protections", "firewall"),
    },
    "exposure": {
        "label": "Exposure to the network",
        "kinds": {"exposed_port", "remote_login_on", "screen_sharing_on", "new_listening_port",
                  "correlated_new_device_and_exposed_port"},
        "needs": ("listening_ports", "host_protections"),
    },
    "network_trust": {
        "label": "Trust in the current network",
        "kinds": {"weak_wifi", "arp_gateway_duplicate", "gateway_mac_changed", "dns_changed",
                  "wifi_security_changed", "new_device"},
        "needs": ("wifi", "arp_neighbors", "dns"),
    },
    "software_integrity": {
        "label": "Software integrity",
        "kinds": {"network_process_suspicious_location", "new_unsigned_network_process", "new_persistence",
                  "persistence_modified"},
        "needs": ("persistence",),
    },
    "downloads": {
        "label": "Downloads",
        "kinds": {"risky_download"},
        "needs": (),
    },
}


def rate(findings: list[dict[str, Any]], available: bool) -> str:
    """Evidence of a problem always shows, even when part of the area could
    not be read (a firewall-off finding must never hide behind "unknown");
    only "good" needs everything to have been checked."""
    if any(f["severity"] == "high" for f in findings):
        return AT_RISK
    if any(f["severity"] == "med" for f in findings):
        return ATTENTION
    return GOOD if available else UNKNOWN


def posture(findings: list[dict[str, Any]], telemetry_available: dict[str, bool],
            monitoring: dict[str, Any] | None) -> list[dict[str, Any]]:
    out = []
    for key, spec in DIMENSIONS.items():
        mine = [f for f in findings if f["kind"] in spec["kinds"]]
        available = all(telemetry_available.get(n, False) for n in spec["needs"])
        out.append({"dimension": key, "label": spec["label"], "rating": rate(mine, available),
                    "evidence": mine,
                    "not_checked": [n for n in spec["needs"] if not telemetry_available.get(n, False)]})
    mapped = set().union(*(spec["kinds"] for spec in DIMENSIONS.values()))
    other = [f for f in findings if f["kind"] not in mapped]
    if other:  # a finding is never dropped because no dimension claims it
        out.append({"dimension": "other", "label": "Other findings", "rating": rate(other, True),
                    "evidence": other, "not_checked": []})
    present = [i for i in (monitoring or {}).get("indicators", []) if i["status"] == "present"]
    out.append({
        "dimension": "monitoring", "label": "Signs of monitoring or remote control",
        "rating": UNKNOWN if monitoring is None else (ATTENTION if present else GOOD),
        "evidence": [{"kind": i["name"], "severity": "info", "title": i["name"].replace("_", " "),
                      "detail": i["evidence"]} for i in present],
        "not_checked": [] if monitoring else ["monitoring indicators"],
    })
    return out


def build_report(*, scan: Any = None, monitoring: dict[str, Any] | None = None,
                 downloads: list[dict[str, Any]] | None = None, lockdown: dict[str, Any] | None = None,
                 now: float | None = None) -> dict[str, Any]:
    """Run everything once and assemble the report. Every argument can be
    injected (tests); by default each part is read live."""
    from . import lockdown as ld
    from . import monitoring as mon
    from .sentry import SentryStore, default_db, run_scan

    if scan is None:
        scan = run_scan(write_alerts=False)
    findings: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for f in scan.all_findings:  # never list one condition twice (the sentry also dedupes at source, #110)
        if (f.kind, f.detail) in seen:
            continue
        seen.add((f.kind, f.detail))
        findings.append({"kind": f.kind, "severity": f.severity, "title": f.title, "detail": f.detail,
                         "recommended_action": f.recommended_action})
    if monitoring is None:
        monitoring = mon.analyze()
    if downloads is None:
        downloads = SentryStore(default_db()).recent_downloads(20)
    if lockdown is None:
        lockdown = ld.status(check_sites=False)
    dims = posture(findings, scan.telemetry_available, monitoring)
    worst = [d for d in dims if d["rating"] == AT_RISK]
    attention = [d for d in dims if d["rating"] == ATTENTION]
    headline = (
        f"{len(worst)} area(s) at risk: " + ", ".join(d["label"] for d in worst) if worst else
        f"No area at risk; {len(attention)} need attention." if attention else
        "No problems found in what could be checked."
    )
    unknowns = sorted({*monitoring.get("unknowns", []),
                       *(f"{d['label']}: {n} could not be read" for d in dims for n in d["not_checked"]),
                       "Malware inside files is only detected when a scanner (ClamAV) is installed; none is.",
                       "Anything outside this Mac: the router's own security, other devices' intentions."})
    return {
        "generated_at": now or time.time(), "headline": headline, "posture": dims,
        "findings": sorted(findings, key=lambda f: {"high": 0, "med": 1, "low": 2}.get(f["severity"], 3)),
        "monitoring": monitoring, "downloads": [d for d in downloads if d.get("risk") in ("high", "med")][:10],
        "lockdown": {"active": lockdown.get("active"), "apps": len(lockdown.get("apps", [])),
                     "sites": len(lockdown.get("sites", [])), "helper_installed": lockdown.get("helper_installed")},
        "unknowns": unknowns,
    }


def to_markdown(r: dict[str, Any]) -> str:
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["generated_at"]))
    lines = [f"# Security report, {when}", "", f"**{r['headline']}**", "", "## Posture, by area", ""]
    marks = {GOOD: "good", ATTENTION: "needs attention", AT_RISK: "AT RISK", UNKNOWN: "unknown (could not check)"}
    for d in r["posture"]:
        lines.append(f"- **{d['label']}**: {marks[d['rating']]}")
        for e in d["evidence"]:
            lines.append(f"  - {e['title']}")
    lines += ["", "## Findings and what to do", ""]
    if not r["findings"]:
        lines.append("None.")
    for f in r["findings"]:
        lines += [f"### [{f['severity']}] {f['title']}", "", f["detail"], "", f"What to do: {f['recommended_action']}", ""]
    lines += ["## Monitoring indicators", "", r["monitoring"].get("summary", ""), ""]
    for i in r["monitoring"].get("indicators", []):
        lines.append(f"- {i['name'].replace('_', ' ')}: {i['status']} ({i['evidence']})")
    lines += ["", "## Downloads needing a look", ""]
    lines += [f"- [{d['risk']}] {d['name']}: {'; '.join(d.get('reasons', []))}" for d in r["downloads"]] or ["None."]
    lk = r["lockdown"]
    lines += ["", "## Lockdown", "", f"{'ON' if lk['active'] else 'off'}; {lk['apps']} app(s), {lk['sites']} site(s) on the "
              f"blocklist; website blocking {'installed' if lk['helper_installed'] else 'NOT installed'}.", ""]
    lines += ["## Unknowns (what this report could not see)", ""] + [f"- {u}" for u in r["unknowns"]]
    return "\n".join(lines) + "\n"


def save_report(r: dict[str, Any], folder: Path | None = None) -> Path:
    from .privacy import private_dir

    folder = private_dir(folder or workspace_dir() / "security" / "reports")
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(r["generated_at"]))
    (folder / f"report-{stamp}.json").write_text(json.dumps(r, indent=2, default=str), encoding="utf-8")
    md = folder / f"report-{stamp}.md"
    md.write_text(to_markdown(r), encoding="utf-8")
    return md


def latest_saved(folder: Path | None = None) -> dict[str, Any] | None:
    folder = folder or workspace_dir() / "security" / "reports"
    files = sorted(folder.glob("report-*.json")) if folder.is_dir() else []
    return json.loads(files[-1].read_text(encoding="utf-8")) if files else None
