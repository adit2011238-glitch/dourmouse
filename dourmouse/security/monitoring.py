""""Am I being monitored?" indicators (MS-5, spec item 24, finding #102).

Technically honest by construction: each indicator is PRESENT, ABSENT or
UNKNOWN, with the evidence it was read from, what it would mean, and how much
it can and cannot tell. None of them proves monitoring (a company laptop is
legitimately managed; a VPN is usually your own), and absence of all of them
does not prove the opposite (Full Disk Access and root would reveal more,
and the UNKNOWN rows say exactly what). The analyzer never guesses.

Indicators read on macOS, without root:
  proxies (system HTTP/HTTPS/SOCKS and PAC), MDM enrollment, configuration
  profiles for this user, extra trusted root certificates (TLS interception),
  VPN connections and network/endpoint-security system extensions, remote
  access services (Remote Login, Screen Sharing, Remote Management), and
  known remote-control software running or installed to start.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import mac_telemetry as mt

PRESENT, ABSENT, UNKNOWN = "present", "absent", "unknown"

REMOTE_CONTROL_APPS = {
    "teamviewer": "TeamViewer", "anydesk": "AnyDesk", "splashtop": "Splashtop", "logmein": "LogMeIn",
    "rustdesk": "RustDesk", "screenconnect": "ScreenConnect", "connectwise": "ConnectWise",
    "chrome remote desktop": "Chrome Remote Desktop", "remotedesktop": "Apple Remote Desktop",
    "gotomypc": "GoToMyPC", "parsec": "Parsec", "realvnc": "RealVNC", "vnc server": "VNC Server",
    "ammyy": "Ammyy Admin", "supremo": "Supremo", "zoho assist": "Zoho Assist",
}


@dataclass
class Indicator:
    name: str
    status: str  # present | absent | unknown
    evidence: str
    meaning: str
    confidence: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_proxies(output: str) -> dict[str, Any]:
    """`scutil --proxy`. Enabled flags are 1; a PAC URL or HTTP(S)/SOCKS host
    set and enabled routes traffic through someone else's server."""
    def val(k: str) -> str | None:
        m = re.search(rf"^\s*{k}\s*:\s*(.+)$", output, re.M)
        return m.group(1).strip() if m else None

    active = {}
    for kind, en, host, port in (("http", "HTTPEnable", "HTTPProxy", "HTTPPort"),
                                 ("https", "HTTPSEnable", "HTTPSProxy", "HTTPSPort"),
                                 ("socks", "SOCKSEnable", "SOCKSProxy", "SOCKSPort")):
        if val(en) == "1":
            active[kind] = f"{val(host)}:{val(port)}"
    if val("ProxyAutoConfigEnable") == "1":
        active["pac"] = val("ProxyAutoConfigURLString") or "(no URL)"
    if val("ProxyAutoDiscoveryEnable") == "1":
        active["auto_discovery"] = "on"
    return active


def parse_nc_list(output: str) -> list[dict[str, Any]]:
    """`scutil --nc list`: configured VPNs and whether each is connected."""
    out = []
    for line in output.splitlines():
        m = re.match(r'^\*?\s*\((\w[\w ]*)\)\s+\S+\s+(\S+)\s+\(([^)]*)\)\s+"([^"]*)"', line.strip())
        if m:
            out.append({"state": m.group(1), "type": m.group(2), "provider": m.group(3), "name": m.group(4)})
    return out


def parse_system_extensions(output: str) -> list[dict[str, Any]]:
    """`systemextensionsctl list`: active network and endpoint-security
    extensions (the categories that can see traffic or processes)."""
    out, category = [], None
    for line in output.splitlines():
        cat = re.match(r"^--- com\.apple\.system_extension\.(\w+)", line)
        if cat:
            category = cat.group(1)
            continue
        m = re.match(r"^(\*?)\s+(\*?)\s+(\S+)\s+(\S+)\s+\(([^)]*)\)\s+(.+?)\s+\[([^\]]*)\]$", line)
        if m and category:
            out.append({"category": category, "enabled": m.group(1) == "*", "active": m.group(2) == "*",
                        "team_id": m.group(3), "bundle_id": m.group(4), "name": m.group(6), "state": m.group(7)})
    return out


def remote_control_apps(process_names: list[str], persistence_programs: list[str]) -> list[dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    for where, names in (("running", process_names), ("starts automatically", persistence_programs)):
        for n in names:
            low = n.lower()
            for key, label in REMOTE_CONTROL_APPS.items():
                if key in low and label not in found:
                    found[label] = {"app": label, "where": where, "evidence": n}
    return list(found.values())


def _running_process_names() -> list[str]:
    ok, out = mt._run(["ps", "-axo", "comm"])
    return out.splitlines()[1:] if ok else []


def analyze(host_protections: dict[str, Any] | None = None, persistence: dict[str, Any] | None = None) -> dict[str, Any]:
    """Every indicator, read live. Pass already-collected host protections /
    persistence to avoid reading them twice in one scan."""
    host = host_protections or mt.get_host_protections()
    pers = persistence or mt.get_persistence_items()
    checks = host.get("checks") or {}
    ind: list[Indicator] = []

    ok, out = mt._run(["scutil", "--proxy"])
    if ok:
        proxies = parse_proxies(out)
        ind.append(Indicator(
            "proxy", PRESENT if proxies else ABSENT,
            ", ".join(f"{k}={v}" for k, v in proxies.items()) or "no system proxy or PAC file is set",
            "A system proxy sends web traffic through another server, which can read unencrypted traffic and "
            "see every site visited.", "high: read directly from the system network configuration"))
    else:
        ind.append(Indicator("proxy", UNKNOWN, out.strip()[:200], "", "none: the check could not run"))

    ok, out = mt._run(["profiles", "status", "-type", "enrollment"])
    if "MDM enrollment:" in out:
        enrolled = bool(re.search(r"MDM enrollment:\s*Yes", out))
        ind.append(Indicator(
            "mdm_enrollment", PRESENT if enrolled else ABSENT, " ".join(out.split())[:200],
            "An MDM server can install profiles and certificates, push apps, and on a supervised Mac restrict "
            "or inspect much of the system. Normal on a work machine; unexpected on a personal one.",
            "high: reported by macOS itself"))
    else:
        ind.append(Indicator("mdm_enrollment", UNKNOWN, out.strip()[:200], "", "none: the check could not run"))

    ok, out = mt._run(["profiles", "list"])
    if ok or "no configuration profiles" in out:
        none = "no configuration profiles" in out.lower()
        ind.append(Indicator(
            "configuration_profiles", ABSENT if none else PRESENT, " ".join(out.split())[:300],
            "Profiles can set proxies, VPNs, DNS and trusted certificates silently.",
            "medium: this user's profiles only; device-wide profiles need `sudo profiles list`"))
    else:
        ind.append(Indicator("configuration_profiles", UNKNOWN, out.strip()[:200], "", "none: the check could not run"))

    roots: list[str] = []
    for args, domain in ((["security", "dump-trust-settings", "-d"], "admin"), (["security", "dump-trust-settings"], "user")):
        _, out = mt._run(args)
        if "No Trust Settings were found" not in out:
            roots.extend(f"{domain}: {m}" for m in re.findall(r"Cert \d+:\s*(.+)", out))
    ind.append(Indicator(
        "extra_trusted_roots", PRESENT if roots else ABSENT,
        "; ".join(roots)[:400] or "no user- or admin-added certificate trust settings",
        "A root certificate you trust lets whoever holds its key impersonate any HTTPS site to this Mac "
        "(TLS interception): the classic way corporate filters and spyware read encrypted traffic.",
        "high for user and admin trust settings; system-wide roots added by an MDM show under profiles"))

    ok, out = mt._run(["scutil", "--nc", "list"])
    vpns = parse_nc_list(out) if ok else []
    connected = [v for v in vpns if v["state"].lower() == "connected"]
    ind.append(Indicator(
        "vpn", PRESENT if connected else ABSENT,
        "; ".join(f"{v['name']} ({v['provider']}, {v['state']})" for v in vpns) or "no VPN configured",
        "A connected VPN carries this Mac's traffic through its operator's network. Usually your own "
        "(Tailscale, a work VPN); worth knowing who runs it.", "high: read from the network configuration"))

    _, out = mt._run(["systemextensionsctl", "list"])
    exts = [e for e in parse_system_extensions(out) if e["active"]]
    ind.append(Indicator(
        "system_extensions", PRESENT if exts else ABSENT,
        "; ".join(f"{e['name']} [{e['category']}, team {e['team_id']}]" for e in exts) or "none active",
        "Network extensions can see or filter traffic; endpoint-security extensions can watch every process "
        "and file. Security tools and VPNs use them legitimately.", "high: listed by macOS"))

    remote = []
    if (checks.get("remote_login") or {}).get("on"):
        remote.append("Remote Login (SSH)")
    if (checks.get("screen_sharing") or {}).get("on"):
        remote.append("Screen Sharing")
    ind.append(Indicator(
        "remote_access_services", PRESENT if remote else ABSENT, ", ".join(remote) or "Remote Login and Screen Sharing off",
        "These let someone with the password log in to or view this Mac over the network.",
        "high: read from launchd"))

    programs = [str(i.get("program") or i.get("label") or "") for i in pers.get("items", [])]
    rc = remote_control_apps(_running_process_names(), programs)
    ind.append(Indicator(
        "remote_control_software", PRESENT if rc else ABSENT,
        "; ".join(f"{r['app']} ({r['where']}: {Path(r['evidence']).name})" for r in rc) or "none of the known tools found",
        "Remote-control apps can show or control this screen from elsewhere. Legitimate when you installed "
        "them (second-screen and support tools); a common spyware vehicle otherwise.",
        f"medium: matched against {len(REMOTE_CONTROL_APPS)} known tools by name; a renamed tool would be missed"))

    unknowns = [
        "Which apps hold Screen Recording, Accessibility or Input Monitoring permission (needs Full Disk Access "
        "to read the privacy database).",
        "Device-wide configuration profiles and login items (need root).",
        "Hardware and firmware implants, and anything on the network outside this Mac.",
    ]
    present = [i for i in ind if i.status == PRESENT]
    return {
        "indicators": [i.to_dict() for i in ind],
        "present": len(present),
        "summary": (
            "No monitoring indicators found in what could be checked." if not present else
            f"{len(present)} indicator(s) present: " + ", ".join(i.name.replace('_', ' ') for i in present)
            + ". Each has a common legitimate explanation; see the evidence."
        ),
        "unknowns": unknowns,
    }
