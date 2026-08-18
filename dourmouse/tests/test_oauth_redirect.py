"""Google only accepts plain-http OAuth redirects on loopback.

Reported symptom: "the localhost throws an OAuth error". The real cause was
the server being reached over a Tailscale 100.x address, which made the
derived redirect URI non-loopback http. Google rejects that with
"Error 400: invalid_request" on its own page, where nothing explains why —
so the failure has to be caught and explained on our side.
"""

from __future__ import annotations

import urllib.parse

import pytest


def redirect_host_is_acceptable(redirect_uri: str) -> bool:
    """The rule the handler enforces, isolated for direct testing."""
    host = urllib.parse.urlparse(redirect_uri).hostname or ""
    return host in ("127.0.0.1", "::1", "localhost") or redirect_uri.startswith("https://")


ACCEPTED = [
    "http://127.0.0.1:8765/api/auth/google/callback",
    "http://localhost:8765/api/auth/google/callback",
    "http://[::1]:8765/api/auth/google/callback",
    "https://dourmouse.example.com/api/auth/google/callback",
    "https://192.168.1.242:8765/api/auth/google/callback",
]

REJECTED = [
    "http://100.98.97.23:8765/api/auth/google/callback",   # the reported case
    "http://192.168.1.242:8765/api/auth/google/callback",  # LAN, plain http
    "http://desktop-4u4t12k:8765/api/auth/google/callback",
    "http://10.0.0.5:8765/api/auth/google/callback",
]


@pytest.mark.parametrize("uri", ACCEPTED)
def test_loopback_and_https_are_allowed(uri):
    assert redirect_host_is_acceptable(uri)


@pytest.mark.parametrize("uri", REJECTED)
def test_non_loopback_plain_http_is_rejected(uri):
    assert not redirect_host_is_acceptable(uri)


def test_the_exact_reported_uri_is_rejected():
    """Pin the specific address that produced the bug report."""
    assert not redirect_host_is_acceptable(
        "http://100.98.97.23:8765/api/auth/google/callback"
    )


def test_https_on_a_vpn_address_is_fine():
    """It is the plain-http part that Google objects to, not the address."""
    assert redirect_host_is_acceptable("https://100.98.97.23:8765/api/auth/google/callback")


# --------------------------------------------------------------------------- #
# the env override
# --------------------------------------------------------------------------- #

def build_redirect(pinned: str | None, host_header: str, server_port: int) -> str:
    """Mirrors _google_redirect_uri's precedence: the pin wins outright."""
    if pinned:
        return pinned.rstrip("/") + "/api/auth/google/callback"
    host, _, port = host_header.rpartition(":")
    if not host:
        host, port = host_header, str(server_port)
    return f"http://{host}:{port or server_port}/api/auth/google/callback"


def test_pin_overrides_a_vpn_host_header():
    """The fix: however the browser reached us, send the registered URI."""
    out = build_redirect("http://127.0.0.1:8765", "100.98.97.23:8765", 8765)
    assert out == "http://127.0.0.1:8765/api/auth/google/callback"
    assert redirect_host_is_acceptable(out)


def test_pin_tolerates_a_trailing_slash():
    assert build_redirect("http://127.0.0.1:8765/", "x:1", 1).startswith(
        "http://127.0.0.1:8765/api/auth/google/callback"
    )


def test_without_a_pin_the_host_header_still_drives_it():
    out = build_redirect(None, "127.0.0.1:8765", 8765)
    assert out == "http://127.0.0.1:8765/api/auth/google/callback"


def test_without_a_pin_a_vpn_host_produces_the_broken_uri():
    """Documents why the pin exists."""
    out = build_redirect(None, "100.98.97.23:8765", 8765)
    assert not redirect_host_is_acceptable(out)
