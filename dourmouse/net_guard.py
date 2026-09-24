"""SSRF-safe fetching of model-supplied URLs (finding #086, X-9 / R0-SEC).

The old guard in ``general_roster._refuse_private_fetch_target`` resolved the
host once, checked the first IPv4 address, then handed the URL to
``urllib.request.urlopen``. Three real holes:

1. ``urlopen`` follows redirects with no re-check, so a public page answering
   ``302 Location: http://169.254.169.254/`` reached the cloud metadata
   endpoint through a guard that had already passed.
2. The connection resolved DNS again after the check, so a rebinding name
   could answer public for the check and loopback for the connect.
3. Only ``gethostbyname``'s first IPv4 answer was checked, and the test was a
   list of ranges (private, loopback, ...) that misses non-global space such
   as CGNAT 100.64.0.0/10.

The fix here: every connection resolves the host itself, refuses the whole
answer if ANY address is not globally routable, and connects to the exact
address it vetted (TLS still verifies the certificate against the hostname).
Redirects go through the same connection path, so every hop is checked, and
are capped. Environment proxies are ignored: through a proxy the real
destination cannot be vetted.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

MAX_REDIRECTS = 5
_ALLOWED_SCHEMES = ("http", "https")


class FetchRefused(OSError):
    """The destination is not a public internet address, or the redirect
    chain broke a rule. Subclasses OSError because urllib wraps connect-time
    OSErrors in URLError; ``guarded_urlopen`` unwraps it again."""


def is_public_address(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Globally routable and unicast. ``is_global`` excludes private,
    loopback, link-local, reserved, unspecified, CGNAT and documentation
    space; an IPv4-mapped IPv6 address is judged by the IPv4 inside it."""
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return addr.is_global and not addr.is_multicast


def vet_host(host: str, port: int) -> list[tuple[Any, ...]]:
    """Resolve ``host`` and return its getaddrinfo entries, or raise
    FetchRefused if any resolved address is not globally routable. All
    answers are checked, not just the first: a name that returns one public
    and one internal address is refused outright."""
    # A name that does not resolve is a network failure, not a refusal:
    # socket.gaierror propagates so callers report it like any other.
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not infos:
        raise FetchRefused(f"could not resolve {host!r}: no addresses")
    for info in infos:
        addr = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
        if not is_public_address(addr):
            raise FetchRefused(
                f"{host!r} resolves to {addr} (a private/internal address); "
                "only the public web can be fetched"
            )
    return infos


def _connect_vetted(host: str, port: int, timeout: Any) -> socket.socket:
    last: OSError | None = None
    for family, socktype, proto, _canon, sockaddr in vet_host(host, port):
        sock = socket.socket(family, socktype, proto)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:  # type: ignore[attr-defined]
                sock.settimeout(timeout)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            sock.close()
            last = exc
    raise last or OSError(f"could not connect to {host!r}")


class _VettedHTTPConnection(http.client.HTTPConnection):
    def connect(self) -> None:
        self.sock = _connect_vetted(self.host, self.port, self.timeout)


class _VettedHTTPSConnection(http.client.HTTPSConnection):
    def connect(self) -> None:
        sock = _connect_vetted(self.host, self.port, self.timeout)
        ctx = getattr(self, "_context", None) or ssl.create_default_context()
        self.sock = ctx.wrap_socket(sock, server_hostname=self.host)


class _VettedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(_VettedHTTPConnection, req)


class _VettedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(_VettedHTTPSConnection, req)


class _CappedRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        scheme = urllib.parse.urlparse(newurl).scheme.lower()
        if scheme not in _ALLOWED_SCHEMES:
            raise FetchRefused(f"redirect to a non-web scheme refused: {newurl!r}")
        # The new hop is vetted when its connection opens, like the first.
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _VettedHTTPHandler(),
        _VettedHTTPSHandler(),
        _CappedRedirectHandler(),
    )


def guarded_urlopen(req: urllib.request.Request | str, timeout: float) -> Any:
    """``urlopen`` for model-supplied URLs. Raises FetchRefused (never a
    wrapped URLError) when any hop targets a non-public address or a
    non-web scheme; every other failure propagates exactly as urlopen's."""
    url = req.full_url if isinstance(req, urllib.request.Request) else req
    if urllib.parse.urlparse(url).scheme.lower() not in _ALLOWED_SCHEMES:
        raise FetchRefused(f"only http(s) URLs can be fetched, got {url!r}")
    try:
        return _build_opener().open(req, timeout=timeout)
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, FetchRefused):
            raise exc.reason from None
        raise
