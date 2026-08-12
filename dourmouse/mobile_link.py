"""Phone link setup for Dourmouse (v5.13) — one command to go mobile.

The web UI is already phone-usable: it binds ``DOURMOUSE_HOST``
(0.0.0.0 for LAN/Tailscale reach) and every non-loopback request is
gated by ``DOURMOUSE_ACCESS_TOKEN`` (Bearer header or the
``dourmouse_session`` cookie set by /api/login — constant-time compare,
loopback exempt). What was missing is the SETUP: generating the token,
writing it to .env, finding the address a phone can actually reach, and
printing a scannable QR.

This module is that setup. Run:

    python -m dourmouse.mobile_link [--rotate] [--port 8765]

It:
1. Generates a token (secrets.token_urlsafe, 32 bytes -> ~43 chars) or
   reuses the one already in .env.
2. Writes DOURMOUSE_HOST=0.0.0.0 + DOURMOUSE_ACCESS_TOKEN into .env
   idempotently (only rewrites the file when something changed; other
   keys are preserved exactly).
3. Detects the machine's LAN IPv4s AND the Tailscale IPv4 (100.x.y.z)
   when the Tailscale interface exists — a phone on the same Wi-Fi uses
   the LAN URL; a phone anywhere uses the Tailscale URL (with the
   Tailscale app installed and the phone added to the tailnet).
4. Prints the pairing URL(s) with a scannable QR (``segno``, pure-Python,
   zero native deps) and the exact remaining steps for the phone.

Honesty (Rule 2.2): if segno is missing the QR is skipped and the URL is
printed plainly (never a fake QR). If no LAN/Tailscale IP is found, the
commands print exactly why and what to check — no fabricated address.
Secrets are only written to the project .env (Rule 2.6), never printed
to a log.
"""

from __future__ import annotations

import argparse
import io
import ipaddress
import secrets
import socket
import subprocess
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"
_DEFAULT_PORT = 8765
# Phone-reachable address space, expressed as EXACT networks (not the
# ``is_private`` grab-bag, which also claims 0.0.0.0/8 and 127.0.0.0/8):
# RFC1918 LAN + Tailscale's CGNAT range (RFC 6598, 100.64.0.0/10).
_RFC1918 = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


# --------------------------------------------------------------------------- #
# Token
# --------------------------------------------------------------------------- #

def generate_token() -> str:
    """A fresh access token: 32 random bytes, urlsafe base64 (~43 chars)."""
    return secrets.token_urlsafe(32)


# --------------------------------------------------------------------------- #
# .env writing (idempotent, preserves everything else)
# --------------------------------------------------------------------------- #

def read_env(path: Path | None = None) -> dict[str, str]:
    """Parse a .env into {KEY: value}. Missing file -> {} (never raises)."""
    path = path or _ENV_PATH
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def write_env(
    host: str,
    token: str,
    path: Path | None = None,
) -> dict[str, Any]:
    """Write DOURMOUSE_HOST + DOURMOUSE_ACCESS_TOKEN into .env.

    Idempotent: rewrites the file ONLY when a value changed; other keys,
    comments, and blank lines are preserved byte-for-byte. Returns
    ``{changed: bool, host: str, token_set: bool}`` so the CLI can say
    honestly whether anything was modified.
    """
    path = path or _ENV_PATH
    target_host = f"DOURMOUSE_HOST={host}"
    target_token = f"DOURMOUSE_ACCESS_TOKEN={token}"

    had_host = False
    had_token = False
    out_lines: list[str] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            key = line.strip().partition("=")[0].strip()
            if key == "DOURMOUSE_HOST":
                had_host = True
                if line != target_host:
                    out_lines.append(target_host)
                else:
                    out_lines.append(line)
                continue
            if key == "DOURMOUSE_ACCESS_TOKEN":
                had_token = True
                if line != target_token:
                    out_lines.append(target_token)
                else:
                    out_lines.append(line)
                continue
            out_lines.append(line)

    changed = False
    if not had_host:
        out_lines.append(target_host)
        changed = True
    if not had_token:
        out_lines.append(target_token)
        changed = True
    # Detect in-place value changes (the loop already replaced them).
    if had_host and target_host not in out_lines:
        changed = True
    if had_token and target_token not in out_lines:
        changed = True

    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return {"changed": changed, "host": host, "token_set": bool(token)}


# --------------------------------------------------------------------------- #
# Address detection (LAN + Tailscale)
# --------------------------------------------------------------------------- #

def _ifconfig_addrs() -> list[str]:
    """IPv4s from ``ifconfig`` (macOS/Linux), or [] if unavailable."""
    try:
        proc = subprocess.run(
            ["ifconfig"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    addrs: list[str] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if "inet " not in line:
            continue
        parts = line.replace("inet ", " ").split()
        if parts and ":" not in parts[0]:
            addrs.append(parts[0])
    return addrs


def _socket_addrs() -> list[str]:
    """IPv4s from socket.getaddrinfo (portable fallback)."""
    out: list[str] = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = str(info[4][0])  # mypy: sockaddr is typed str|int; always a str IP
            if ip and not ip.startswith("127."):
                out.append(ip)
    except OSError:
        pass
    return out


def detect_addresses() -> dict[str, Any]:
    """Phone-reachable addresses on this machine.

    Returns ``{lan: [..], tailscale: [..], found: bool}``. ``lan`` are
    private IPv4s (RFC1918); ``tailscale`` is the Tailscale interface IP
    (100.64.0.0/10) when present. Deterministic, never raises: a machine
    with no private interface reports found:False and the CLI says why.
    """
    raw = _ifconfig_addrs() or _socket_addrs()
    lan: list[str] = []
    tailscale: list[str] = []
    for ip in raw:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        # Phone-reachable only: exact RFC1918 or CGNAT membership. This is
        # deliberately NOT ``is_private`` — CPython marks 0.0.0.0/8 and
        # 127.0.0.0/8 private, and ``is_unspecified`` only covers 0.0.0.0/32,
        # so a loose check lets 0.0.0.1 masquerade as a LAN address.
        # NB: ``addr in _RFC1918`` would test tuple-equality (always False);
        # network membership needs ``any(addr in net ...)``.
        if not (any(addr in net for net in _RFC1918) or addr in _CGNAT):
            continue
        if addr in _CGNAT:
            tailscale.append(ip)
        else:
            lan.append(ip)
    # Dedupe, keep order.
    lan = list(dict.fromkeys(lan))
    tailscale = list(dict.fromkeys(tailscale))
    return {"lan": lan, "tailscale": tailscale, "found": bool(lan or tailscale)}


def pairing_url(host_ip: str, port: int = _DEFAULT_PORT) -> str:
    """The URL a phone opens to reach Dourmouse on this host."""
    return f"http://{host_ip}:{port}/login"


# --------------------------------------------------------------------------- #
# QR
# --------------------------------------------------------------------------- #

def qr_ascii(url: str) -> str | None:
    """Render a scannable QR to a terminal string; None if segno is missing.

    Honest degradation (Rule 2.2): without segno we return None and the
    caller prints the URL plainly — never a fake/unscannable QR.
    """
    try:
        import segno
    except ImportError:
        return None
    try:
        # segno's terminal() prints to stdout when no file is given and its
        # stubs type it as returning None — capture via a buffer instead so
        # we get the QR as a real string (and never scribble on stdout).
        qr = segno.make(url, error="m")
        buf = io.StringIO()
        # Default border (NOT compact=True): borderless QRs routinely fail to
        # scan from a phone camera — the quiet zone is what makes this work.
        qr.terminal(buf)
        return buf.getvalue()
    except Exception:  # noqa: BLE001 -- a broken QR must never crash setup
        return None


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mobile-link",
        description="One command to open Dourmouse on your phone.",
    )
    p.add_argument(
        "--rotate", action="store_true",
        help="generate a FRESH token (default: reuse the one already in .env)",
    )
    p.add_argument(
        "--port", type=int, default=_DEFAULT_PORT,
        help=f"UI port to link (default {_DEFAULT_PORT})",
    )
    p.add_argument(
        "--no-write", action="store_true",
        help="print the plan without touching .env (dry run)",
    )
    return p


def _token_for_env(rotate: bool) -> str:
    """The token to use: existing one from .env, or a fresh one."""
    if not rotate:
        existing = read_env().get("DOURMOUSE_ACCESS_TOKEN", "")
        if existing:
            return existing
    return generate_token()


def main(argv: list[str] | None = None) -> int:
    args = _arg_parser().parse_args(argv)
    token = _token_for_env(args.rotate)
    addresses = detect_addresses()

    if not args.no_write:
        write_env("0.0.0.0", token)
        print("[ENV] DOURMOUSE_HOST=0.0.0.0 written to .env (remote reach).")
        print("[ENV] DOURMOUSE_ACCESS_TOKEN written to .env (auth gate).")
        print("[ENV] RESTART the UI server for the new binding to take effect:")
        print("        python -m dourmouse.webui")
    else:
        print("[DRY RUN] would write DOURMOUSE_HOST=0.0.0.0 + a token to .env")

    print()
    print("=" * 72)
    print("DOURMOUSE // PHONE LINK")
    print("=" * 72)

    if not addresses["found"]:
        print("NO PHONE-REACHABLE ADDRESS FOUND (honest).")
        print("  Check: is this machine on Wi-Fi/Ethernet?")
        print("  For anywhere-access: install Tailscale on this Mac and run")
        print("  'tailscale up' — then re-run this command.")
        return 0

    if args.no_write:
        # Dry run: never print a REAL token (it was not written, so pasting
        # it would fail). Show what the real run would do instead.
        existing = read_env().get("DOURMOUSE_ACCESS_TOKEN", "")
        token_display = existing if existing else "(fresh token — generated on the real run)"
    else:
        token_display = token if token else "(none)"
    for label, ips in (("LAN (same Wi-Fi)", addresses["lan"]), ("TAILSCALE (anywhere)", addresses["tailscale"])):
        for ip in ips:
            url = pairing_url(ip, args.port)
            print()
            print(f"--- {label}: {url} ---")
            qr = qr_ascii(url)
            if qr:
                print(qr)
            else:
                print("(QR unavailable — segno not installed; the URL above is the link)")
            print(f"  Token: {token_display}")

    print()
    print("ON YOUR PHONE:")
    print("  1. Same Wi-Fi  -> open the LAN URL (or scan its QR).")
    print("  2. Anywhere    -> install the Tailscale app, sign in to the")
    print("                    same account as this Mac, then open the")
    print("                    TAILSCALE URL (or scan its QR).")
    print("  3. Enter the token on the access gate (pasted above) — the")
    print("     session cookie then unlocks the dashboard.")
    print()
    print("Security: the token gates every non-loopback request; the phone")
    print("session is HttpOnly + SameSite=Strict. Rotate with --rotate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
