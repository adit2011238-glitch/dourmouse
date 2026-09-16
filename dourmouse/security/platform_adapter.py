"""Cross-platform security telemetry — the foundational layer Phase 4
(docs/GODSPEED_ROADMAP.md) builds on. macOS implemented first (this
machine's own real platform); Windows/Linux adapters are real, tracked
follow-on work, not stubbed here with fabricated data.

Every operation returns a structured, honest result. A command that
isn't available, times out, or can't be parsed returns
``{"available": False, "reason": "..."}`` — never a fabricated value,
matching this codebase's own established "NOT CONFIGURED" honesty
convention elsewhere (Rule 2.1/2.2). Every subprocess call uses an
argument list (never ``shell=True``) and a real timeout, so one hung
command can never freeze a security scan (see docs/ENGINEERING_AUDIT.md's
shell/subprocess audit item).

Deliberately narrow scope for this pass: real telemetry gathering only.
Baselines, anomaly detection, the event schema, AI sentries, and the
dashboard UI are separate, not-yet-built layers on top of this one (see
docs/GODSPEED_ROADMAP.md Phase 4).
"""

from __future__ import annotations

import re
import subprocess
from typing import Any

_DEFAULT_TIMEOUT_S = 5.0


def _run(cmd: list[str], timeout: float = _DEFAULT_TIMEOUT_S) -> tuple[bool, str]:
    """Real subprocess call, argument-list only, always bounded. Returns
    (ok, stdout-or-honest-error-reason) — never raises."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return False, f"{cmd[0]} is not installed on this system"
    except subprocess.TimeoutExpired:
        return False, f"{cmd[0]} timed out after {timeout}s"
    except OSError as exc:
        return False, f"{cmd[0]} failed to start: {exc}"
    if proc.returncode != 0 and not proc.stdout.strip():
        return False, (proc.stderr or f"{cmd[0]} exited {proc.returncode}").strip()
    return True, proc.stdout


def _unavailable(reason: str) -> dict[str, Any]:
    return {"available": False, "reason": reason}


# --------------------------------------------------------------------------- #
# Interfaces
# --------------------------------------------------------------------------- #

_IFACE_HEADER_RE = re.compile(r"^(\S+?):\s+flags=\d+<([^>]*)>\s+mtu\s+(\d+)")
_ETHER_RE = re.compile(r"^ether\s+([0-9a-fA-F:]+)")
_INET_P2P_RE = re.compile(r"^inet\s+(\S+)\s+-->\s+(\S+)\s+netmask\s+(\S+)")
_INET_RE = re.compile(r"^inet\s+(\S+)\s+netmask\s+(\S+)(?:\s+broadcast\s+(\S+))?")
_INET6_RE = re.compile(r"^inet6\s+(\S+)")
_STATUS_RE = re.compile(r"^status:\s+(\S+)")


def _parse_ifconfig(output: str) -> list[dict[str, Any]]:
    interfaces: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw_line in output.splitlines():
        if raw_line and not raw_line[0].isspace():
            m = _IFACE_HEADER_RE.match(raw_line)
            if m:
                current = {
                    "name": m.group(1),
                    "flags": [f for f in m.group(2).split(",") if f],
                    "mtu": int(m.group(3)),
                    "mac": None,
                    "ipv4": [],
                    "ipv6": [],
                    "status": None,
                }
                interfaces.append(current)
            else:
                current = None
            continue
        if current is None:
            continue
        line = raw_line.strip()
        m = _ETHER_RE.match(line)
        if m:
            current["mac"] = m.group(1)
            continue
        m = _INET_P2P_RE.match(line)
        if m:
            current["ipv4"].append({"address": m.group(1), "peer": m.group(2), "netmask": m.group(3)})
            continue
        m = _INET_RE.match(line)
        if m:
            current["ipv4"].append({"address": m.group(1), "netmask": m.group(2), "broadcast": m.group(3)})
            continue
        m = _INET6_RE.match(line)
        if m:
            current["ipv6"].append(m.group(1).split("%")[0])
            continue
        m = _STATUS_RE.match(line)
        if m:
            current["status"] = m.group(1)
            continue
    return interfaces


def get_interfaces() -> dict[str, Any]:
    ok, out = _run(["ifconfig"])
    if not ok:
        return _unavailable(out)
    interfaces = _parse_ifconfig(out)
    return {"available": True, "interfaces": interfaces}


# --------------------------------------------------------------------------- #
# Default gateway
# --------------------------------------------------------------------------- #

def get_default_gateway() -> dict[str, Any]:
    ok, out = _run(["route", "-n", "get", "default"])
    if not ok:
        return _unavailable(out)
    gateway = interface = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("gateway:"):
            gateway = line.split(":", 1)[1].strip()
        elif line.startswith("interface:"):
            interface = line.split(":", 1)[1].strip()
    if gateway is None:
        return _unavailable("no default route configured")
    return {"available": True, "gateway": gateway, "interface": interface}


# --------------------------------------------------------------------------- #
# DNS
# --------------------------------------------------------------------------- #

_RESOLVER_SPLIT_RE = re.compile(r"\nresolver #\d+\n")
_NAMESERVER_RE = re.compile(r"^nameserver\[\d+\]\s*:\s*(.+)$")
_SEARCH_DOMAIN_RE = re.compile(r"^search domain\[\d+\]\s*:\s*(.+)$")
_SIMPLE_FIELD_RE = re.compile(r"^(domain|if_index|order|flags)\s*:\s*(.+)$")


def _parse_scutil_dns(output: str) -> list[dict[str, Any]]:
    resolvers: list[dict[str, Any]] = []
    for block in _RESOLVER_SPLIT_RE.split("\n" + output)[1:]:
        entry: dict[str, Any] = {"nameservers": [], "search_domains": []}
        for raw_line in block.splitlines():
            line = raw_line.strip()
            if not line:
                break
            m = _NAMESERVER_RE.match(line)
            if m:
                entry["nameservers"].append(m.group(1).strip())
                continue
            m = _SEARCH_DOMAIN_RE.match(line)
            if m:
                entry["search_domains"].append(m.group(1).strip())
                continue
            m = _SIMPLE_FIELD_RE.match(line)
            if m:
                entry[m.group(1)] = m.group(2).strip()
        resolvers.append(entry)
    return resolvers


def get_dns_configuration() -> dict[str, Any]:
    ok, out = _run(["scutil", "--dns"])
    if not ok:
        return _unavailable(out)
    resolvers = _parse_scutil_dns(out)
    return {"available": True, "resolvers": resolvers}


# --------------------------------------------------------------------------- #
# ARP / local neighbors
# --------------------------------------------------------------------------- #

_ARP_RE = re.compile(r"^(\S+)\s+\(([\d.]+)\)\s+at\s+(\S+)\s+on\s+(\S+)")


def get_arp_neighbors() -> dict[str, Any]:
    ok, out = _run(["arp", "-a"])
    if not ok:
        return _unavailable(out)
    neighbors = []
    for line in out.splitlines():
        m = _ARP_RE.match(line.strip())
        if not m:
            continue
        hostname, ip, mac, iface = m.groups()
        neighbors.append({
            "hostname": None if hostname == "?" else hostname,
            "ip": ip,
            "mac": None if mac == "(incomplete)" else mac,
            "interface": iface,
        })
    return {"available": True, "neighbors": neighbors}


# --------------------------------------------------------------------------- #
# Listening ports / host exposure
# --------------------------------------------------------------------------- #

def _classify_exposure(bind_addr: str) -> str:
    """Spec's own exposure taxonomy: a service bound to loopback can
    only ever be reached from this machine; 0.0.0.0/`*` is reachable
    from anywhere those interfaces reach, including the LAN and any
    Tailscale peer — a materially different risk, worth surfacing
    honestly rather than lumping every listening port together."""
    if bind_addr in ("127.0.0.1", "::1", "localhost"):
        return "LOOPBACK_ONLY"
    if bind_addr in ("*", "0.0.0.0", "::"):
        return "ALL_INTERFACES"
    if bind_addr.startswith("100.") or bind_addr.startswith("fd7a:"):
        return "TAILSCALE"
    if bind_addr.startswith(("192.168.", "10.", "172.")):
        return "LOCAL_NETWORK"
    return "UNKNOWN"


_LSOF_LISTEN_RE = re.compile(
    r"^(\S+)\s+(\d+)\s+(\S+)\s+\S+\s+(IPv4|IPv6)\s+\S+\s+\S*\s+(TCP|UDP)\s+(.+?):(\d+|\*)\s*(?:\(LISTEN\))?$"
)


def get_listening_ports() -> dict[str, Any]:
    ok, out = _run(["lsof", "-iTCP", "-sTCP:LISTEN", "-n", "-P"], timeout=10.0)
    if not ok:
        return _unavailable(out)
    ports = []
    for line in out.splitlines()[1:]:  # skip the header row
        m = _LSOF_LISTEN_RE.match(line.strip())
        if not m:
            continue
        command, pid, user, family, proto, bind_addr, port = m.groups()
        ports.append({
            "command": command,
            "pid": int(pid),
            "user": user,
            "family": family,
            "protocol": proto,
            "bind_address": bind_addr,
            "port": None if port == "*" else int(port),
            "exposure": _classify_exposure(bind_addr),
        })
    return {"available": True, "listening_ports": ports}


# --------------------------------------------------------------------------- #
# Firewall
# --------------------------------------------------------------------------- #

def get_firewall_status() -> dict[str, Any]:
    ok, out = _run(["/usr/libexec/ApplicationFirewall/socketfilterfw", "--getglobalstate"])
    if not ok:
        return _unavailable(out)
    enabled = "State = 1" in out or "Firewall is enabled" in out
    return {"available": True, "enabled": enabled, "raw": out.strip()}


# --------------------------------------------------------------------------- #
# Bundled snapshot
# --------------------------------------------------------------------------- #

def get_system_security_state() -> dict[str, Any]:
    """One real, honest snapshot — every field independently reports its
    own availability rather than one failure blanking the whole result."""
    return {
        "interfaces": get_interfaces(),
        "default_gateway": get_default_gateway(),
        "dns": get_dns_configuration(),
        "arp_neighbors": get_arp_neighbors(),
        "listening_ports": get_listening_ports(),
        "firewall": get_firewall_status(),
    }
