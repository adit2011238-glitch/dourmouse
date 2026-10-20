#!/usr/bin/env python3
"""Pure-Python port scanner for the security canary suite. No nmap needed
(this Mac does not have it installed, and installing software needs the
owner's own OK first -- a stdlib scanner needs nothing extra on either box).

Run ON THE MAC (checks what is listening locally):
    python3 scan.py 127.0.0.1

Run FROM THE LAPTOP/DESKTOP (checks what the Mac exposes on the LAN):
    python3 scan.py 192.168.1.54      # use the Mac's real LAN IP

PASS for the LAN run: nothing answers on 8765 (Dourmouse), 9333/9334 (the
Electron debug ports) or any port you did not expect. SSH (22) answering is
a known, already-flagged finding (Remote Login is on) -- not new, but worth
seeing with your own eyes.
"""
from __future__ import annotations

import socket
import sys
import time

#: Ports worth a look for this threat model. Not a full 1-65535 sweep by
#: default (slow over a real LAN); pass --all for that.
_INTERESTING = {
    22: "SSH / Remote Login",
    80: "HTTP",
    443: "HTTPS",
    445: "SMB / File Sharing",
    548: "AFP / File Sharing",
    3283: "Apple Remote Desktop",
    3389: "RDP",
    5000: "AirPlay/other",
    5900: "VNC / Screen Sharing",
    7000: "AirPlay",
    8765: "Dourmouse server (should be LOOPBACK ONLY)",
    8799: "this canary server itself, if also on the target",
    9333: "Electron CDP debug port (should be LOOPBACK ONLY)",
    9334: "Electron 2nd debug port (should be LOOPBACK ONLY)",
}


def scan(host: str, ports: list[int], timeout: float = 0.5) -> dict[int, bool]:
    out: dict[int, bool] = {}
    for port in ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            out[port] = s.connect_ex((host, port)) == 0
        except OSError:
            out[port] = False
        finally:
            s.close()
    return out


def main() -> None:
    args = sys.argv[1:]
    full = "--all" in args
    args = [a for a in args if a != "--all"]
    host = args[0] if args else "127.0.0.1"
    ports = list(range(1, 65536)) if full else sorted(_INTERESTING)
    print(f"Scanning {host} ({'all 65535 ports, this takes a while' if full else str(len(ports)) + ' known ports'})...")
    t0 = time.time()
    result = scan(host, ports)
    open_ports = [p for p, is_open in result.items() if is_open]
    print(f"Done in {time.time()-t0:.1f}s.\n")
    if not open_ports:
        print("No open ports found among those checked. PASS for the loopback-only claim if this was run from a different box.")
        return
    print("OPEN:")
    for p in sorted(open_ports):
        label = _INTERESTING.get(p, "unknown service")
        flag = "  <-- CHECK THIS (expected loopback-only)" if p in (8765, 9333, 9334) and host != "127.0.0.1" else ""
        print(f"  {p:5d}  {label}{flag}")


if __name__ == "__main__":
    main()
