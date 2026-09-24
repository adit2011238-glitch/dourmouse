"""The ``security`` subagent — read-only inspection tools over
``platform_adapter``'s real telemetry (Phase 4, docs/GODSPEED_ROADMAP.md),
plus the sentry's own bookkeeping (false-positive dismissal, incident
lifecycle -- Phase 2 step 3).

Deliberately read-only with respect to the HOST in this pass: no tool here
changes firewall rules, disconnects anything, or otherwise acts on the
machine or network. The incident tools mutate this subsystem's OWN
persisted record-keeping (``SentryStore``'s ``incidents`` table), the same
REGULAR-permission bookkeeping ``goal_tools.py`` already establishes for
its own store. Real remediation tools that act on the host are separate,
later work, and per the spec's own policy default ("ask before changing
anything") will need REQUIRES_CONFIRMATION when they exist.
"""

from __future__ import annotations

import dataclasses
import time
from typing import Any

from dourmouse.dispatch import Subagent, ToolSpec
from dourmouse.security import platform_adapter as pa
from dourmouse.security import reputation as rep
from dourmouse.security.sentry import SentryStore, run_scan
from dourmouse.security.sentry import default_db as _sentry_db


def _format_interfaces(result: dict[str, Any]) -> str:
    if not result["available"]:
        return f"Interfaces: unavailable ({result['reason']})"
    lines = [f"{len(result['interfaces'])} interface(s):"]
    for iface in result["interfaces"]:
        if "UP" not in iface["flags"]:
            continue
        addrs = ", ".join(a["address"] for a in iface["ipv4"]) or "no IPv4"
        lines.append(f"  {iface['name']}: {addrs}" + (f" ({iface['status']})" if iface["status"] else ""))
    return "\n".join(lines)


def _security_status(_arguments: dict[str, Any]) -> str:
    snap = pa.get_system_security_state()
    lines = [_format_interfaces(snap["interfaces"])]

    gw = snap["default_gateway"]
    lines.append(f"Default gateway: {gw['gateway']} via {gw['interface']}" if gw["available"] else f"Default gateway: unavailable ({gw['reason']})")

    dns = snap["dns"]
    if dns["available"]:
        servers = sorted({ns for r in dns["resolvers"] for ns in r["nameservers"]})
        lines.append(f"DNS: {len(dns['resolvers'])} resolver(s), nameservers: {', '.join(servers) or 'none'}")
    else:
        lines.append(f"DNS: unavailable ({dns['reason']})")

    fw = snap["firewall"]
    lines.append(f"Application Firewall: {'enabled' if fw['enabled'] else 'disabled'}" if fw["available"] else f"Firewall: unavailable ({fw['reason']})")

    lp = snap["listening_ports"]
    if lp["available"]:
        exposed = [p for p in lp["listening_ports"] if p["exposure"] in ("ALL_INTERFACES", "LOCAL_NETWORK", "TAILSCALE")]
        lines.append(f"Listening ports: {len(lp['listening_ports'])} total, {len(exposed)} reachable beyond this machine")
        for p in exposed:
            lines.append(f"  {p['command']} (pid {p['pid']}) on {p['bind_address']}:{p['port']} — {p['exposure']}")
    else:
        lines.append(f"Listening ports: unavailable ({lp['reason']})")

    arp = snap["arp_neighbors"]
    lines.append(f"Local network: {len(arp['neighbors'])} known neighbor(s)" if arp["available"] else f"Local network neighbors: unavailable ({arp['reason']})")

    return "\n".join(lines)


def _list_exposed_services(arguments: dict[str, Any]) -> str:
    lp = pa.get_listening_ports()
    if not lp["available"]:
        return f"ERROR: could not read listening ports: {lp['reason']}"
    only = str(arguments.get("exposure") or "").strip().upper() or None
    ports = lp["listening_ports"]
    if only:
        ports = [p for p in ports if p["exposure"] == only]
    if not ports:
        return "No listening services" + (f" with exposure {only}" if only else "") + "."
    lines = [f"{len(ports)} listening service(s):"]
    for p in ports:
        lines.append(f"  {p['command']} (pid {p['pid']}) — {p['protocol']} {p['bind_address']}:{p['port']} — {p['exposure']}")
    return "\n".join(lines)


def _security_sentry_scan(_arguments: dict[str, Any]) -> str:
    result = run_scan()
    lines = [
        f"Security sentry scan: {len(result.all_findings)} active finding(s), "
        f"risk score {result.risk_score:.1f}"
        + (f", {len(result.suppressed_false_positives)} previously dismissed" if result.suppressed_false_positives else "")
        + "."
    ]
    for f in result.all_findings:
        is_new = f in result.new_findings
        lines.append(
            f"\n[{f.severity.upper()}{' - NEW' if is_new else ''}] {f.title}"
            f"\n  {f.detail}"
            f"\n  Suggested (not applied): {f.recommended_action}"
            f"\n  fingerprint={f.fingerprint} (use security_sentry_dismiss to mark a false positive)"
        )
    if result.alerts_written:
        lines.append(f"\n{result.alerts_written} real alert(s) written for new HIGH-severity finding(s).")
    unavailable = [k for k, ok in result.telemetry_available.items() if not ok]
    if unavailable:
        lines.append(f"\nTelemetry unavailable for: {', '.join(unavailable)} (honest gap, not fabricated).")
    return "\n".join(lines)


def _security_external_peers(_arguments: dict[str, Any]) -> str:
    result = pa.get_established_connections()
    if not result["available"]:
        return f"ERROR: could not read established connections: {result['reason']}"
    peers: dict[str, list[str]] = {}
    for c in result["connections"]:
        if rep._reject_non_public(c["remote_address"]) is not None:
            continue  # real LAN/loopback/Tailscale peer -- not an external one
        peers.setdefault(c["remote_address"], []).append(f"{c['command']} (pid {c['pid']})")
    if not peers:
        return "No real external (public-internet) peers currently connected."
    lines = [f"{len(peers)} real external peer(s) currently connected:"]
    for ip, commands in peers.items():
        lines.append(f"  {ip} -- {', '.join(sorted(set(commands)))}")
    return "\n".join(lines)


def _security_check_reputation(arguments: dict[str, Any]) -> str:
    ip = str(arguments.get("ip") or "").strip()
    if not ip:
        return "ERROR: security_check_reputation requires a non-empty 'ip'."
    result = rep.check_ip_reputation(ip)
    if not result["available"]:
        return f"ERROR: {result['reason']}"
    return (
        f"{result['ip']}: abuse confidence {result['abuse_confidence_score']}/100, "
        f"{result['total_reports']} real report(s), country {result['country_code']}, "
        f"ISP {result['isp']}, Tor exit node: {result['is_tor']}"
    )


def _security_known_devices(_arguments: dict[str, Any]) -> str:
    rows = SentryStore(_sentry_db()).devices_snapshot()
    if not rows:
        return "No real device baseline recorded yet -- run security_sentry_scan first."
    lines = [f"{len(rows)} real known device(s) in the baseline:"]
    for r in rows:
        label = r["hostname"] or r["ip"]
        lines.append(f"  {label} -- MAC {r['mac'] or 'unknown'}, IP {r['ip']}")
    return "\n".join(lines)


def _security_incident_open(arguments: dict[str, Any]) -> str:
    fingerprint = str(arguments.get("fingerprint") or "").strip()
    if not fingerprint:
        return "ERROR: security_incident_open requires a non-empty 'fingerprint'."
    note = str(arguments.get("note") or "").strip()
    result = SentryStore(_sentry_db()).open_incident(fingerprint, note, now=time.time())
    if result == "unknown_fingerprint":
        return f"ERROR: no known finding with fingerprint {fingerprint!r} (run security_sentry_scan first)."
    if result == "already_open":
        return f"Incident for {fingerprint} is already open -- no new case created."
    return f"Real incident opened for {fingerprint} (status OPEN)."


def _security_incident_update(arguments: dict[str, Any]) -> str:
    fingerprint = str(arguments.get("fingerprint") or "").strip()
    if not fingerprint:
        return "ERROR: security_incident_update requires a non-empty 'fingerprint'."
    status = str(arguments.get("status") or "").strip().upper() or None
    note = str(arguments.get("note") or "").strip() or None
    try:
        result = SentryStore(_sentry_db()).update_incident(fingerprint, status, note, now=time.time())
    except ValueError as exc:
        return f"ERROR: {exc}"
    if result == "not_found":
        return f"ERROR: no open incident for {fingerprint} (run security_incident_open first)."
    if result == "terminal":
        return f"ERROR: incident {fingerprint} is already closed -- open a new incident for a recurrence."
    incident = SentryStore(_sentry_db()).get_incident(fingerprint)
    return f"Incident {fingerprint} updated: status={incident['status']}, {len(incident['notes'])} note(s)."


def _security_incidents(arguments: dict[str, Any]) -> str:
    status = str(arguments.get("status") or "").strip().upper() or None
    rows = SentryStore(_sentry_db()).list_incidents(status)
    if not rows:
        return "No real incidents" + (f" with status {status}" if status else "") + "."
    lines = [f"{len(rows)} real incident(s):"]
    for r in rows:
        lines.append(f"  {r['fingerprint']} -- {r['status']} ({len(r['notes'])} note(s))")
    return "\n".join(lines)


def _security_sentry_dismiss(arguments: dict[str, Any]) -> str:
    fingerprint = str(arguments.get("fingerprint") or "").strip()
    if not fingerprint:
        return "ERROR: security_sentry_dismiss requires a non-empty 'fingerprint'."
    ok = SentryStore(_sentry_db()).mark_false_positive(fingerprint)
    if not ok:
        return f"ERROR: no known finding with fingerprint {fingerprint!r} (run security_sentry_scan first)."
    return f"Marked {fingerprint} as a false positive -- it will not be reported as a new finding again."


def _security_downloads(arguments: dict[str, Any]) -> str:
    from pathlib import Path

    from .downloads import assess

    target = (arguments.get("path") or "").strip()
    if target:
        p = Path(target).expanduser()
        if not p.exists():
            return f"ERROR: no such file: {p}"
        rows = [dataclasses.asdict(assess(p))]
    else:
        rows = SentryStore(_sentry_db()).recent_downloads(20)
        if not rows:
            return "No downloads have been assessed yet (the watcher reports files that arrive after it starts)."
    lines = []
    for r in rows:
        lines.append(f"[{r['risk']}] {r['name']} ({r['kind']}, {r['size']} bytes)")
        for reason in r.get("reasons") or []:
            lines.append(f"    - {reason}")
        if r.get("where_from"):
            lines.append(f"    from: {r['where_from'][0]}")
        lines.append(f"    {r.get('confidence', '')}")
    return "\n".join(lines)


def _security_monitoring_check(_arguments: dict[str, Any]) -> str:
    from .monitoring import analyze

    r = analyze()
    lines = [r["summary"], ""]
    for i in r["indicators"]:
        lines.append(f"[{i['status'].upper()}] {i['name'].replace('_', ' ')}: {i['evidence']}")
        if i["status"] == "present":
            lines.append(f"    what it means: {i['meaning']}")
        lines.append(f"    confidence: {i['confidence']}")
    lines.append("")
    lines.append("Could not be checked:")
    lines.extend(f"  - {u}" for u in r["unknowns"])
    return "\n".join(lines)


def build_security_subagent() -> Subagent:
    return Subagent(
        name="security",
        domain="Both",
        description=(
            "Real, read-only network and host security telemetry for this machine: "
            "interfaces, default gateway, DNS resolvers, ARP neighbors, listening "
            "ports (with exposure classification), and Application Firewall state. "
            "Observational only — never fabricates a finding and never changes "
            "anything."
        ),
        tools=(
            ToolSpec(
                name="security_status",
                description="A real, current summary of this host's network and security posture: interfaces, gateway, DNS, firewall, and any service reachable beyond this machine.",
                parameters={"type": "object", "properties": {}},
                handler=_security_status,
            ),
            ToolSpec(
                name="list_exposed_services",
                description="List real listening network services, optionally filtered by exposure (LOOPBACK_ONLY, LOCAL_NETWORK, TAILSCALE, ALL_INTERFACES, UNKNOWN).",
                parameters={"type": "object", "properties": {"exposure": {"type": "string", "default": ""}}},
                handler=_list_exposed_services,
            ),
            ToolSpec(
                name="security_sentry_scan",
                description=(
                    "Run a real security scan: checks this host's real telemetry against a "
                    "real, deterministic rule set (no model judgment) -- currently a disabled "
                    "Application Firewall and any service exposed to more than this machine. "
                    "A genuinely NEW high-severity finding writes a real, persisted alert. A "
                    "finding already dismissed with security_sentry_dismiss stays suppressed."
                ),
                parameters={"type": "object", "properties": {}},
                handler=_security_sentry_scan,
            ),
            ToolSpec(
                name="security_external_peers",
                description=(
                    "List real, currently-established outbound connections to public-"
                    "internet peers (excludes LAN/loopback/Tailscale addresses) -- the "
                    "real candidates for security_check_reputation, never a scan target."
                ),
                parameters={"type": "object", "properties": {}},
                handler=_security_external_peers,
            ),
            ToolSpec(
                name="security_check_reputation",
                description=(
                    "Real AbuseIPDB reputation lookup for one real public IP address "
                    "this host has genuinely been observed connecting to (see "
                    "security_external_peers). Honestly reports NOT CONFIGURED if no "
                    "ABUSEIPDB_API_KEY is set, and refuses private/LAN addresses."
                ),
                parameters={
                    "type": "object",
                    "properties": {"ip": {"type": "string"}},
                    "required": ["ip"],
                },
                handler=_security_check_reputation,
            ),
            ToolSpec(
                name="security_known_devices",
                description=(
                    "List this host's real, persisted known-device baseline (MAC/IP/"
                    "hostname, first/last seen) built from real ARP neighbors seen "
                    "across past security_sentry_scan runs -- the same baseline a "
                    "genuinely new device on the LAN is compared against."
                ),
                parameters={"type": "object", "properties": {}},
                handler=_security_known_devices,
            ),
            ToolSpec(
                name="security_sentry_dismiss",
                description=(
                    "Mark a security_sentry_scan finding (by its fingerprint) as a false "
                    "positive so it stops being reported as new on future scans."
                ),
                parameters={
                    "type": "object",
                    "properties": {"fingerprint": {"type": "string"}},
                    "required": ["fingerprint"],
                },
                handler=_security_sentry_dismiss,
            ),
            ToolSpec(
                name="security_incident_open",
                description=(
                    "Open a real, tracked incident/case for an existing security_sentry_scan "
                    "finding (by its fingerprint), with an optional initial note -- a real "
                    "OPEN -> INVESTIGATING -> RESOLVED/ACCEPTED_RISK operator workflow, not "
                    "just a flat findings list."
                ),
                parameters={
                    "type": "object",
                    "properties": {"fingerprint": {"type": "string"}, "note": {"type": "string", "default": ""}},
                    "required": ["fingerprint"],
                },
                handler=_security_incident_open,
            ),
            ToolSpec(
                name="security_incident_update",
                description=(
                    "Move an open incident to a new status (OPEN, INVESTIGATING, RESOLVED, "
                    "ACCEPTED_RISK) and/or append a real note. A RESOLVED/ACCEPTED_RISK "
                    "incident never silently reopens -- open a new incident for a real "
                    "recurrence instead."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "fingerprint": {"type": "string"},
                        "status": {"type": "string", "default": ""},
                        "note": {"type": "string", "default": ""},
                    },
                    "required": ["fingerprint"],
                },
                handler=_security_incident_update,
            ),
            ToolSpec(
                name="security_incidents",
                description="List real tracked incidents, optionally filtered by status.",
                parameters={"type": "object", "properties": {"status": {"type": "string", "default": ""}}},
                handler=_security_incidents,
            ),
            ToolSpec(
                name="security_monitoring_check",
                description=(
                    "Answer 'am I being monitored?' honestly: proxies, MDM, configuration profiles, extra trusted "
                    "root certificates, VPNs, network and endpoint-security extensions, remote access services, and "
                    "remote-control software. Each indicator is present, absent or unknown with its evidence, and "
                    "what could not be checked is listed."
                ),
                parameters={"type": "object", "properties": {}},
                handler=_security_monitoring_check,
            ),
            ToolSpec(
                name="security_downloads",
                description=(
                    "List what recently landed in ~/Downloads with each file's assessment (real type, origin, "
                    "signature, Gatekeeper verdict, risk and the reasons), or assess one file by path."
                ),
                parameters={"type": "object", "properties": {"path": {"type": "string", "default": ""}}},
                handler=_security_downloads,
            ),
        ),
    )
