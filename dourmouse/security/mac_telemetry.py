"""Mac security telemetry beyond the network basics (MS-1, finding #099).

Owner decision 2026-09-24: the security system is for this Mac only. This
module reads what a defender on macOS actually needs and platform_adapter
did not have: the Wi-Fi link's security, the host's own protections
(firewall, FileVault, SIP, Gatekeeper, Remote Login, Screen Sharing), who a
network-active process really is (path, parent, code signature, Gatekeeper
verdict), persistence items, and connection diagnostics.

Rules, same as platform_adapter: every source reports its own availability
and never raises; a value macOS hides (the SSID without Location permission)
is reported as redacted, never guessed; parsers are pure functions so they
are tested against real captured output.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import socket
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any

_TIMEOUT = 10.0


def _run(cmd: list[str], timeout: float = _TIMEOUT) -> tuple[bool, str]:
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed argument lists only
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False,
        )
    except FileNotFoundError:
        return False, f"{cmd[0]} is not available on this system"
    except subprocess.TimeoutExpired:
        return False, f"{cmd[0]} timed out after {timeout}s"
    except OSError as exc:
        return False, f"{cmd[0]} failed to start: {exc}"
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, out


# --------------------------------------------------------------------------- #
# Wi-Fi
# --------------------------------------------------------------------------- #

_SECURITY_MODES = {
    "spairport_security_mode_none": "open",
    "spairport_security_mode_wep": "wep",
    "spairport_security_mode_wpa_personal": "wpa_personal",
    "spairport_security_mode_wpa_personal_mixed": "wpa_wpa2_personal",
    "spairport_security_mode_wpa2_personal": "wpa2_personal",
    "spairport_security_mode_wpa2_personal_mixed": "wpa2_wpa3_personal",
    "spairport_security_mode_wpa3_personal": "wpa3_personal",
    "spairport_security_mode_wpa3_transition": "wpa2_wpa3_personal",
    "spairport_security_mode_wpa_enterprise": "wpa_enterprise",
    "spairport_security_mode_wpa2_enterprise": "wpa2_enterprise",
    "spairport_security_mode_wpa3_enterprise": "wpa3_enterprise",
}
WEAK_WIFI = {"open", "wep", "wpa_personal", "wpa_wpa2_personal"}


def parse_wifi(profile: dict[str, Any]) -> dict[str, Any]:
    """From `system_profiler SPAirPortDataType -json`. macOS redacts the SSID
    (and omits the BSSID) unless the reading process has Location permission;
    that is reported, not papered over."""
    for itf in (profile.get("SPAirPortDataType") or [{}])[0].get("spairport_airport_interfaces", []):
        cur = itf.get("spairport_current_network_information")
        if not cur:
            continue
        raw_mode = str(cur.get("spairport_security_mode", ""))
        ssid = cur.get("_name")
        signal = str(cur.get("spairport_signal_noise", ""))
        m = re.match(r"(-?\d+)\s*dBm\s*/\s*(-?\d+)\s*dBm", signal)
        return {
            "available": True,
            "connected": True,
            "interface": itf.get("_name"),
            "ssid": None if ssid in (None, "<redacted>") else ssid,
            "ssid_redacted": ssid == "<redacted>",
            "bssid": cur.get("spairport_network_bssid"),
            "security": _SECURITY_MODES.get(raw_mode, raw_mode.replace("spairport_security_mode_", "") or "unknown"),
            "channel": cur.get("spairport_network_channel"),
            "phy_mode": cur.get("spairport_network_phymode"),
            "rssi_dbm": int(m.group(1)) if m else None,
            "noise_dbm": int(m.group(2)) if m else None,
        }
    return {"available": True, "connected": False}


_IPCONFIG_SECURITY = {
    "NONE": "open", "WEP": "wep", "WPA_PSK": "wpa_personal", "WPA_WPA2_PSK": "wpa_wpa2_personal",
    "WPA2_PSK": "wpa2_personal", "WPA2_WPA3_PSK": "wpa2_wpa3_personal", "WPA3_SAE": "wpa3_personal",
    "WPA3_PSK": "wpa3_personal", "WPA2_WPA3_SAE": "wpa2_wpa3_personal",
    "WPA_ENTERPRISE": "wpa_enterprise", "WPA2_ENTERPRISE": "wpa2_enterprise", "WPA3_ENTERPRISE": "wpa3_enterprise",
}


def parse_ipconfig_summary(output: str, interface: str = "en0") -> dict[str, Any]:
    """From `ipconfig getsummary en0`: the link's security in about 10 ms
    (system_profiler takes about 5 s, measured on this Mac), used by every
    background scan. No signal strength or channel; get_wifi_details() has
    those, on demand."""
    def field(name: str) -> str | None:
        m = re.search(rf"^\s*{name}\s*:\s*(.+)$", output, re.M)
        return m.group(1).strip() if m else None

    if field("InterfaceType") != "WiFi" or field("LinkStatusActive") != "TRUE":
        return {"available": True, "connected": False}
    raw = (field("Security") or "").upper()
    ssid, bssid = field("SSID"), field("BSSID")
    return {
        "available": True, "connected": True, "interface": interface,
        "ssid": None if ssid in (None, "<redacted>") else ssid, "ssid_redacted": ssid == "<redacted>",
        "bssid": None if bssid in (None, "<redacted>") else bssid,
        "security": _IPCONFIG_SECURITY.get(raw, raw.lower() or "unknown"),
    }


def get_wifi(interface: str = "en0") -> dict[str, Any]:
    ok, out = _run(["ipconfig", "getsummary", interface])
    if not ok:
        return {"available": False, "reason": out.strip()[:300]}
    return parse_ipconfig_summary(out, interface)


def get_wifi_details() -> dict[str, Any]:
    """Signal, noise, channel and PHY mode too (system_profiler, about 5 s)."""
    ok, out = _run(["system_profiler", "SPAirPortDataType", "-json"], timeout=30)
    if not ok:
        return {"available": False, "reason": out.strip()[:300]}
    try:
        return parse_wifi(json.loads(out))
    except ValueError as exc:
        return {"available": False, "reason": f"unreadable system_profiler output: {exc}"}


# --------------------------------------------------------------------------- #
# Host protections
# --------------------------------------------------------------------------- #

def parse_disabled_services(output: str) -> dict[str, bool]:
    """`launchctl print-disabled system` lines like  "com.openssh.sshd" => enabled."""
    return {m.group(1): m.group(2) == "enabled" for m in re.finditer(r'"([^"]+)"\s*=>\s*(enabled|disabled)', output)}


def _port_accepting(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.5):
            return True
    except OSError:
        return False


def get_host_protections() -> dict[str, Any]:
    """The Mac's own defences, each read from the real tool. `None` means the
    check itself could not run (reported with its reason), never "off"."""
    out: dict[str, Any] = {"available": True, "checks": {}}
    checks = out["checks"]

    ok, txt = _run(["/usr/libexec/ApplicationFirewall/socketfilterfw", "--getglobalstate"])
    checks["firewall"] = {"on": ("State = 1" in txt or "enabled" in txt.lower()) if ok else None, "raw": txt.strip()[:200]}
    ok, txt = _run(["/usr/libexec/ApplicationFirewall/socketfilterfw", "--getstealthmode"])
    checks["stealth_mode"] = {"on": ("is on" in txt) if ok else None, "raw": txt.strip()[:200]}
    ok, txt = _run(["fdesetup", "status"])
    checks["filevault"] = {"on": ("FileVault is On" in txt) if ok else None, "raw": txt.strip()[:200]}
    ok, txt = _run(["csrutil", "status"])
    checks["sip"] = {"on": ("enabled" in txt) if ok else None, "raw": txt.strip()[:200]}
    ok, txt = _run(["spctl", "--status"])
    checks["gatekeeper"] = {"on": ("assessments enabled" in txt) if (ok or "assessments" in txt) else None, "raw": txt.strip()[:200]}

    ok, txt = _run(["launchctl", "print-disabled", "system"])
    services = parse_disabled_services(txt) if ok else {}
    checks["remote_login"] = {
        "on": services.get("com.openssh.sshd") if services else None,
        "accepting": _port_accepting(22),
    }
    checks["screen_sharing"] = {
        "on": services.get("com.apple.screensharing") if services else None,
        "accepting": _port_accepting(5900),
    }
    return out


# --------------------------------------------------------------------------- #
# Code signing and Gatekeeper
# --------------------------------------------------------------------------- #

def parse_codesign(output: str, returncode_ok: bool) -> dict[str, Any]:
    """From `codesign -dv --verbose=2 <path>` (it writes to stderr)."""
    if "code object is not signed at all" in output:
        return {"signed": False, "kind": "unsigned", "authorities": [], "team_id": None}
    if not returncode_ok and "Authority=" not in output and "Signature=" not in output:
        return {"signed": None, "kind": "unknown", "authorities": [], "team_id": None, "reason": output.strip()[:200]}
    authorities = re.findall(r"^Authority=(.+)$", output, re.M)
    team = re.search(r"^TeamIdentifier=(.+)$", output, re.M)
    adhoc = "Signature=adhoc" in output or "(adhoc)" in output
    if adhoc:
        kind = "adhoc"
    elif any(a.startswith("Apple ") or a == "Software Signing" or a.startswith("macOS Software Signing") for a in authorities):
        kind = "apple"
    elif any(a.startswith("Developer ID Application") for a in authorities):
        kind = "developer_id"
    elif any(a.startswith("Apple Development") or a.startswith("Apple Distribution") for a in authorities):
        kind = "development"
    else:
        kind = "other"
    return {
        "signed": True, "kind": kind, "authorities": authorities,
        "team_id": team.group(1).strip() if team and team.group(1).strip() != "not set" else None,
    }


def parse_spctl(output: str) -> dict[str, Any]:
    """From `spctl --assess --type execute -vv <path>`. Gatekeeper's execute
    assessment only rates apps: for a bare command-line binary it answers
    "rejected (the code is valid but does not seem to be an app)", which is
    not a security verdict (found live: a copy of /bin/echo, Apple-signed,
    gets exactly that). That case is "not_an_app", judged by signature alone."""
    if "does not seem to be an app" in output:
        verdict = "not_an_app"
    else:
        verdict = "accepted" if ": accepted" in output else "rejected" if ": rejected" in output else "unknown"
    source = re.search(r"^source=(.+)$", output, re.M)
    reason = re.search(r": rejected \((.+)\)", output)
    return {"verdict": verdict, "source": source.group(1).strip() if source else None,
            "reason": reason.group(1) if reason else None}


_SIG_CACHE: dict[tuple[str, float, int], dict[str, Any]] = {}


def code_signature(path: str) -> dict[str, Any]:
    """Signature and Gatekeeper verdict for an executable, cached by path,
    mtime and size (so a replaced binary is re-checked)."""
    try:
        st = os.stat(path)
    except OSError as exc:
        return {"path": path, "available": False, "reason": str(exc)}
    key = (path, st.st_mtime, st.st_size)
    if key in _SIG_CACHE:
        return _SIG_CACHE[key]
    ok, cs = _run(["codesign", "-dv", "--verbose=2", path])
    sig = parse_codesign(cs, ok)
    target = _bundle_for(path) or path
    _, gk = _run(["spctl", "--assess", "--type", "execute", "-vv", target])
    result = {"path": path, "available": True, **sig, "gatekeeper": parse_spctl(gk)}
    _SIG_CACHE[key] = result
    return result


def _bundle_for(path: str) -> str | None:
    """Gatekeeper assesses app bundles, not the binary inside them."""
    m = re.match(r"^(.*?\.app)/", path)
    return m.group(1) if m else None


# --------------------------------------------------------------------------- #
# Processes
# --------------------------------------------------------------------------- #

# Classified, never written to: S108 is about creating temp files, not naming them.
_SUSPICIOUS_DIRS = ("/tmp/", "/private/tmp/", "/var/tmp/", "/private/var/tmp/", "/Users/Shared/")  # noqa: S108


def location_class(path: str | None) -> str:
    """Where a running executable lives: system / applications / user
    downloads / temporary / other. A network-active process running out of a
    temp or Downloads folder is a classic malware trait."""
    if not path:
        return "unknown"
    home = str(Path.home())
    if path.startswith(("/System/", "/usr/bin/", "/usr/sbin/", "/usr/libexec/", "/bin/", "/sbin/")):
        return "system"
    if path.startswith(("/Applications/", f"{home}/Applications/")):
        return "applications"
    if path.startswith(f"{home}/Downloads/"):
        return "downloads"
    if path.startswith(_SUSPICIOUS_DIRS) or "/T/" in path and path.startswith("/private/var/folders/"):
        return "temporary"
    return "other"


def process_details(pid: int) -> dict[str, Any]:
    """Who a process really is: executable path, parent, user, start time,
    and command line (truncated). psutil is a declared dependency."""
    try:
        import psutil
    except ImportError:
        return {"pid": pid, "available": False, "reason": "psutil is not installed"}
    try:
        p = psutil.Process(pid)
        with p.oneshot():
            exe = None
            try:
                exe = p.exe()
            except (psutil.AccessDenied, psutil.ZombieProcess):
                exe = None
            parent = p.parent()
            return {
                "pid": pid, "available": True, "name": p.name(), "exe": exe,
                "location": location_class(exe),
                "ppid": p.ppid(), "parent_name": parent.name() if parent else None,
                "user": p.username(), "started_at": p.create_time(),
                "cmdline": " ".join(p.cmdline())[:500] if exe else None,
            }
    except psutil.NoSuchProcess:
        return {"pid": pid, "available": False, "reason": "process has exited"}
    except psutil.AccessDenied:
        return {"pid": pid, "available": False, "reason": "access denied (owned by another user)"}


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #

def persistence_dirs() -> list[Path]:
    home = Path.home()
    return [home / "Library/LaunchAgents", Path("/Library/LaunchAgents"), Path("/Library/LaunchDaemons")]


def parse_launchd_plist(data: bytes) -> dict[str, Any]:
    plist = plistlib.loads(data)
    program = plist.get("Program") or (plist.get("ProgramArguments") or [None])[0]
    return {
        "label": plist.get("Label"),
        "program": program,
        "run_at_load": bool(plist.get("RunAtLoad")),
        "keep_alive": bool(plist.get("KeepAlive")),
    }


def get_persistence_items() -> dict[str, Any]:
    """Launch agents and daemons: how something survives a restart. Login
    items managed by the Background Task Management database need root to
    read (`sfltool dumpbtm`); that gap is reported, not hidden."""
    items: list[dict[str, Any]] = []
    for d in persistence_dirs():
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.plist")):
            try:
                data = f.read_bytes()
                parsed = parse_launchd_plist(data)
            except (OSError, plistlib.InvalidFileException, ValueError) as exc:
                items.append({"path": str(f), "error": f"unreadable: {exc}"})
                continue
            items.append({
                "path": str(f), "scope": "user" if str(f).startswith(str(Path.home())) else "system",
                "sha256": hashlib.sha256(data).hexdigest(), "modified_at": f.stat().st_mtime, **parsed,
            })
    return {"available": True, "items": items,
            "gaps": ["Login items in the Background Task Management database need root to read (sfltool dumpbtm)."]}


# --------------------------------------------------------------------------- #
# Connection diagnostics
# --------------------------------------------------------------------------- #

def parse_ping(output: str) -> dict[str, Any]:
    """From macOS `ping -c N`. Jitter is the standard deviation of the
    individual round trips (macOS reports it as stddev)."""
    loss = re.search(r"([\d.]+)% packet loss", output)
    rtts = [float(x) for x in re.findall(r"time=([\d.]+) ms", output)]
    return {
        "sent": len(re.findall(r"icmp_seq=", output)) or None,
        "loss_pct": float(loss.group(1)) if loss else None,
        "avg_ms": round(statistics.mean(rtts), 2) if rtts else None,
        "jitter_ms": round(statistics.pstdev(rtts), 2) if len(rtts) > 1 else None,
        "min_ms": min(rtts) if rtts else None, "max_ms": max(rtts) if rtts else None,
    }


def ping(host: str, count: int = 5) -> dict[str, Any]:
    _, out = _run(["ping", "-c", str(count), "-i", "0.2", "-t", str(count + 3), host], timeout=count + 8)
    return {"host": host, **parse_ping(out)}


def dns_timing(name: str = "apple.com") -> dict[str, Any]:
    t0 = time.monotonic()
    try:
        infos = socket.getaddrinfo(name, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        failed: dict[str, Any] = {"name": name, "ok": False, "error": str(exc), "ms": round((time.monotonic() - t0) * 1000, 1)}
        return failed
    return {"name": name, "ok": True, "addresses": sorted({i[4][0] for i in infos})[:4],
            "ms": round((time.monotonic() - t0) * 1000, 1)}


def get_diagnostics(gateway_ip: str | None) -> dict[str, Any]:
    """Latency, jitter and loss to the router and to the internet, and DNS
    lookup time: separates "my Wi-Fi is bad" from "the internet is down"
    from "DNS is broken"."""
    out: dict[str, Any] = {"available": True}
    if gateway_ip:
        out["gateway"] = ping(gateway_ip)
    out["internet"] = ping("1.1.1.1")
    out["dns"] = dns_timing()
    return out


# --------------------------------------------------------------------------- #
# Network identity
# --------------------------------------------------------------------------- #

def network_id(gateway_ip: str | None, wifi: dict[str, Any], dns_domains: list[str] | None = None) -> str:
    """A stable id for "which network am I on", for per-network baselines;
    a hash, so the id itself reveals nothing. Deliberately NOT anchored on the
    router's MAC address: detecting a changed router MAC is the ARP-spoofing
    check, and a spoofer changes exactly that, so a MAC-anchored id would file
    the attack under a brand-new (silently learning) network. The SSID would
    be the natural anchor but macOS redacts it without Location permission,
    so: gateway IP, Wi-Fi security (or "wired"), and the DHCP search domains."""
    security = wifi.get("security") if wifi.get("connected") else "wired"
    ssid = wifi.get("ssid") or ""
    domains = ",".join(sorted(dns_domains or []))
    return hashlib.sha256(f"{gateway_ip}|{security}|{ssid}|{domains}".encode()).hexdigest()[:16]
