"""Check the moment the network changes (MS-10, spec items 9 and 38;
finding #108).

The sentry scans every few minutes. The riskiest moment, joining a new
network, should not wait for the next tick: this watcher polls a cheap
identity of the current network (default route's gateway and interface,
Wi-Fi network name when macOS reveals it) every few seconds and runs a scan
at once when it changes. Each scan and each change is pushed to the live
event stream, so the console updates without a refresh.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from collections.abc import Callable
from typing import Any

Identity = tuple[str, str, str]


def current_identity() -> Identity:
    from . import mac_telemetry as mt
    from . import platform_adapter as pa

    gw = pa.get_default_gateway()
    if not gw.get("available"):
        return ("", "", "offline")
    ssid = ""
    if str(gw.get("interface") or "").startswith("en"):
        try:
            ssid = str(mt.get_wifi(str(gw["interface"])).get("ssid") or "")
        except Exception:  # noqa: BLE001 -- a Wi-Fi read failure just means no name
            ssid = ""
    return (str(gw.get("gateway") or ""), str(gw.get("interface") or ""), ssid)


def describe(identity: Identity) -> str:
    gateway, interface, ssid = identity
    if ssid == "offline" and not gateway:
        return "offline"
    return f"{ssid or 'network'} via {interface or '?'} (router {gateway or '?'})"


class NetworkWatcher:
    def __init__(self, on_change: Callable[[Identity | None, Identity], None],
                 identity_fn: Callable[[], Identity] = current_identity, interval: float = 5.0) -> None:
        self._on_change = on_change
        self._identity_fn = identity_fn
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.current: Identity | None = None
        self.changes = 0

    def poll(self) -> bool:
        """One check; True when the network changed (the first read is the
        starting point, not a change)."""
        now = self._identity_fn()
        if self.current is None:
            self.current = now
            return False
        if now == self.current:
            return False
        before, self.current = self.current, now
        self.changes += 1
        self._on_change(before, now)
        return True

    def start(self) -> None:
        if self._thread is not None:
            return

        def loop() -> None:
            while not self._stop.is_set():
                with contextlib.suppress(Exception):  # one bad read never stops the watcher
                    self.poll()
                self._stop.wait(self._interval)

        self._thread = threading.Thread(target=loop, daemon=True, name="dourmouse-netwatch")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


def netwatch_enabled() -> bool:
    return os.environ.get("DOURMOUSE_NETWATCH", "1").strip().lower() not in ("0", "false", "no", "off")


def analyst_enabled() -> bool:
    return os.environ.get("DOURMOUSE_SECURITY_ANALYST", "1").strip().lower() not in ("0", "false", "no", "off")


def scan_event(result: Any, reason: str) -> dict[str, Any]:
    """The live event for one scan: counts and the new findings, not the
    whole state."""
    counts = {"high": 0, "med": 0, "low": 0}
    for f in result.all_findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    return {"type": "security_scan", "reason": reason, "at": time.time(), "counts": counts,
            "new": [{"severity": f.severity, "title": f.title} for f in result.new_findings]}
