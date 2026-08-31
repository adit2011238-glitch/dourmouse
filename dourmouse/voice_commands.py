"""Bounded voice/text command grammar for the Vision workspace
(world-monitor-expansion, item 3: "Voice commands").

WHAT THIS IS: a small, deterministic (Rule 2.8 — no LLM judgment involved
in recognizing a command) regex-based parser over a FIXED, real, bounded set
of imperatives, matching the task brief's own framing exactly: "a real,
bounded, testable command set" rather than "a full open-ended NLU system".
Four real commands are recognized:

    "email <person> saying/that/: <message>"  -> action "email"
    "open <panel>"                            -> action "open_panel"
    "close <panel>"                           -> action "close_panel"
    "search for <query>" / "search <query>"   -> action "search"

Anything that doesn't match one of these four shapes returns None — the
caller (ui/workspace.html) falls back to treating the utterance as an
ordinary chat turn to the companion agent, exactly the same honest
degradation dourmouse/wakeword.py and dourmouse/voice.py use elsewhere:
never guess at intent, never silently drop an utterance.

WHERE THIS RUNS: this module has zero dependencies beyond the stdlib, so it
is called from BOTH sides of the real round trip —
webui.py's POST /api/voice/command (the single source of truth the browser
actually calls, so the parsing logic lives in exactly one place, never
duplicated in JS) and directly from dourmouse/tests/test_voice_commands.py
(pure function, no server needed to test the grammar itself).

Honesty (Rule 2.2): this module only PARSES text into a structured command.
It does not itself call any tool, send any email, or open any window —
ui/workspace.html's dispatcher is what turns a parsed VoiceCommand into a
real /api/chat call or a real panel action, and it is that dispatcher (not
this module) that owns whatever confirmation gating the underlying action
already requires (e.g. gmail_send is REQUIRES_CONFIRMATION regardless of
whether the request that reached it came from typed text, a click, or a
voice command — this module does not and cannot bypass that).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: Real panel ids the Vision workspace actually renders (ui/workspace.html).
#: Kept here (not guessed per-call) so "open"/"close" resolution is the same
#: deterministic lookup every time, and so a typo'd panel name is reported
#: honestly (returns None -> "I didn't recognize that panel") instead of
#: silently opening the wrong one.
_PANEL_ALIASES: dict[str, str] = {
    "mail": "mail", "email": "mail", "inbox": "mail", "gmail": "mail",
    "gmail inbox": "mail", "mail panel": "mail",
    "chat": "chat", "companion": "chat", "companion chat": "chat",
    "assistant": "chat",
    "map": "map", "world": "map", "world map": "map", "worldmap": "map",
    "pulse": "map", "world pulse": "map",
    "research": "research", "research panel": "research",
    "globe": "globe", "3d": "globe", "3d view": "globe",
    "god's eye": "globe", "gods eye": "globe", "god's eye view": "globe",
    "3d model": "design3d", "design3d": "design3d", "3d design": "design3d",
    "3d editor": "design3d", "model editor": "design3d",
}

_EMAIL_RE = re.compile(
    r"^\s*email\s+(?P<person>[\w.@'\- ]+?)\s*(?:\s(?:saying|that)|:)\s+(?P<message>.+?)\s*$",
    re.IGNORECASE,
)
_OPEN_RE = re.compile(
    r"^\s*open\s+(?:the\s+)?(?P<panel>[\w' ]+?)\s*$",
    re.IGNORECASE,
)
_CLOSE_RE = re.compile(
    r"^\s*close\s+(?:the\s+)?(?P<panel>[\w' ]+?)\s*$",
    re.IGNORECASE,
)
# Two separate patterns (tried in order) rather than one with an optional
# "for" -- a single "search(?:\s+for)?\s+(query)" pattern is ambiguous on
# "search for" with nothing after it: the regex engine backtracks to treat
# "for" itself as the query instead of failing, which would wrongly turn a
# genuinely empty search into a command. Trying the explicit "for" form
# first, then a plain form that refuses to start with a bare "for", removes
# that ambiguity entirely.
_SEARCH_FOR_RE = re.compile(
    r"^\s*search\s+for\s+(?P<query>.+?)\s*$",
    re.IGNORECASE,
)
_SEARCH_PLAIN_RE = re.compile(
    r"^\s*search\s+(?P<query>(?!for\b).+?)\s*$",
    re.IGNORECASE,
)

#: Trailing noise words stripped from an "open"/"close" target before panel
#: lookup, so "open the mail panel" and "open mail" resolve identically.
_TRAILING_NOISE = re.compile(r"\s+(?:panel|window)$", re.IGNORECASE)


@dataclass(frozen=True)
class VoiceCommand:
    """One recognized, bounded command. ``args`` is the exact, minimal set
    of fields the action needs — never a grab-bag of the raw match."""

    action: str
    args: dict[str, Any]
    raw: str

    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action, "args": self.args, "raw": self.raw}


def resolve_panel(raw: str) -> str | None:
    """Normalize a spoken/typed panel reference to a real panel id, or None
    when it doesn't match any panel this workspace actually has. Exposed
    separately from parse_voice_command so callers (and tests) can probe
    panel-name resolution on its own."""
    key = _TRAILING_NOISE.sub("", (raw or "").strip().lower())
    return _PANEL_ALIASES.get(key)


def parse_voice_command(text: str) -> VoiceCommand | None:
    """Parse one utterance against the fixed 4-command grammar above.

    Returns None for anything that doesn't match — including a
    grammatically-close near-miss (e.g. "open" with no panel named, or an
    unrecognized panel name) — so the caller can fall back to ordinary chat
    rather than acting on a guess. Never raises."""
    stripped = (text or "").strip()
    if not stripped:
        return None

    m = _EMAIL_RE.match(stripped)
    if m:
        person = m.group("person").strip()
        message = m.group("message").strip()
        if person and message:
            return VoiceCommand("email", {"person": person, "message": message}, stripped)

    m = _OPEN_RE.match(stripped)
    if m:
        panel = resolve_panel(m.group("panel"))
        if panel:
            return VoiceCommand("open_panel", {"panel": panel}, stripped)

    m = _CLOSE_RE.match(stripped)
    if m:
        panel = resolve_panel(m.group("panel"))
        if panel:
            return VoiceCommand("close_panel", {"panel": panel}, stripped)

    m = _SEARCH_FOR_RE.match(stripped) or _SEARCH_PLAIN_RE.match(stripped)
    if m:
        query = m.group("query").strip()
        if query:
            return VoiceCommand("search", {"query": query}, stripped)

    return None


def available_commands() -> list[dict[str, str]]:
    """The real, fixed command list — for the workspace's own on-screen
    "what can I say" help, so the UI never invents documentation that drifts
    from what this parser actually recognizes."""
    return [
        {"pattern": "email <person> saying <message>",
         "example": "email sam saying I'm running ten minutes late"},
        {"pattern": "open <panel>",
         "example": "open mail  (panels: mail, chat, map, research, globe)"},
        {"pattern": "close <panel>",
         "example": "close research"},
        {"pattern": "search for <query>",
         "example": "search for nvidia earnings"},
    ]
