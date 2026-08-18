"""Network failure taxonomy — honest errors that a human can act on.

The roster's rule is never to fabricate: a failed fetch must be reported, not
papered over. That rule was already kept, but the reports leaked transport
detail straight into chat ("HTTP 404 from https://query1.finance.yahoo.com/
v8/finance/chart/BANGLADESH?interval=1d&range=1d"). Honest, and useless.

This module keeps the honesty and fixes the delivery: `classify` maps an
exception (or an already-stringified failure) to a stable kind, and
`friendly` renders the kind as a sentence that says what failed and what to
do next. The raw detail is not discarded — it goes to the structured error
log via `dourmouse.obs`, so debugging keeps everything chat no longer shows.

Stdlib only, matching the rest of the package.
"""

from __future__ import annotations

import json
import re
import socket
import ssl
import urllib.error
from enum import Enum
from typing import Any

__all__ = ["ErrorKind", "classify", "friendly", "http_status", "report"]


class ErrorKind(str, Enum):
    """What went wrong, at the level a user's next action depends on."""

    NOT_FOUND = "not_found"          # 404/410 — the thing asked for isn't there
    RATE_LIMITED = "rate_limited"    # 429 — back off and retry
    AUTH = "auth"                    # 401/403 — credentials/permission
    SERVER_ERROR = "server_error"    # 5xx — their side, retry later
    TIMEOUT = "timeout"              # took too long
    OFFLINE = "offline"              # DNS/connection refused — no network
    TLS = "tls"                      # certificate/handshake failure
    PARSE = "parse"                  # reached it, could not read it
    UNKNOWN = "unknown"


# "HTTP 404 from https://..." is the shape live_feeds._http_get raises, so a
# already-stringified RuntimeError can still be classified without the
# original exception object.
_HTTP_IN_TEXT = re.compile(r"\bHTTP\s+(\d{3})\b")


def http_status(exc: BaseException | str) -> int | None:
    """Best-effort HTTP status for an exception or a failure string."""
    if isinstance(exc, urllib.error.HTTPError):
        return int(exc.code)
    text = exc if isinstance(exc, str) else str(exc)
    m = _HTTP_IN_TEXT.search(text)
    return int(m.group(1)) if m else None


def _kind_for_status(status: int) -> ErrorKind:
    if status in (401, 403):
        return ErrorKind.AUTH
    if status in (404, 410):
        return ErrorKind.NOT_FOUND
    if status == 429:
        return ErrorKind.RATE_LIMITED
    if 500 <= status <= 599:
        return ErrorKind.SERVER_ERROR
    return ErrorKind.UNKNOWN


def classify(exc: BaseException | str) -> ErrorKind:
    """Map an exception (or failure string) to an ErrorKind.

    Accepts strings because several call sites have already collapsed the
    original exception into a RuntimeError message by the time the roster
    handler sees it.
    """
    status = http_status(exc)
    if status is not None:
        kind = _kind_for_status(status)
        if kind is not ErrorKind.UNKNOWN:
            return kind

    if isinstance(exc, BaseException):
        # TimeoutError covers socket.timeout (alias since 3.10).
        if isinstance(exc, TimeoutError):
            return ErrorKind.TIMEOUT
        if isinstance(exc, ssl.SSLError):
            return ErrorKind.TLS
        if isinstance(exc, (json.JSONDecodeError, ValueError)):
            return ErrorKind.PARSE
        if isinstance(exc, urllib.error.URLError):
            reason = getattr(exc, "reason", None)
            if isinstance(reason, ssl.SSLError):
                return ErrorKind.TLS
            if isinstance(reason, TimeoutError):
                return ErrorKind.TIMEOUT
            if isinstance(reason, socket.gaierror):
                return ErrorKind.OFFLINE
            if isinstance(reason, ConnectionError):
                return ErrorKind.OFFLINE
            return ErrorKind.OFFLINE
        if isinstance(exc, ConnectionError):
            return ErrorKind.OFFLINE
        if isinstance(exc, socket.gaierror):
            return ErrorKind.OFFLINE

    text = (exc if isinstance(exc, str) else str(exc)).lower()
    if "timeout" in text or "timed out" in text:
        return ErrorKind.TIMEOUT
    if "tls" in text or "certificate" in text or "ssl" in text:
        return ErrorKind.TLS
    if "non-json" in text or "could not parse" in text or "unexpected response shape" in text:
        return ErrorKind.PARSE
    if "network error" in text or "nodename" in text or "name or service not known" in text:
        return ErrorKind.OFFLINE
    if "no items" in text or "no results" in text:
        return ErrorKind.NOT_FOUND
    return ErrorKind.UNKNOWN


# What the user should understand, and what they can do about it. `{what}` is
# the thing they asked for, filled in by the caller.
_TEMPLATES: dict[ErrorKind, str] = {
    ErrorKind.NOT_FOUND: "I couldn't find {what}. The source has no entry for it.",
    ErrorKind.RATE_LIMITED: "The source is rate-limiting me right now, so I couldn't get {what}. Worth retrying in a minute.",
    ErrorKind.AUTH: "I'm not authorised to read {what} — that source needs credentials I don't have configured.",
    ErrorKind.SERVER_ERROR: "The source is having problems on their end, so I couldn't get {what}. Worth retrying shortly.",
    ErrorKind.TIMEOUT: "The source took too long to answer, so I couldn't get {what}.",
    ErrorKind.OFFLINE: "I couldn't reach the network, so I couldn't get {what}.",
    ErrorKind.TLS: "I couldn't establish a secure connection to the source for {what}.",
    ErrorKind.PARSE: "I reached the source for {what} but couldn't read what it sent back.",
    ErrorKind.UNKNOWN: "I couldn't get {what}.",
}

# Kinds where retrying the same call is pointless — the caller should try a
# different route (e.g. fall back to web_search) instead of backing off.
_TERMINAL = frozenset({ErrorKind.NOT_FOUND, ErrorKind.AUTH, ErrorKind.PARSE})


def is_retryable(kind: ErrorKind) -> bool:
    """True when retrying the same request could plausibly succeed."""
    return kind not in _TERMINAL


_URL_RE = re.compile(r"https?://\S+")
# Covers both shapes seen in the wild: live_feeds' "HTTP 404 from <url>" and
# urllib's own "HTTP Error 404: Not Found".
_STATUS_RE = re.compile(
    r"\bHTTP\s+(?:Error\s+)?\d{3}\b\s*(?:from|:)?\s*", flags=re.IGNORECASE
)


def scrub(detail: str) -> str:
    """Strip transport noise (URLs, bare HTTP status) from a message.

    Used to keep a diagnostic string readable in chat without leaking the
    endpoint or status code that made the original report useless.
    """
    cleaned = _URL_RE.sub("", detail)
    cleaned = _STATUS_RE.sub("", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" \t-—:;,")
    return cleaned


def friendly(
    exc: BaseException | str,
    *,
    what: str,
    suggestion: str | None = None,
) -> str:
    """Render a failure as a sentence the user can act on.

    `what` names the thing that was being fetched, in object position — e.g.
    "a quote for BANGLADESH", "today's headlines". `suggestion` appends a
    concrete next step when the caller knows a better route.

    The underlying text is always appended, scrubbed. Scrubbing is what makes
    that safe: "HTTP 404 from https://query1.finance.yahoo.com/..." reduces to
    nothing and is dropped, while a real diagnostic ("no network", a parser
    complaint, a bug's message) survives intact. Dropping detail wholesale
    would trade an unreadable report for an undebuggable one.
    """
    kind = classify(exc)
    msg = _TEMPLATES[kind].format(what=what)
    detail = scrub(str(exc))
    if detail:
        msg = f"{msg} The tool reported: {detail}"
    if suggestion:
        msg = f"{msg} {suggestion}"
    return msg


def report(
    exc: BaseException | str,
    *,
    what: str,
    source: str,
    suggestion: str | None = None,
    extra: dict[str, Any] | None = None,
    prefix: str | None = None,
) -> str:
    """Log the raw failure, return the friendly message.

    This is the one call a roster handler needs: the transport detail lands in
    logs/errors.log for debugging, and the string handed back to the model
    (and thence the user) carries no URLs or status codes.

    `prefix` preserves the roster's long-standing "X FAILED (reported
    honestly):" convention, which ~20 other handlers still use and the model
    is accustomed to. Only the tail after it changes.
    """
    from dourmouse import obs

    kind = classify(exc)
    obs.log_error(
        source=source,
        kind=kind.value,
        what=what,
        detail=str(exc),
        status=http_status(exc),
        retryable=is_retryable(kind),
        extra=extra or {},
    )
    msg = friendly(exc, what=what, suggestion=suggestion)
    return f"{prefix} {msg}" if prefix else msg
