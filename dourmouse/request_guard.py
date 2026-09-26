"""Browser-borne request guard for the local server (finding #135).

The server trusts anything that connects from this machine: a loopback client
skips the access token (``webui._Handler._authorized``), so the desktop app and
local tools work with no configuration. That trust has a hole. Any web page open
in the owner's browser can make the browser connect to ``127.0.0.1``, and to the
server that request is indistinguishable from the app's own. Two well-known
attacks follow:

- **Cross-site request forgery.** A page on another site sends a plain POST
  (a form, or ``fetch`` with a simple content type, which needs no preflight)
  to a state-changing route such as ``/api/security/action``.
- **DNS rebinding.** A page on a site the attacker controls re-points its own
  name at ``127.0.0.1``. The requests are then "same-origin" to the browser and
  the attacker's script can read the answers too.

Both leave a mark the server can see and a browser cannot forge: the ``Host``
header names the attacker's site, and a state-changing request carries a foreign
``Origin`` (or ``Sec-Fetch-Site: cross-site``). This module is that check, as a
pure function so it can be tested without a server.

What it does not change: a non-browser client (curl, a script, a test) sends
neither ``Origin`` nor ``Sec-Fetch-Site`` and is allowed exactly as before. Any
process running as the owner can already read the owner's files, so it is not
the threat here. A client that is not on loopback is not judged here either: the
access token decides that.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Iterable
from typing import Protocol
from urllib.parse import urlsplit


class Headers(Protocol):
    """What the guard reads: a lookup by name that returns None when the header
    is absent. ``http.server``'s header object (case-insensitive) and a plain
    dict of canonical names both satisfy it."""

    def get(self, name: str, /) -> str | None: ...


_LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_ALLOWED_HOSTS_ENV = "DOURMOUSE_ALLOWED_HOSTS"


def extra_allowed_hosts() -> frozenset[str]:
    """Names the owner has added (for example a Tailscale name that a local
    proxy forwards from), comma separated in ``DOURMOUSE_ALLOWED_HOSTS``. An
    entry is a bare name (any port) or ``name:port`` (that port only)."""
    raw = os.environ.get(_ALLOWED_HOSTS_ENV, "")
    return frozenset(h.strip().lower() for h in raw.split(",") if h.strip())


def _is_loopback(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip == "localhost"
    mapped = getattr(addr, "ipv4_mapped", None)
    return (mapped or addr).is_loopback


def _split_host(value: str) -> tuple[str, int | None]:
    """``localhost:8765`` -> ("localhost", 8765); ``[::1]:8765`` -> ("::1", 8765)."""
    value = value.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return value, None
        name, rest = value[1:end], value[end + 1:]
        return name, int(rest[1:]) if rest.startswith(":") and rest[1:].isdigit() else None
    name, sep, port = value.rpartition(":")
    if sep and port.isdigit():
        return name, int(port)
    return value, None


def _is_ours(name: str, name_port: int, port: int, extra: frozenset[str] | set[str]) -> bool:
    """The server's own loopback name on its own port, or a name the owner added."""
    if name in extra or f"{name}:{name_port}" in extra:
        return True
    return name in _LOOPBACK_NAMES and name_port == port


def _foreign_origin(origin: str, extra: frozenset[str] | set[str], port: int) -> bool:
    try:
        parts = urlsplit(origin)
        origin_port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return True
    return parts.scheme not in ("http", "https") or not _is_ours((parts.hostname or "").lower(), origin_port, port, extra)


def is_cross_origin(headers: Headers, port: int, allowed_hosts: Iterable[str] = ()) -> bool:
    """True when the browser says the request comes from another origin, or
    from an opaque one (a sandboxed frame sends ``Origin: null``). A request
    that carries neither header is not from a browser and is not cross-origin."""
    site = headers.get("Sec-Fetch-Site")
    if site is not None and site.strip().lower() not in ("same-origin", "none"):
        return True
    origin = headers.get("Origin")
    if origin is None:
        return False
    return _foreign_origin(origin, {h.lower() for h in allowed_hosts}, port)


def check(
    method: str,
    headers: Headers,
    client_ip: str,
    port: int,
    allowed_hosts: Iterable[str] = (),
) -> str | None:
    """None when the request may proceed, else the reason it is refused."""
    if not _is_loopback(client_ip):
        return None  # a remote client is judged by the access token, not here
    extra = {h.lower() for h in allowed_hosts}

    host = headers.get("Host")
    if host is not None:
        name, host_port = _split_host(host)
        if not _is_ours(name, host_port if host_port is not None else 80, port, extra):
            return "host not allowed (a page on another site cannot address this server)"

    if method.upper() in _SAFE_METHODS:
        return None

    origin = headers.get("Origin")
    if origin is not None and _foreign_origin(origin, extra, port):
        return "cross-origin request refused"

    site = headers.get("Sec-Fetch-Site")
    if site is not None and site.strip().lower() not in ("same-origin", "none"):
        return "cross-site request refused"
    return None
