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

import ipaddress
import os
import re
import subprocess
from typing import Any

_DEFAULT_TIMEOUT_S = 5.0


def _run(cmd: list[str], timeout: float = _DEFAULT_TIMEOUT_S) -> tuple[bool, str]:
    """Real subprocess call, argument-list only, always bounded. Returns
    (ok, stdout-or-honest-error-reason) — never raises."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
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
    # -n (finding #087): plain `arp -a` reverse-resolves every neighbour. On
    # this Mac's LAN (206 entries) that ran past the 5s timeout, so the whole
    # ARP view came back "unavailable" and new-device detection was blind;
    # `arp -an` answers in 0.1s and sends no PTR query per LAN device.
    # Hostnames are therefore always None here (a display label only; devices
    # are keyed by MAC, and a stored name is kept, see SentryStore).
    ok, out = _run(["arp", "-an"])
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
    addr = bind_addr.strip()
    if addr == "*":
        return "ALL_INTERFACES"
    if addr.lower() == "localhost":
        return "LOOPBACK_ONLY"
    # lsof prints IPv6 binds in brackets, and may add a zone id (fe80::1%en0).
    addr = addr.strip("[]").split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return "UNKNOWN"
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return "LOOPBACK_ONLY"
    if ip.is_unspecified:
        return "ALL_INTERFACES"
    if any(ip in net for net in _TAILSCALE_NETS):
        return "TAILSCALE"
    if any(ip in net for net in _LOCAL_NETS):
        return "LOCAL_NETWORK"
    return "UNKNOWN"


#: Tailscale's CGNAT range (100.64.0.0/10) and its IPv6 prefix; the rest of 100.x is public.
_TAILSCALE_NETS = tuple(ipaddress.ip_network(n) for n in ("100.64.0.0/10", "fd7a:115c:a1e0::/48"))
#: Private and link-local ranges (172.16.0.0/12 only, not all of 172.x).
_LOCAL_NETS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "fc00::/7", "fe80::/10"))


_LSOF_LISTEN_RE = re.compile(
    r"^(\S+)\s+(\d+)\s+(\S+)\s+\S+\s+(IPv4|IPv6)\s+\S+\s+\S*\s+(TCP|UDP)\s+(.+?):(\d+|\*)\s*(?:\(LISTEN\))?$"
)


# --------------------------------------------------------------------------- #
# Every user's sockets (SEC3-7). lsof run as the owner lists only the owner's
# own processes, so root daemons, socket-activated sharing services and root
# malware were invisible. macOS netstat -anv names the owning process of every
# socket without root ("process:pid" column), so its sockets are merged in.
# --------------------------------------------------------------------------- #

_NETSTAT = "/usr/sbin/netstat"
_PS = "/bin/ps"

#: Columns after "process:pid" in macOS netstat -anv: state options gencnt
#: flags flags1 usecnt rtncnt fltrs. The process name before them may contain
#: spaces and is cut to 16 characters.
_NETSTAT_FAMILY = {"tcp4": "IPv4", "tcp6": "IPv6", "tcp46": "IPv6"}

#: macOS services that launchd or a system daemon listens for. A listener on
#: one of these ports owned by one of these processes is named for the owner,
#: because "launchd listens on port 445" says nothing useful.
_SHARING = "System Settings > General > Sharing"
MAC_SERVICE_PORTS: dict[int, tuple[str, str]] = {
    22: ("Remote Login (SSH)", f"{_SHARING} > Remote Login"),
    88: ("Kerberos sign-in for Mac sharing (kdc)",
         f"{_SHARING} (it stops when File Sharing, Screen Sharing and Remote Management are all off)"),
    445: ("File Sharing (SMB)", f"{_SHARING} > File Sharing"),
    548: ("File Sharing (AFP)", f"{_SHARING} > File Sharing"),
    3031: ("Remote Application Scripting", f"{_SHARING} > Remote Application Scripting"),
    3283: ("Remote Management", f"{_SHARING} > Remote Management"),
    5900: ("Screen Sharing (VNC)", f"{_SHARING} > Screen Sharing"),
}
_SERVICE_OWNERS = frozenset({"launchd", "kdc", "sshd", "smbd", "screensharingd", "ARDAgent", "AppleFileServer"})

_PARTIAL_NOTE = ("Only this user's own programs could be read (netstat did not run: {reason}); "
                 "listeners and connections owned by the system or root are not shown.")


def _split_netstat_address(text: str) -> tuple[str, int | None]:
    """"192.168.1.226.51881" -> ("192.168.1.226", 51881); "*.88" -> ("*", 88);
    "::1.18789" -> ("::1", 18789); "*.*" -> ("*", None)."""
    addr, _, port = text.rpartition(".")
    return addr, (int(port) if port.isdigit() else None)


def parse_netstat_anv(text: str) -> list[dict[str, Any]] | None:
    """Every TCP socket in macOS `netstat -anv -p tcp` output, with the
    owning process for every user. None when the layout is not the one this
    parser knows (no "process:pid" column), so it is never guessed."""
    lines = text.splitlines()
    header = next((ln.split() for ln in lines if ln.startswith("Proto")), None)
    if not header or "process:pid" not in header:
        return None
    # "Local Address" and "Foreign Address" are two words in the header, one in a row.
    proc_col = header.index("process:pid") - 2
    trailing = len(header) - header.index("process:pid") - 1
    rows: list[dict[str, Any]] = []
    for line in lines:
        parts = line.split()
        if not parts or parts[0] not in _NETSTAT_FAMILY or len(parts) < proc_col + trailing:
            continue
        owner = " ".join(parts[proc_col:len(parts) - trailing])
        name, _, pid = owner.rpartition(":")
        local_addr, local_port = _split_netstat_address(parts[3])
        remote_addr, remote_port = _split_netstat_address(parts[4])
        rows.append({
            "family": _NETSTAT_FAMILY[parts[0]],
            "local_address": local_addr, "local_port": local_port,
            "remote_address": remote_addr, "remote_port": remote_port,
            "state": parts[5],
            "command": name if pid.isdigit() else None,
            "pid": int(pid) if pid.isdigit() else None,
        })
    return rows


def _current_user() -> str:
    import pwd

    return pwd.getpwuid(os.getuid()).pw_name


def _process_owners() -> dict[int, str]:
    ok, out = _run([_PS, "-axo", "pid=,user="], timeout=10.0)
    owners: dict[int, str] = {}
    if ok:
        for line in out.splitlines():
            pid, _, user = line.strip().partition(" ")
            if pid.isdigit() and user.strip():
                owners[int(pid)] = user.strip()
    return owners


def _all_users_sockets() -> tuple[list[dict[str, Any]] | None, str | None]:
    """(sockets, None) or (None, why the full view is missing)."""
    ok, out = _run([_NETSTAT, "-anv", "-p", "tcp"], timeout=10.0)
    if not ok:
        return None, _PARTIAL_NOTE.format(reason=out.strip()[:160] or "no output")
    rows = parse_netstat_anv(out)
    if rows is None:
        return None, _PARTIAL_NOTE.format(reason="its output layout was not recognised")
    return rows, None


def _bind_key(addr: str) -> str:
    return addr.strip("[]").split("%", 1)[0]


def _parse_lsof_listening(out: str) -> list[dict[str, Any]]:
    ports = []
    for line in out.splitlines()[1:]:  # skip the header row
        m = _LSOF_LISTEN_RE.match(line.strip())
        if not m:
            continue
        command, pid, user, family, proto, bind_addr, port = m.groups()
        entry: dict[str, Any] = {
            "command": command,
            "pid": int(pid),
            "user": user,
            "family": family,
            "protocol": proto,
            "bind_address": bind_addr,
            "port": None if port == "*" else int(port),
            "exposure": _classify_exposure(bind_addr),
        }
        _name_service(entry)
        ports.append(entry)
    return ports


def _name_service(entry: dict[str, Any]) -> None:
    """Adds the macOS sharing service a system listener belongs to, when it is one."""
    if entry["port"] in MAC_SERVICE_PORTS and entry["command"] in _SERVICE_OWNERS:
        entry["service"], entry["service_setting"] = MAC_SERVICE_PORTS[entry["port"]]


def get_listening_ports() -> dict[str, Any]:
    ok, out = _run(["lsof", "-iTCP", "-sTCP:LISTEN", "-n", "-P"], timeout=10.0)
    ports = _parse_lsof_listening(out) if ok else []
    sockets, gap = _all_users_sockets()
    if not ok and sockets is None:
        return _unavailable(out)
    if sockets is not None:
        # What lsof already reported stays exactly as it was (same names, so
        # existing baselines keep matching); netstat adds only what lsof missed.
        lsof_seen = {(p["pid"], p["port"]) for p in ports} | {(_bind_key(p["bind_address"]), p["port"]) for p in ports}
        added: set[tuple[Any, ...]] = set()
        owners, me = _process_owners(), _current_user()
        for s in sockets:
            if s["state"] != "LISTEN" or s["pid"] is None or s["local_port"] is None:
                continue
            if owners.get(s["pid"]) == me:  # lsof already lists the owner's own programs
                continue
            bind = _bind_key(s["local_address"])
            key = (s["pid"], s["local_port"], s["family"], bind)
            if (s["pid"], s["local_port"]) in lsof_seen or (bind, s["local_port"]) in lsof_seen or key in added:
                continue
            added.add(key)
            entry: dict[str, Any] = {
                "command": s["command"], "pid": s["pid"], "user": owners.get(s["pid"], "unknown"),
                "family": s["family"], "protocol": "TCP", "bind_address": s["local_address"],
                "port": s["local_port"], "exposure": _classify_exposure(s["local_address"]),
                "source": "netstat",
            }
            _name_service(entry)
            ports.append(entry)
    result: dict[str, Any] = {"available": True, "listening_ports": ports, "complete": sockets is not None}
    if not ok:
        result["gaps"] = [f"lsof did not run ({out.strip()[:160]}); netstat alone was used."]
    if gap:
        result["gaps"] = [gap]
    return result


# --------------------------------------------------------------------------- #
# Established connections (real outbound peers this host is genuinely
# talking to right now -- distinct from get_listening_ports's own LISTEN-
# state view. This is the real telemetry Phase 2 step 2 (threat-
# intelligence enrichment) enriches: a reputation lookup only makes sense
# against an IP this host is ACTUALLY connected to, never a scan target.
# --------------------------------------------------------------------------- #

_LSOF_ESTABLISHED_RE = re.compile(
    r"^(\S+)\s+(\d+)\s+(\S+)\s+\S+\s+(IPv4|IPv6)\s+\S+\s+\S*\s+(TCP)\s+"
    r"(.+?):(\d+)->(.+?):(\d+)\s*\(ESTABLISHED\)$"
)


def get_established_connections() -> dict[str, Any]:
    ok, out = _run(["lsof", "-iTCP", "-sTCP:ESTABLISHED", "-n", "-P"], timeout=10.0)
    connections = []
    for line in (out.splitlines()[1:] if ok else []):  # skip the header row
        m = _LSOF_ESTABLISHED_RE.match(line.strip())
        if not m:
            continue
        command, pid, user, family, proto, local_addr, local_port, remote_addr, remote_port = m.groups()
        connections.append({
            "command": command,
            "pid": int(pid),
            "user": user,
            "family": family,
            "protocol": proto,
            "local_address": local_addr.strip("[]"),
            "local_port": int(local_port),
            "remote_address": remote_addr.strip("[]"),
            "remote_port": int(remote_port),
        })
    sockets, gap = _all_users_sockets()
    if not ok and sockets is None:
        return _unavailable(out)
    if sockets is not None:
        # SEC3-7: connections of system and root processes, which lsof as the
        # owner cannot see; lsof's own rows keep their names unchanged.
        lsof_seen = {(c["pid"], c["local_port"], c["remote_port"]) for c in connections}
        owners, me = _process_owners(), _current_user()
        for s in sockets:
            if s["state"] != "ESTABLISHED" or s["pid"] is None or s["local_port"] is None or s["remote_port"] is None:
                continue
            # The owner's own programs come from lsof only, so a connection opened
            # between the two commands never appears under a second spelling of
            # the same program's name (lsof cuts names to 9 characters, netstat to 16).
            if owners.get(s["pid"]) == me:
                continue
            key = (s["pid"], s["local_port"], s["remote_port"])
            if key in lsof_seen:
                continue
            lsof_seen.add(key)
            connections.append({
                "command": s["command"], "pid": s["pid"], "user": owners.get(s["pid"], "unknown"),
                "family": s["family"], "protocol": "TCP",
                "local_address": s["local_address"], "local_port": s["local_port"],
                "remote_address": s["remote_address"], "remote_port": s["remote_port"],
                "source": "netstat",
            })
    result: dict[str, Any] = {"available": True, "connections": connections, "complete": sockets is not None}
    if not ok:
        result["gaps"] = [f"lsof did not run ({out.strip()[:160]}); netstat alone was used."]
    if gap:
        result["gaps"] = [gap]
    return result


# --------------------------------------------------------------------------- #
# Firewall
# --------------------------------------------------------------------------- #

def parse_firewall_state(text: str) -> bool | None:
    """From socketfilterfw --getglobalstate. "State = 0" is off; every other
    state is on, including 2, "block all incoming connections", which is
    stricter than 1. None when the output says neither."""
    m = re.search(r"State\s*=\s*(\d+)", text)
    if m:
        return m.group(1) != "0"
    low = text.lower()
    if "disabled" in low:
        return False
    if "enabled" in low or "block all" in low:
        return True
    return None


def get_firewall_status() -> dict[str, Any]:
    ok, out = _run(["/usr/libexec/ApplicationFirewall/socketfilterfw", "--getglobalstate"])
    if not ok:
        return _unavailable(out)
    return {"available": True, "enabled": bool(parse_firewall_state(out)), "raw": out.strip()}


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
        "established_connections": get_established_connections(),
        "firewall": get_firewall_status(),
    }
