"""Backend for the Wi-Fi card on the SECURITY screen (plan 1.9, finding 205).

``GET /api/os/security/wifi`` answers one question in plain words: what can the people who run
the network I am on see of what I do? It reads three things from this Mac and nothing else: the
Wi-Fi link (name, security mode), the DNS servers the Mac asks, and whether a VPN is connected.
Each statement in the answer is a fixed sentence chosen from those facts, never a model's text,
and a fact that could not be read says so instead of becoming a reassurance. Read only; the
route changes nothing on the Mac.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable
from typing import Any

from . import Request, route

SECURITY_WORDS = {
    "open": "open, no password",
    "wep": "WEP, an old and broken protection",
    "wpa_personal": "WPA, an old protection",
    "wpa_wpa2_personal": "WPA and WPA2 mixed",
    "wpa2_personal": "WPA2 password",
    "wpa2_wpa3_personal": "WPA2 and WPA3 password",
    "wpa3_personal": "WPA3 password",
    "wpa_enterprise": "WPA sign-in",
    "wpa2_enterprise": "WPA2 sign-in",
    "wpa3_enterprise": "WPA3 sign-in",
}
WEAK = {"open", "wep", "wpa_personal"}


def dns_kind(servers: list[str]) -> str:
    """router when every server is a private or link-local address, public when none is, mixed otherwise,
    unknown when the Mac reported none."""
    kinds = set()
    for s in servers:
        try:
            ip = ipaddress.ip_address(s.split("%")[0])
        except ValueError:
            continue
        kinds.add("router" if (ip.is_private or ip.is_link_local or ip.is_loopback) else "public")
    if not kinds:
        return "unknown"
    return kinds.pop() if len(kinds) == 1 else "mixed"


def can_see(wifi: dict[str, Any], dns_servers: list[str], vpn_connected: bool | None) -> dict[str, Any]:
    """The summary. ``sees`` is what the network operator can see, ``hidden`` what they cannot,
    ``unknown`` what this Mac could not check. Every line is a constant sentence."""
    if not wifi.get("available", True):
        return {"state": "unavailable", "sees": [], "hidden": [], "unknown": ["The Wi-Fi link could not be read."]}
    if not wifi.get("connected"):
        return {"state": "offline", "sees": [], "hidden": [], "unknown": []}
    sec = str(wifi.get("security") or "unknown")
    kind = dns_kind(dns_servers)
    sees: list[str] = ["That this Mac is on the network, and its name and hardware address."]
    hidden: list[str] = []
    unknown: list[str] = []
    if vpn_connected:
        sees.append("That a VPN is in use, how much data moves and when. The VPN's operator can see more.")
        hidden.append("Which sites and servers you connect to.")
    else:
        sees.append("The addresses of the sites and servers you connect to, how much data moves and when.")
        if kind == "router":
            sees.append("Every site name you look up: the router answers those lookups.")
        elif kind == "public":
            sees.append("Every site name you look up, unless your lookups are encrypted. Dourmouse cannot tell that from here.")
        elif kind == "mixed":
            sees.append("Many of the site names you look up. Dourmouse cannot tell which lookups are encrypted.")
        else:
            unknown.append("Which servers answer your site lookups could not be read.")
        if vpn_connected is None:
            unknown.append("Whether a VPN is connected could not be read.")
    hidden.append("What is on a page, what you type and your passwords, as long as the site uses HTTPS.")
    if sec in WEAK:
        sees.append("Anyone in range can capture the radio traffic on this network. Only HTTPS keeps pages private.")
    elif sec == "unknown":
        unknown.append("How this network is protected could not be read.")
    else:
        hidden.append("Other people on this network cannot read your traffic without the password.")
    return {"state": "connected", "sees": sees, "hidden": hidden, "unknown": unknown}


def _read_wifi() -> dict[str, Any]:
    from dourmouse.security import mac_telemetry as mt

    return mt.get_wifi()


def _read_dns() -> list[str] | None:
    from dourmouse.security import platform_adapter as pa

    dns = pa.get_dns_configuration()
    if not dns.get("available"):
        return None
    out: list[str] = []
    for r in dns.get("resolvers", []):
        for s in r.get("nameservers", []):
            if s not in out:
                out.append(s)
    return out


def _read_vpn() -> bool | None:
    from dourmouse.security import mac_telemetry as mt
    from dourmouse.security import monitoring

    ok, out = mt._run(["scutil", "--nc", "list"])
    if not ok:
        return None
    return any(v["state"].lower() == "connected" for v in monitoring.parse_nc_list(out))


def build(read_wifi: Callable[[], dict[str, Any]] = _read_wifi, read_dns: Callable[[], list[str] | None] = _read_dns,
          read_vpn: Callable[[], bool | None] = _read_vpn) -> dict[str, Any]:
    wifi = read_wifi()
    servers = read_dns() if wifi.get("connected") else []
    vpn = read_vpn() if wifi.get("connected") else None
    sec = wifi.get("security")
    summary = can_see(wifi, servers or [], vpn)
    return {
        "connected": bool(wifi.get("connected")),
        "name": wifi.get("ssid"),
        "name_hidden": bool(wifi.get("ssid_redacted")),
        "security": sec,
        "security_word": SECURITY_WORDS.get(str(sec), str(sec or "unknown").replace("_", " ")) if wifi.get("connected") else None,
        "weak": sec in WEAK,
        "dns_servers": servers or [],
        "dns_kind": dns_kind(servers or []),
        "vpn_connected": vpn,
        **summary,
    }


@route("GET", "/api/os/security/wifi")
def wifi_summary(req: Request) -> tuple[int, dict[str, Any]]:
    return 200, build()
