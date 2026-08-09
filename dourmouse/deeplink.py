"""Strict allow-list deep-link parser for the DOURMOUSE desktop shell (v5.19).

Deep links (``dourmouse://atlas``, ``dourmouse://portfolio``,
``dourmouse://world``, ``dourmouse://alerts``, ...) must NEVER reach the OS
with free-form input: a malicious external link must not be able to execute
commands or navigate anywhere it likes. This module is the single gate:

- the destination MUST be on the allow-list (home / atlas / world /
  portfolio / markets / intelligence / alerts / settings / command),
- each extra path segment must match ``[A-Za-z0-9_-]{1,64}`` (the one id
  segment the desktop portfolio allows),
- the result is a validated SPA hash route that the shell hands to
  ``location.hash`` — nothing else is ever derived from the input.

Everything else is dropped with an honest reason (Rule 2.2). The parser is
server-side and pytest-tested here so every platform (macOS / Windows /
Linux) shares the identical gate; ``deep_link_from_argv`` is the OS-launcher
helper (macOS registers the scheme via Info.plist ``CFBundleURLTypes``, the
OS re-launches the app with the link in argv, and ONLY the validated parse
of it may be used).

The HTTP transport is ``GET /api/deeplink?to=...`` (see webui.py), which is
token-gated off-loopback and serves the same allow-list.
"""

from __future__ import annotations

import re
from typing import Any

#: Allowed top-level destinations — the workspace ids the shell routes.
ALLOWED_DESTINATIONS = frozenset(
    {
        "home",
        "atlas",
        "world",
        "portfolio",
        "markets",
        "intelligence",
        "alerts",
        "settings",
        "command",
    }
)

#: One extra path segment: letters/digits/underscore/dash, 1-64 chars.
#: This is the ONLY charset that may ever appear in a deep-link id, so the
#: resulting href can never carry a path escape, scheme, or injection.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_SCHEME = "dourmouse://"
#: Bounded depth — a link can name a workspace plus a few id segments, never
#: an unbounded tail.
_MAX_SEGMENTS = 4
_MAX_HREF_LEN = 200

#: dest -> SPA hash route (the existing front-end hash router).
_DEST_HREFS = {
    "home": "#/",
    "atlas": "#/atlas",
    "world": "#/world",
    "portfolio": "#/portfolio",
    "markets": "#/markets",
    "intelligence": "#/intelligence",
    "alerts": "#/alerts",
    "settings": "#/settings",
    "command": "#/command",
}


def parse_deeplink(raw: str | None) -> dict[str, Any]:
    """Parse ONE deep link into a safe navigation target (never executes).

    Accepted forms (the ``dourmouse://`` scheme is optional — bare
    destinations work for the HTTP route and stay convenient for tests):

    - ``dourmouse://atlas``            -> ``{"ok": True, "href": "#/atlas"}``
    - ``dourmouse://atlas/research``   -> ``{"ok": True, "href": "#/atlas/research"}``
    - ``alerts``                       -> ``{"ok": True, "href": "#/alerts"}``

    Returns ``{"ok": True, "dest", "segments", "href"}`` on success or
    ``{"ok": False, "reason"}`` on anything off the allow-list. The ``href``
    only ever contains ``[A-Za-z0-9_/-#]`` — safe to assign to
    ``location.hash`` or emit as a 302 Location.
    """
    raw_input = raw or ""
    # Control characters are NEVER silently normalized away — an allow-list
    # parser must refuse them outright (a newline is not "sloppy whitespace",
    # it is a class of injection). Checked BEFORE strip so a trailing \n
    # cannot be laundered away; only ordinary edge whitespace the HTTP route
    # may add (e.g. ?to=alerts%20) is stripped below.
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw_input):
        return {"ok": False, "reason": "control characters in deep link"}
    raw = raw_input.strip()
    if not raw:
        return {"ok": False, "reason": "empty deep link"}
    value = raw
    if value.startswith(_SCHEME):
        value = value[len(_SCHEME):]
    elif value.startswith("dourmouse:"):
        return {"ok": False, "reason": "only the dourmouse:// scheme is valid"}
    value = value.strip("/")
    parts = [p for p in value.split("/") if p]
    if not parts:
        return {"ok": False, "reason": "no destination"}
    if len(parts) > _MAX_SEGMENTS:
        return {"ok": False, "reason": "too many path segments"}
    dest = parts[0].lower()
    if dest not in ALLOWED_DESTINATIONS:
        return {"ok": False, "reason": f"unknown destination {parts[0]!r}"}
    segments = parts[1:]
    for segment in segments:
        if not _ID_RE.match(segment):
            return {"ok": False, "reason": f"invalid segment {segment!r}"}
    href = _DEST_HREFS[dest]
    if segments:
        href = f"#/{dest}/{'/'.join(segments)}"
    if len(href) > _MAX_HREF_LEN:
        return {"ok": False, "reason": "deep link too long"}
    return {"ok": True, "dest": dest, "segments": segments, "href": href}


def deep_link_from_argv(argv: list[str] | None) -> str | None:
    """The first ``dourmouse://`` URL in argv, or None (OS-launcher helper).

    When the OS registers the URL scheme it launches the app with the link
    in argv; the shell passes argv here and feeds the result to
    ``parse_deeplink``. The raw argv text is never used directly — only the
    validated parse may navigate anything.
    """
    for arg in argv or []:
        if isinstance(arg, str) and arg.startswith(_SCHEME):
            return arg
    return None
