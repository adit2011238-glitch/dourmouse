"""The baseline engine (MS-2, spec items 9 and 18, finding #100): turns raw
telemetry into "this differs from before".

A finding like "sshd listens on port 22" means nothing on its own; "a new
listening port appeared since yesterday" or "the router's MAC address changed
on the same network" does. So every scan reduces the telemetry to
observations, compares them with what this Mac has seen before on this
network, and reports only what is new or changed.

Two scopes. Host-level things (listening ports, persistence items, the Mac's
own protections, which processes talk to the network) are the same wherever
the Mac is, so they share one baseline. Network-level things (the gateway's
MAC address, DNS resolvers, Wi-Fi security) are per network: a café and home
legitimately differ, and moving between them is not an anomaly.

Learning period: a baseline says nothing until it has seen enough (by
default 3 scans over at least 30 minutes), so the first scans after install or
on a new network seed it silently instead of calling everything "new".

Pure: no I/O, no clock (``now`` is passed in). The store lives in sentry.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

HOST = "host"
LEARNING_MIN_SCANS = 3
LEARNING_MIN_SECONDS = 30 * 60


@dataclass(frozen=True)
class Observation:
    scope: str  # HOST or a network id
    category: str
    key: str  # what it is (stable identity)
    value: str = ""  # its current state; a change of value on a known key is "changed"


@dataclass(frozen=True)
class Anomaly:
    kind: str  # "new" | "changed" | "gone"
    observation: Observation
    previous_value: str | None = None


def observations(state: dict[str, Any], network_id: str) -> list[Observation]:
    """Reduce one telemetry snapshot to comparable observations."""
    obs: list[Observation] = []

    lp = state.get("listening_ports") or {}
    if lp.get("available"):
        for p in lp.get("listening_ports", []):
            obs.append(Observation(HOST, "listening_port",
                                   f"{p['command']}|{p['protocol']}|{p['port']}", p.get("exposure", "")))

    est = state.get("established_connections") or {}
    if est.get("available"):
        for c in est.get("connections", []):
            obs.append(Observation(HOST, "network_process", str(c.get("command", "")), ""))

    pers = state.get("persistence") or {}
    if pers.get("available"):
        for item in pers.get("items", []):
            if "error" in item:
                continue
            obs.append(Observation(HOST, "persistence", item["path"], item.get("sha256", "")))

    prot = (state.get("host_protections") or {}).get("checks") or {}
    for name, check in prot.items():
        if check.get("on") is not None:
            obs.append(Observation(HOST, "protection", name, "on" if check["on"] else "off"))

    gw = state.get("default_gateway") or {}
    arp = state.get("arp_neighbors") or {}
    if gw.get("available") and gw.get("gateway") and arp.get("available"):
        mac = next((n.get("mac") for n in arp.get("neighbors", []) if n.get("ip") == gw["gateway"]), None)
        if mac:
            obs.append(Observation(network_id, "gateway_mac", gw["gateway"], mac.lower()))

    dns = state.get("dns") or {}
    if dns.get("available"):
        servers = sorted({s for r in dns.get("resolvers", []) for s in r.get("nameservers", [])})
        if servers:
            obs.append(Observation(network_id, "dns_resolvers", "resolvers", ",".join(servers)))

    wifi = state.get("wifi") or {}
    if wifi.get("connected"):
        obs.append(Observation(network_id, "wifi_security", "security", str(wifi.get("security"))))

    # One observation per (scope, category, key): a process with ten
    # connections is one "this process talks to the network" fact.
    return list(dict.fromkeys(obs))


def is_learning(meta: dict[str, Any] | None, now: float) -> bool:
    if not meta:
        return True
    return meta["scans"] < LEARNING_MIN_SCANS or now - meta["first_seen"] < LEARNING_MIN_SECONDS


def compare(
    current: list[Observation],
    known: dict[tuple[str, str, str], str],
    *,
    categories_that_report_gone: frozenset[str] = frozenset({"persistence"}),
) -> list[Anomaly]:
    """``known`` maps (scope, category, key) to the last value seen. New keys
    and changed values are anomalies; a key that disappeared is reported only
    for categories where disappearing matters (a removed persistence item is
    worth knowing; a closed connection is not)."""
    out: list[Anomaly] = []
    seen: set[tuple[str, str, str]] = set()
    for o in current:
        k = (o.scope, o.category, o.key)
        seen.add(k)
        if k not in known:
            out.append(Anomaly("new", o))
        elif known[k] != o.value:
            out.append(Anomaly("changed", o, previous_value=known[k]))
    scopes = {o.scope for o in current}
    for (scope, category, key), value in known.items():
        if scope in scopes and category in categories_that_report_gone and (scope, category, key) not in seen:
            out.append(Anomaly("gone", Observation(scope, category, key, value), previous_value=value))
    return out
