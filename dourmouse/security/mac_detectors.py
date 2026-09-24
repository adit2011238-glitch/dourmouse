"""Mac detectors (MS-3, spec items 8, 10, 11, 13, 14, 15; finding #100).

Pure and deterministic like sentry._detect_findings: the same telemetry and
the same baseline anomalies always produce the same findings, with no I/O,
clock or model. Two kinds of rule:

- **Posture**, checked every scan: the Mac's own protections (FileVault, SIP,
  Gatekeeper, stealth mode, Remote Login, Screen Sharing), the Wi-Fi link's
  security, ARP evidence of spoofing, programs running out of Downloads or a
  temp folder while talking to the network.
- **Change**, from the baseline engine and only after its learning period: the
  router's MAC changed on the same network, DNS resolvers changed, a new
  listening port, a new or modified persistence item, a protection that was
  on is now off, a new unsigned program talking to the network.

Noise discipline for a developer's Mac: Homebrew and virtualenv binaries are
ad hoc signed, so "unsigned program on the network" alone would fire all day.
An unsigned or ad hoc program is reported only when it is NEW to the network
after learning, or when it runs from Downloads or a temp folder.
"""

from __future__ import annotations

from typing import Any

from .baseline import HOST, Anomaly
from .mac_telemetry import WEAK_WIFI
from .sentry import SentryFinding, _fingerprint

_NOTE = " Never applied automatically; this is a suggestion only."

_PROTECTION_TEXT = {
    "filevault": ("high", "FileVault disk encryption is off",
                  "Anyone with the Mac in hand can read the disk.",
                  "Turn it on in System Settings > Privacy & Security > FileVault."),
    "sip": ("high", "System Integrity Protection is disabled",
            "System files and protected apps can be modified by any root process.",
            "Re-enable it from Recovery: `csrutil enable`, then restart."),
    "gatekeeper": ("high", "Gatekeeper assessments are disabled",
                   "Unsigned and unnotarized apps open without any check.",
                   "Run `sudo spctl --master-enable` yourself, or set it in System Settings."),
    "stealth_mode": ("low", "Firewall stealth mode is off",
                     "The Mac answers probes (ping, closed-port replies) on every network it joins.",
                     "Run `sudo /usr/libexec/ApplicationFirewall/socketfilterfw --setstealthmode on` yourself."),
}


def _finding(kind: str, severity: str, title: str, detail: str, action: str, *key: str) -> SentryFinding:
    return SentryFinding(
        fingerprint=_fingerprint(kind, *key), kind=kind, severity=severity, title=title,
        detail=detail, recommended_action=action + _NOTE,
    )


def _posture(state: dict[str, Any]) -> list[SentryFinding]:
    out: list[SentryFinding] = []
    checks = (state.get("host_protections") or {}).get("checks") or {}
    for name, (sev, title, detail, action) in _PROTECTION_TEXT.items():
        if (checks.get(name) or {}).get("on") is False:
            out.append(_finding(f"{name}_off", sev, title, detail, action))
    for name, label, port, how in (
        ("remote_login", "Remote Login (SSH)", 22, "System Settings > General > Sharing > Remote Login"),
        ("screen_sharing", "Screen Sharing", 5900, "System Settings > General > Sharing > Screen Sharing"),
    ):
        c = checks.get(name) or {}
        if c.get("on") and c.get("accepting"):
            out.append(_finding(
                f"{name}_on", "med", f"{label} is on and accepting connections",
                f"Port {port} accepts connections, so anyone who can reach this Mac on a network can try to "
                "log in to it. With the firewall off, that includes every network it joins.",
                f"If you do not use it, turn it off in {how}.",
            ))

    wifi = state.get("wifi") or {}
    sec = wifi.get("security")
    if wifi.get("connected") and sec in WEAK_WIFI:
        sev = "high" if sec in ("open", "wep") else "med"
        out.append(_finding(
            "weak_wifi", sev, f"Connected to a {sec.replace('_', ' ')} Wi-Fi network",
            "Traffic on this network can be read or tampered with by others nearby"
            + (" (no encryption at all)." if sec == "open" else " (broken or legacy encryption)."),
            "Use a VPN on this network, or switch to a WPA2/WPA3 network.", sec,
        ))

    gw = (state.get("default_gateway") or {}).get("gateway")
    neighbors = (state.get("arp_neighbors") or {}).get("neighbors") or []
    gw_mac = next((n.get("mac") for n in neighbors if n.get("ip") == gw), None)
    if gw and gw_mac:
        others = sorted(n["ip"] for n in neighbors if n.get("mac") == gw_mac and n.get("ip") != gw)
        if others:
            out.append(_finding(
                "arp_gateway_duplicate", "high", "Another device claims the router's hardware address",
                f"The router {gw} and {', '.join(others)} report the same MAC {gw_mac}. That is the "
                "signature of ARP spoofing, where a device on the network intercepts traffic meant for the router.",
                "Disconnect from this network, then check the router's client list for the extra device.", gw_mac,
            ))

    for proc in state.get("network_processes") or []:
        loc = proc.get("location")
        if loc in ("downloads", "temporary"):
            out.append(_finding(
                "network_process_suspicious_location", "high",
                f"{proc.get('name')} is talking to the network from a {loc} folder",
                f"{proc.get('exe')} (pid {proc.get('pid')}, started by {proc.get('parent_name')}) has open "
                f"network connections. Legitimate apps rarely run from {loc}; malware often does.",
                f"Check what it is before anything else; quarantining the file ({proc.get('exe')}) is available "
                "through the approval gate.", proc.get("exe") or str(proc.get("pid")),
            ))
    return out


def _changes(anomalies: list[Anomaly], state: dict[str, Any]) -> list[SentryFinding]:
    out: list[SentryFinding] = []
    by_name = {p.get("name"): p for p in state.get("network_processes") or []}
    persistence = {i["path"]: i for i in (state.get("persistence") or {}).get("items", []) if "path" in i}
    for a in anomalies:
        o = a.observation
        if o.category == "gateway_mac" and a.kind == "changed":
            out.append(_finding(
                "gateway_mac_changed", "high", "The router's hardware address changed on this network",
                f"The router {o.key} used to answer as {a.previous_value}; now it answers as {o.value}. A replaced "
                "router, or a different network that happens to use the same router address, explains this; "
                "otherwise it is the classic sign of ARP spoofing (a device impersonating the router).",
                "If you did not replace the router, disconnect and investigate the device with the new address.",
                o.scope, o.value,
            ))
        elif o.category == "dns_resolvers" and a.kind == "changed":
            out.append(_finding(
                "dns_changed", "med", "DNS servers changed on this network",
                f"Resolvers were {a.previous_value}, now {o.value}. Whoever runs DNS can redirect names to any address.",
                "Confirm the new servers are expected (router update, VPN) before trusting lookups on this network.",
                o.scope, o.value,
            ))
        elif o.category == "wifi_security" and a.kind == "changed":
            out.append(_finding(
                "wifi_security_changed", "med", "This network's Wi-Fi security changed",
                f"It was {a.previous_value}, now {o.value}. A downgrade can mean an impostor access point with the same name.",
                "Check the router's settings; if you did not change them, treat the network as untrusted.",
                o.scope, o.value,
            ))
        elif o.category == "protection" and a.kind == "changed" and o.value == "off":
            out.append(_finding(
                "protection_turned_off", "high", f"{o.key.replace('_', ' ')} was on and is now off",
                "A protection this Mac had is gone. Malware and remote-access tools often disable protections first.",
                "Turn it back on unless you switched it off yourself.", o.key,
            ))
        elif o.category == "listening_port" and a.kind in ("new", "changed"):
            command, proto, port = (o.key.split("|") + ["", "", ""])[:3]
            out.append(_finding(
                "new_listening_port", "med", f"{command} started listening on {proto} port {port}",
                f"This port was not open before ({o.value.lower() or 'exposure unknown'}). New listeners are how "
                "backdoors and forgotten dev servers become reachable.",
                f"If you did not start {command}, stop it; if you did, bind it to 127.0.0.1.", o.key, o.value,
            ))
        elif o.category == "persistence":
            item = persistence.get(o.key, {})
            prog = item.get("program") or "unknown program"
            risky = any(s in prog for s in ("/tmp/", "/Downloads/", "/Users/Shared/")) or "/." in prog  # noqa: S108 -- classifying a path, not using it
            if a.kind == "new":
                out.append(_finding(
                    "new_persistence", "high" if risky else "med",
                    f"New item set to start automatically: {item.get('label') or o.key}",
                    f"{o.key} launches {prog}{' at login' if item.get('run_at_load') else ''}. Surviving a restart "
                    "is the first thing malware sets up." + (" It runs from a hidden or temporary location." if risky else ""),
                    f"If you did not install it, disable it (move {o.key} aside) through the approval gate.", o.key,
                ))
            elif a.kind == "changed":
                out.append(_finding(
                    "persistence_modified", "med", f"A startup item was modified: {item.get('label') or o.key}",
                    f"{o.key} changed content since it was last seen; it now launches {prog}.",
                    "Check that the change came from an app update you expect.", o.key, o.value,
                ))
        elif o.category == "network_process" and a.kind == "new":
            proc = by_name.get(o.key) or {}
            sig = proc.get("signature") or {}
            if sig.get("kind") in ("unsigned", "adhoc", "unknown") and proc.get("location") not in ("downloads", "temporary"):
                out.append(_finding(
                    "new_unsigned_network_process", "med", f"A new unsigned program is talking to the network: {o.key}",
                    f"{proc.get('exe') or o.key} ({sig.get('kind')} signature, Gatekeeper: "
                    f"{(sig.get('gatekeeper') or {}).get('verdict', 'unknown')}) opened network connections for the "
                    "first time since the baseline was learned.",
                    "Confirm you started it (a script or developer tool is common); otherwise stop it.", o.key,
                ))
    return out


def detect_mac_findings(state: dict[str, Any], anomalies: list[Anomaly]) -> list[SentryFinding]:
    """``anomalies`` must already exclude scopes that are still learning."""
    return _posture(state) + _changes(anomalies, state)


__all__ = ["HOST", "detect_mac_findings"]
