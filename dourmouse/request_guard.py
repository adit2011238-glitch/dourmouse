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


def is_loopback(ip: str) -> bool:
    """True for 127.0.0.0/8, ::1 (also IPv4-mapped) and the name localhost."""
    return _is_loopback(ip)


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


# --------------------------------------------------------------------------- #
# Owner-only routes (security review A5, phase H)
# --------------------------------------------------------------------------- #
#
# The Host and Origin checks above cannot tell the owner's app window from a
# page the model is driving: both are same-origin to this server once a
# browser tab is pointed at it. The routes below change what the model is
# allowed to do (approvals, auto-approve, the app-driving allow list, the
# security switches, the config file), so they need proof that the request
# comes from the owner's own window: a per-launch secret the server holds in
# memory only, delivered to the app window as an HttpOnly cookie (or sent by a
# trusted client as a header). A page in the browser pane lives in its own
# cookie partition and never receives it; the model's sandboxed shell has no
# network and no copy of it.

OWNER_COOKIE = "dourmouse_owner"
OWNER_HEADER = "X-Dourmouse-Owner"
OWNER_GATE_ENV = "DOURMOUSE_OWNER_GATE"

#: Exact POST paths that only the owner may call.
OWNER_ONLY_POST: frozenset[str] = frozenset({
    # Approving a gated tool call, or a goal's task, or a self-extension.
    "/api/confirm",
    "/api/goals/tasks/approve",
    "/api/self_extensions/approve",
    "/api/os/agentsmith/approve",
    # App driving (phase F1): the allow list, acting as the owner, and
    # releasing the kill switch. Engaging the kill switch stays open to all.
    "/api/os/apps/allow",
    "/api/os/apps/deny",
    "/api/os/apps/act",
    "/api/os/apps/resume",
    # The vision kill switch can be released through this route.
    "/api/vision/kill-switch",
    # Raising the conversation's role, switching the model backend.
    "/api/role",
    "/api/backend",
    # Standing jobs run tools unattended.
    "/api/schedules",
    "/api/schedules/update",
    "/api/schedules/toggle",
    "/api/schedules/remove",
    "/api/os/goals/create",
    "/api/os/browser/history/clear",
    "/api/os/browser/bookmarks/remove",
    # Destructive housekeeping.
    "/api/projects/delete",
    "/api/artifacts/clear",
})

#: POST path prefixes that only the owner may call.
OWNER_ONLY_POST_PREFIXES: tuple[str, ...] = (
    "/api/settings/",     # auto-approve, API keys, scopes, model, features
    "/api/os/settings/",  # the shell's toggles, features, reset
    "/api/os/comms/",     # trash, archive and flag act on the owner's mailbox
    "/api/security/",     # dismissals, lockdown on and off, quarantine
    "/api/setup/",        # writes the config file and restarts the server
)

_OWNER_SECRET_MIN = 32
_OWNER_SECRET_MAX = 256


def is_owner_route(method: str, path: str) -> bool:
    """True when ``method path`` is one of the owner-only actions above."""
    if method.upper() not in ("POST", "PUT", "PATCH", "DELETE"):
        return False
    if path in OWNER_ONLY_POST:
        return True
    if path.startswith("/api/atlas-lab/proposals/") and path.endswith("/approve"):
        return True  # approving runs model-written code in the sandbox
    return any(path.startswith(prefix) for prefix in OWNER_ONLY_POST_PREFIXES)


def valid_owner_secret(value: str | None) -> bool:
    """A usable secret: 32 to 256 URL-safe characters (what ``secrets.token_urlsafe``
    and Node's ``randomBytes(32).toString("base64url")`` produce)."""
    if not value or not (_OWNER_SECRET_MIN <= len(value) <= _OWNER_SECRET_MAX):
        return False
    return all(c.isascii() and (c.isalnum() or c in "-_") for c in value)


def _cookie_value(headers: Headers, name: str) -> str | None:
    for part in (headers.get("Cookie") or "").split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key == name:
            return value.strip()
    return None


def owner_proof_matches(headers: Headers, secret: str) -> bool:
    """True when the request carries the per-launch owner secret, as the
    ``dourmouse_owner`` cookie or the ``X-Dourmouse-Owner`` header. Compared in
    constant time, as bytes (a non-ASCII value cannot raise)."""
    import hmac

    if not secret:
        return False
    expected = secret.encode("utf-8")
    for candidate in (headers.get(OWNER_HEADER), _cookie_value(headers, OWNER_COOKIE)):
        if candidate and hmac.compare_digest(candidate.strip().encode("utf-8", "replace"), expected):
            return True
    return False


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
