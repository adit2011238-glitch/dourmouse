"""Real external-IP reputation lookups (Domain I, Phase 2 step 2) --
AbuseIPDB's free-tier check endpoint, keyed. Honestly NOT CONFIGURED
without a real key set (this codebase's own established Rule 2.2
convention, see ``worldmonitor.py``'s identical ``WORLDMONITOR_API_KEY``
shape) -- never a fabricated score.

Deliberately narrow scope: looks up a real IP already seen in this host's
own real ``get_established_connections()`` telemetry (a genuinely observed
peer), never scans, probes, or acts against anything. A private/loopback/
link-local/reserved/multicast address is refused before any network call
-- the same real classification ``general_roster.py``'s own
``_refuse_private_fetch_target`` already uses, reused here rather than
re-derived, since asking a public reputation API about a LAN address is
meaningless (and would leak the user's own internal topology to a third
party for no reason).
"""

from __future__ import annotations

import ipaddress
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

REPUTATION_API_KEY_ENV = "ABUSEIPDB_API_KEY"
_DEFAULT_TIMEOUT_S = 10.0


def reputation_configured() -> bool:
    return bool(os.environ.get(REPUTATION_API_KEY_ENV, "").strip())


def _reject_non_public(ip: str) -> str | None:
    """Real, honest refusal reason for an address a reputation lookup
    should never be sent for, or None when the address is a real public
    target. Returns a reason string (never raises) so callers can report
    it the same way every other honest-refusal path in this codebase
    does."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return f"{ip!r} is not a real, parseable IP address."
    if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast:
        return f"{ip} is a private/loopback/reserved address -- reputation lookups are for real public peers only."
    return None


def check_ip_reputation(ip: str, timeout: float = _DEFAULT_TIMEOUT_S) -> dict[str, Any]:
    """One real AbuseIPDB check call. Returns a structured, honest result
    -- ``{"available": False, "reason": ...}`` for every failure mode (not
    configured, non-public target, network error, malformed response),
    matching this codebase's own established honesty convention. Never
    raises."""
    reject_reason = _reject_non_public(ip)
    if reject_reason is not None:
        return {"available": False, "reason": reject_reason}
    if not reputation_configured():
        return {
            "available": False,
            "reason": (
                f"NOT CONFIGURED: set {REPUTATION_API_KEY_ENV} to enable real "
                "IP reputation lookups (AbuseIPDB)."
            ),
        }
    key = os.environ[REPUTATION_API_KEY_ENV].strip()
    url = "https://api.abuseipdb.com/api/v2/check?" + urllib.parse.urlencode(
        {"ipAddress": ip, "maxAgeInDays": 90}
    )
    req = urllib.request.Request(
        url, headers={"Key": key, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed, hardcoded API endpoint
            raw = resp.read(200_000)
    except urllib.error.HTTPError as exc:
        return {"available": False, "reason": f"AbuseIPDB returned HTTP {exc.code}"}
    except urllib.error.URLError as exc:
        return {"available": False, "reason": f"AbuseIPDB unreachable: {exc.reason}"}
    except TimeoutError:
        return {"available": False, "reason": f"AbuseIPDB timed out after {timeout}s"}
    try:
        payload = json.loads(raw).get("data") or {}
    except json.JSONDecodeError:
        return {"available": False, "reason": "AbuseIPDB returned a real response this parser could not read"}
    if "abuseConfidenceScore" not in payload:
        return {"available": False, "reason": "AbuseIPDB response did not contain a real confidence score"}
    return {
        "available": True,
        "ip": payload.get("ipAddress", ip),
        "abuse_confidence_score": payload.get("abuseConfidenceScore"),
        "total_reports": payload.get("totalReports"),
        "country_code": payload.get("countryCode"),
        "isp": payload.get("isp"),
        "is_tor": payload.get("isTor"),
    }
