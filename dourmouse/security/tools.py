"""The ``security`` subagent — read-only inspection tools over
``platform_adapter``'s real telemetry (Phase 4, docs/GODSPEED_ROADMAP.md).

Deliberately read-only in this pass: every tool here observes, none of
them changes firewall rules, disconnects anything, or otherwise acts.
Real remediation tools are separate, later work, and per the spec's own
policy default ("ask before changing anything") will need
REQUIRES_CONFIRMATION when they exist — nothing here needs it, since
reading your own host's network state is a REGULAR, safe operation
(matches ``goal_tools.py``'s identical reasoning for its own bookkeeping
tools).
"""

from __future__ import annotations

from typing import Any

from dourmouse.dispatch import Subagent, ToolSpec
from dourmouse.security import platform_adapter as pa
from dourmouse.security.sentry import DEFAULT_DB as _SENTRY_DB
from dourmouse.security.sentry import SentryStore, run_scan


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


def _security_known_devices(_arguments: dict[str, Any]) -> str:
    rows = SentryStore(_SENTRY_DB).devices_snapshot()
    if not rows:
        return "No real device baseline recorded yet -- run security_sentry_scan first."
    lines = [f"{len(rows)} real known device(s) in the baseline:"]
    for r in rows:
        label = r["hostname"] or r["ip"]
        lines.append(f"  {label} -- MAC {r['mac'] or 'unknown'}, IP {r['ip']}")
    return "\n".join(lines)


def _security_sentry_dismiss(arguments: dict[str, Any]) -> str:
    fingerprint = str(arguments.get("fingerprint") or "").strip()
    if not fingerprint:
        return "ERROR: security_sentry_dismiss requires a non-empty 'fingerprint'."
    ok = SentryStore(_SENTRY_DB).mark_false_positive(fingerprint)
    if not ok:
        return f"ERROR: no known finding with fingerprint {fingerprint!r} (run security_sentry_scan first)."
    return f"Marked {fingerprint} as a false positive -- it will not be reported as a new finding again."


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
        ),
    )
