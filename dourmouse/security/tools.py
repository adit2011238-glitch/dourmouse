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

from dourmouse.dispatch import Permission, Subagent, ToolSpec
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


def _format_lockdown(st: dict[str, Any]) -> str:
    lines = [f"Lockdown is {'ON' if st['active'] else 'off'}."]
    lines.append("Apps: " + (", ".join(a["name"] for a in st["apps"]) or "none"))
    sites = []
    for s in st["sites"]:
        mark = ""
        if "blocked_now" in s:
            mark = " (blocked now)" if s["blocked_now"] else " (NOT blocked right now)"
        note = f" [path {s['path_ignored']} cannot be blocked, whole site is]" if s.get("path_ignored") else ""
        sites.append(s["domain"] + mark + note)
    lines.append("Websites: " + (", ".join(sites) or "none"))
    lines.extend("Note: " + x for x in st["limits"])
    return "\n".join(lines)


def _lockdown_status(_arguments: dict[str, Any]) -> str:
    from . import lockdown

    return _format_lockdown(lockdown.status())


def _lockdown_edit(arguments: dict[str, Any]) -> str:
    from . import lockdown

    bl = lockdown.Blocklist.load()
    added, removed, errors = [], [], []
    for entry in arguments.get("add_sites") or []:
        try:
            added.append(bl.add_site(entry)["domain"])
        except ValueError as exc:
            errors.append(str(exc))
    for entry in arguments.get("add_apps") or []:
        added.append(bl.add_app(entry)["name"])
    for entry in arguments.get("remove") or []:
        if bl.remove(entry):
            removed.append(entry)
    bl.save()
    if bl.active:
        lockdown.write_hosts_request(bl)  # a running lockdown picks up the change
    out = []
    if added:
        out.append("Added: " + ", ".join(added))
    if removed:
        out.append("Removed: " + ", ".join(removed))
    out.extend("ERROR: " + e for e in errors)
    return "\n".join(out + ["", _format_lockdown(lockdown.status(bl))])


def _lockdown_start(_arguments: dict[str, Any]) -> str:
    from . import lockdown

    return _format_lockdown(lockdown.start())


def _lockdown_stop(_arguments: dict[str, Any]) -> str:
    from . import lockdown

    return _format_lockdown(lockdown.stop())


def _security_diagnose_connection(arguments: dict[str, Any]) -> str:
    from .connectivity import diagnose

    r = diagnose(str(arguments.get("target") or ""))
    lines = [f"{r['category']}: {r['why']}"]
    lines.extend(f"  {'ok ' if s['ok'] else 'FAIL'} {s['step']}: {s['detail']} ({s['ms']} ms)" for s in r["steps"])
    return "\n".join(lines)


def _security_report(_arguments: dict[str, Any]) -> str:
    from .report import build_report, save_report, to_markdown

    r = build_report()
    path = save_report(r)
    return to_markdown(r) + f"\n(saved to {path})"


def _security_analyze(_arguments: dict[str, Any]) -> str:
    from .analyst import analyze

    scan = run_scan(write_alerts=False)
    findings = [{"kind": f.kind, "severity": f.severity, "title": f.title, "detail": f.detail,
                 "recommended_action": f.recommended_action} for f in scan.all_findings]
    r = analyze(findings)
    if not r["ok"]:
        return "The analyst could not run: " + r["error"]
    lines = [f"Worry level: {r['worry']}", r["summary"], ""]
    for p in r["points"]:
        lines.append(f"- About {', '.join(p['titles'])}: {p['meaning']}")
        if p["first_step"]:
            lines.append(f"  First step: {p['first_step']}")
    if r["dropped"]:
        lines.append(f"({r['dropped']} point(s) dropped: they did not cite a real finding.)")
    return "\n".join(lines)


def _respond(fn: Any) -> str:
    from .response import ResponseRefused

    try:
        r = fn()
    except ResponseRefused as exc:
        return f"Not done: {exc}"
    if r.get("needs_root"):
        cmds = r["needs_root"] if isinstance(r["needs_root"], list) else [r["needs_root"]]
        return r["note"] + "\n" + "\n".join(cmds)
    return "Done: " + ", ".join(f"{k}={v}" for k, v in r.items() if k in (
        "name", "pid", "result", "original_path", "now_at", "id", "restored_to", "path", "quarantine_id"))


def _security_kill_process(a: dict[str, Any]) -> str:
    from .response import kill_process

    return _respond(lambda: kill_process(int(a["pid"]), expect_name=a.get("expect_name") or None))


def _security_quarantine_file(a: dict[str, Any]) -> str:
    from .response import quarantine_file

    return _respond(lambda: quarantine_file(str(a["path"]), reason=str(a.get("reason") or "")))


def _security_restore(a: dict[str, Any]) -> str:
    from .response import restore

    return _respond(lambda: restore(str(a["id"])))


def _security_disable_startup_item(a: dict[str, Any]) -> str:
    from .response import disable_startup_item

    return _respond(lambda: disable_startup_item(str(a["path"]), reason=str(a.get("reason") or "")))


def _security_quarantine_list(_a: dict[str, Any]) -> str:
    from .response import list_quarantine

    items = list_quarantine()
    if not items:
        return "Quarantine is empty."
    return "\n".join(f"{m['id']}: {m['original_path']} ({m.get('reason') or 'no reason given'})" for m in items)


def _security_block_domain(a: dict[str, Any]) -> str:
    from . import lockdown

    try:
        st = lockdown.block_domain_always(str(a["domain"]), str(a.get("reason") or ""))
    except ValueError as exc:
        return f"Not done: {exc}"
    return _format_lockdown(st) + "\nBlocked for good: " + ", ".join(s["domain"] for s in st["always_blocked"])


def _security_unblock_domain(a: dict[str, Any]) -> str:
    from . import lockdown

    st = lockdown.unblock_domain_always(str(a["domain"]))
    return "Blocked for good now: " + (", ".join(s["domain"] for s in st["always_blocked"]) or "nothing")


def _security_browser_history(a: dict[str, Any]) -> str:
    from . import browser_history as bh
    from . import lockdown
    from .privacy import privacy_mode

    hours = float(a.get("hours") or 24)
    r = bh.recent_history(hours=hours)
    bl = lockdown.Blocklist.load()
    blocked = {s["domain"] for s in bl.always} | ({s["domain"] for s in bl.sites} if bl.active else set())
    hits = bh.blocked_visits(r["visits"], blocked)
    lines = [f"Last {hours:g} h: {len(r['visits'])} visit(s), {len(r['searches'])} typed search(es)."]
    lines += [f"  {s['source']}: {s['status']} ({s['detail']})" for s in r["sources"] if s["status"] != "read" or s["detail"] != "0 visit(s)"]
    lines.append(f"Visits to blocked domains: {len(hits)}")
    if privacy_mode():  # finding #112: no URLs or search terms leave this Mac
        lines.append("Privacy mode is on: URLs, domains and search terms are not shown to the chat.")
        return "\n".join(lines)
    lines += [f"  {v['browser']}: {v['domain']}" for v in hits[:20]]
    lines.append("Most visited: " + ", ".join(f"{d} ({n})" for d, n in bh.top_domains(r["visits"])))
    if r["searches"]:
        lines.append("Recent searches: " + "; ".join(s["term"] for s in r["searches"][:15]))
    return "\n".join(lines)


def _security_self_audit(_a: dict[str, Any]) -> str:
    from .self_audit import run_self_audit

    r = run_self_audit()
    lines = [f"Privacy mode: {'on' if r['privacy_mode'] else 'off'}"]
    if not r["findings"]:
        lines.append("Dourmouse's own security: nothing wrong in what was checked.")
    for f in r["findings"]:
        lines += [f"[{f['severity']}] {f['title']}", f"  {f['detail']}", f"  Fix: {f['fix']}"]
    lines.append("Checked: " + "; ".join(r["checked"]))
    lines.append("Not checked: " + "; ".join(r["not_checked"]))
    return "\n".join(lines)


def _security_privacy_mode(a: dict[str, Any]) -> str:
    from .privacy import set_privacy_mode

    on = bool(a.get("on"))
    set_privacy_mode(on)
    return ("Privacy mode ON: the analyst will not send findings to the cloud model, and browser history "
            "stays on this Mac." if on else "Privacy mode OFF.")


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
                name="lockdown_status",
                description="Show the lockdown blocklist (apps and websites), whether lockdown is on, and whether each site is really blocked right now.",
                parameters={"type": "object", "properties": {}},
                handler=_lockdown_status,
            ),
            ToolSpec(
                name="lockdown_edit",
                description=(
                    "Add or remove entries on the lockdown blocklist. Websites can be URLs or domains (the whole "
                    "domain is blocked); apps by name (e.g. 'Discord'), bundle id or path. Does not start lockdown."
                ),
                parameters={"type": "object", "properties": {
                    "add_sites": {"type": "array", "items": {"type": "string"}},
                    "add_apps": {"type": "array", "items": {"type": "string"}},
                    "remove": {"type": "array", "items": {"type": "string"}},
                }},
                handler=_lockdown_edit,
            ),
            ToolSpec(
                name="lockdown_start",
                description="Start lockdown: every app and website on the blocklist becomes unopenable until lockdown_stop.",
                parameters={"type": "object", "properties": {}},
                handler=_lockdown_start,
                permission=Permission.REQUIRES_CONFIRMATION,
                confirm_prompt=lambda a: "Start lockdown now? Everything on the blocklist will be closed and blocked until you end it.",
            ),
            ToolSpec(
                name="lockdown_stop",
                description="End lockdown: blocklisted apps and websites open normally again.",
                parameters={"type": "object", "properties": {}},
                handler=_lockdown_stop,
                permission=Permission.REQUIRES_CONFIRMATION,
                confirm_prompt=lambda a: "End lockdown now? Blocked apps and websites will open normally again.",
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
            ToolSpec(
                name="security_diagnose_connection",
                description=(
                    "Diagnose why a website or service will not load: walks name lookup, connection, TLS and HTTP and "
                    "names exactly one cause (OK, BLOCKED, DNS, ROUTING, UNREACHABLE, TIMEOUT, TLS, SERVER, UNKNOWN) "
                    "with the evidence for each step."
                ),
                parameters={"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]},
                handler=_security_diagnose_connection,
            ),
            ToolSpec(
                name="security_report",
                description=(
                    "The one-button security report for this Mac: posture by area (never one magic number), every "
                    "finding with what to do, monitoring indicators, downloads, lockdown, and what could not be "
                    "checked. Kept on this Mac for later."
                    # Not "saved to the workspace": the planner's routing scorer
                    # read that as write intent and tied this agent with
                    # dev_coding on "save it to a file" (finding #110).
                ),
                parameters={"type": "object", "properties": {}},
                handler=_security_report,
            ),
            ToolSpec(
                name="security_analyze",
                description=(
                    "Ask the security analyst to explain the current findings together in plain English: how worried "
                    "to be and what to do first. It only explains what the detectors found; it never adds findings."
                ),
                parameters={"type": "object", "properties": {}},
                handler=_security_analyze,
            ),
            ToolSpec(
                name="security_kill_process",
                description="Stop a running process by pid (SIGTERM, then SIGKILL). Refuses macOS's own core processes.",
                parameters={"type": "object", "properties": {
                    "pid": {"type": "integer"},
                    "expect_name": {"type": "string", "description": "the process name you expect; refused if the pid now belongs to something else"},
                }, "required": ["pid"]},
                handler=_security_kill_process,
                permission=Permission.REQUIRES_CONFIRMATION,
                confirm_prompt=lambda a: f"Stop process {a.get('pid')} ({a.get('expect_name') or 'name not given'})?",
            ),
            ToolSpec(
                name="security_quarantine_file",
                description="Move a suspicious file or app into quarantine (kept intact and restorable) and make it non-runnable.",
                parameters={"type": "object", "properties": {"path": {"type": "string"}, "reason": {"type": "string"}},
                            "required": ["path"]},
                handler=_security_quarantine_file,
                permission=Permission.REQUIRES_CONFIRMATION,
                confirm_prompt=lambda a: f"Move {a.get('path')} into quarantine? It can be restored later.",
            ),
            ToolSpec(
                name="security_disable_startup_item",
                description="Stop a launch agent from running at login: unloads it and quarantines its plist (restorable).",
                parameters={"type": "object", "properties": {"path": {"type": "string"}, "reason": {"type": "string"}},
                            "required": ["path"]},
                handler=_security_disable_startup_item,
                permission=Permission.REQUIRES_CONFIRMATION,
                confirm_prompt=lambda a: f"Disable the startup item {a.get('path')}? It can be restored later.",
            ),
            ToolSpec(
                name="security_quarantine_list",
                description="List what is in quarantine, with each item's id, original location and reason.",
                parameters={"type": "object", "properties": {}},
                handler=_security_quarantine_list,
            ),
            ToolSpec(
                name="security_restore",
                description="Put a quarantined file or startup item back where it was, by quarantine id.",
                parameters={"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
                handler=_security_restore,
                permission=Permission.REQUIRES_CONFIRMATION,
                confirm_prompt=lambda a: f"Restore quarantined item {a.get('id')} to its original place?",
            ),
            ToolSpec(
                name="security_block_domain",
                description="Block a dangerous domain for good (stays blocked with lockdown off), e.g. a phishing or malware site.",
                parameters={"type": "object", "properties": {"domain": {"type": "string"}, "reason": {"type": "string"}},
                            "required": ["domain"]},
                handler=_security_block_domain,
                permission=Permission.REQUIRES_CONFIRMATION,
                confirm_prompt=lambda a: f"Block {a.get('domain')} on this Mac until you unblock it?",
            ),
            ToolSpec(
                name="security_browser_history",
                description=(
                    "Browsing history and the exact queries typed into Chrome (all profiles), Arc, Brave, Edge, "
                    "Safari (needs Full Disk Access) and Firefox, read locally, with visits to blocked domains "
                    "flagged. Honours privacy mode."
                ),
                parameters={"type": "object", "properties": {"hours": {"type": "number", "default": 24}}},
                handler=_security_browser_history,
            ),
            ToolSpec(
                name="security_self_audit",
                description=(
                    "Dourmouse checks its own security: where it listens, auto-approve, key file permissions, "
                    ".env in git, the root lockdown helper's integrity, and private folders."
                ),
                parameters={"type": "object", "properties": {}},
                handler=_security_self_audit,
            ),
            ToolSpec(
                name="security_privacy_mode",
                description="Turn privacy mode on or off: when on, security data about this Mac is not sent to the cloud model.",
                parameters={"type": "object", "properties": {"on": {"type": "boolean"}}, "required": ["on"]},
                handler=_security_privacy_mode,
                permission=Permission.REQUIRES_CONFIRMATION,
                confirm_prompt=lambda a: f"Turn privacy mode {'on' if a.get('on') else 'off'}?",
            ),
            ToolSpec(
                name="security_unblock_domain",
                description="Remove a domain from the permanent security block list.",
                parameters={"type": "object", "properties": {"domain": {"type": "string"}}, "required": ["domain"]},
                handler=_security_unblock_domain,
                permission=Permission.REQUIRES_CONFIRMATION,
                confirm_prompt=lambda a: f"Unblock {a.get('domain')}?",
            ),
        ),
    )
