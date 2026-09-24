"""Connection failure taxonomy (MS-6, spec item 6, finding #104).

"It won't load" has one of a handful of real causes, and each has a
different fix. diagnose() walks the connection the way a network engineer
would (name lookup, then a TCP connection, then TLS, then an HTTP request)
and stops at the first step that fails, returning exactly one category with
the evidence for it:

  OK          everything worked
  BLOCKED     the name resolves to a sinkhole (0.0.0.0 / 127.0.0.1 / ::),
              e.g. a lockdown or a filtering DNS
  DNS         the name does not resolve at all
  ROUTING     no route to the host or network (the local network is the problem)
  UNREACHABLE the host actively refused the connection (nothing listening, or a
              firewall rejecting)
  TIMEOUT     the connection or handshake never answered (a silent firewall,
              a dead host, or a network black hole)
  TLS         the connection works but the encrypted session fails (a bad or
              intercepted certificate, a protocol mismatch)
  SERVER      the server answered with an error (HTTP 5xx)
  UNKNOWN     something else; the evidence says what

It never guesses: a category is only assigned from an observed failure at
that step. Diagnosing an internal address is refused like fetch_url's
(net_guard), except for loopback when explicitly allowed for testing.
"""

from __future__ import annotations

import errno
import http.client
import ipaddress
import socket
import ssl
import time
import urllib.parse
from typing import Any

OK, BLOCKED, DNS, ROUTING, UNREACHABLE, TIMEOUT, TLS, SERVER, UNKNOWN = (
    "OK", "BLOCKED", "DNS", "ROUTING", "UNREACHABLE", "TIMEOUT", "TLS", "SERVER", "UNKNOWN",
)
_SINKHOLES = {"0.0.0.0", "::", "127.0.0.1", "::1"}  # noqa: S104 -- compared, never bound
_ROUTING_ERRNOS = {errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN, errno.EHOSTDOWN}


def _step(steps: list[dict[str, Any]], name: str, ok: bool, detail: str, t0: float) -> None:
    steps.append({"step": name, "ok": ok, "detail": detail, "ms": round((time.monotonic() - t0) * 1000, 1)})


def diagnose(target: str, *, timeout: float = 6.0, allow_loopback: bool = False) -> dict[str, Any]:
    url = target if "://" in target else f"https://{target}"
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    tls = parts.scheme == "https"
    port = parts.port or (443 if tls else 80)
    steps: list[dict[str, Any]] = []

    def done(category: str, why: str) -> dict[str, Any]:
        return {"target": target, "host": host, "port": port, "category": category, "why": why, "steps": steps}

    t0 = time.monotonic()
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        _step(steps, "dns", False, str(exc), t0)
        return done(DNS, f"{host} does not resolve: {exc}")
    addrs = sorted({str(i[4][0]) for i in infos})
    _step(steps, "dns", True, ", ".join(addrs), t0)
    if set(addrs) <= _SINKHOLES and not (allow_loopback and set(addrs) <= {"127.0.0.1", "::1"}):
        return done(BLOCKED, f"{host} resolves to {', '.join(addrs)}: it is being blocked on purpose "
                             "(a lockdown, a hosts entry, or a filtering DNS)")
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if not ip.is_global and not (allow_loopback and ip.is_loopback):
            return done(UNKNOWN, f"{host} resolves to the internal address {a}; not diagnosed (internal targets are refused)")

    family, _, _, _, sockaddr = infos[0]
    t0 = time.monotonic()
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(sockaddr)
    except TimeoutError:
        sock.close()
        _step(steps, "tcp", False, f"no answer within {timeout}s", t0)
        return done(TIMEOUT, f"{sockaddr[0]}:{port} never answered: a silent firewall, a dead host, or a black hole")
    except ConnectionRefusedError:
        sock.close()
        _step(steps, "tcp", False, "connection refused", t0)
        return done(UNREACHABLE, f"{sockaddr[0]}:{port} refused the connection: nothing is listening, or a firewall rejects it")
    except OSError as exc:
        sock.close()
        _step(steps, "tcp", False, str(exc), t0)
        if exc.errno in _ROUTING_ERRNOS:
            return done(ROUTING, f"no route to {sockaddr[0]}: the problem is this Mac's network or its router")
        return done(UNKNOWN, f"TCP connection failed: {exc}")
    _step(steps, "tcp", True, f"connected to {sockaddr[0]}:{port}", t0)

    conn_sock: socket.socket | ssl.SSLSocket = sock
    if tls:
        t0 = time.monotonic()
        ctx = ssl.create_default_context()
        try:
            conn_sock = ctx.wrap_socket(sock, server_hostname=host)
        except ssl.SSLCertVerificationError as exc:
            sock.close()
            _step(steps, "tls", False, exc.verify_message or str(exc), t0)
            return done(TLS, f"the certificate for {host} is not trusted ({exc.verify_message}): expired, wrong "
                             "name, or someone intercepting the connection")
        except (ssl.SSLError, TimeoutError, OSError) as exc:
            sock.close()
            _step(steps, "tls", False, str(exc), t0)
            return done(TLS, f"the encrypted session with {host} failed: {exc}")
        cert: dict[str, Any] = dict(conn_sock.getpeercert() or {})
        issuer = "?"
        for rdn in cert.get("issuer", ()):
            for key, value in rdn:
                if key == "organizationName":
                    issuer = str(value)
        _step(steps, "tls", True, f"{conn_sock.version()}, certificate issued by {issuer}", t0)

    t0 = time.monotonic()
    try:
        req = f"HEAD {parts.path or '/'} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: dourmouse-diagnose/1.0\r\nConnection: close\r\n\r\n"
        conn_sock.sendall(req.encode("ascii", "ignore"))
        resp = http.client.HTTPResponse(conn_sock)  # type: ignore[arg-type]
        resp.begin()
        status = resp.status
    except (OSError, http.client.HTTPException) as exc:
        _step(steps, "http", False, str(exc), t0)
        return done(UNKNOWN, f"connected, but the HTTP exchange failed: {exc}")
    finally:
        conn_sock.close()
    _step(steps, "http", status < 500, f"HTTP {status}", t0)
    if status >= 500:
        return done(SERVER, f"{host} is reachable but its server answered HTTP {status} (the fault is on their side)")
    return done(OK, f"{host} works (HTTP {status})")
