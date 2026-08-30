"""Provider-agnostic multi-account model router (Aider-AI/aider
architecture port, part 4/4).

Dourmouse already routes BETWEEN providers (dispatch.py's
DOURMOUSE_ORCHESTRATOR_BACKEND: claude / ollama_cloud / split). What this
adds is routing WITHIN one provider across multiple accounts — several
NVIDIA or Ollama Cloud API keys rotated automatically when one hits a
rate limit, so a single exhausted key never stalls the whole run.

Deterministic (Rule 2.8): account selection is round-robin over whichever
accounts are not currently cooling down, driven by real wall-clock time
and a real error message, never an LLM judgment call.

Rule 2.6: keys are read from env at call time and never logged — only an
account's short ``name`` (e.g. "nvidia-2") ever appears in a log line or
error message, never the key itself.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

#: How long one rate-limited account sits out before it's eligible again.
#: Chosen to comfortably outlast a typical per-minute NIM/Ollama Cloud
#: rate-limit window without being so long a transient 429 takes an
#: account out of rotation for an entire long session.
DEFAULT_COOLDOWN_SECONDS = 60.0

#: Substrings that identify a rate-limit/quota error across the providers
#: this router serves — real API responses observed in this codebase's own
#: error-handling modules (net_errors.py), not a guess.
_RATE_LIMIT_MARKERS = (
    "429",
    "rate limit",
    "rate_limit",
    "too many requests",
    "quota exceeded",
    "quota_exceeded",
)


def is_rate_limit_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _RATE_LIMIT_MARKERS)


@dataclass(frozen=True)
class Account:
    """One provider account. ``extra`` carries provider-specific fields
    (base_url, model, ...) the router itself never inspects — it only
    ever schedules accounts by name, never by what they're for."""

    name: str
    provider: str
    api_key: str
    extra: dict = field(default_factory=dict)


class AccountPool:
    """Round-robins over a provider's accounts, skipping any currently in
    cooldown. Empty/one-account pools degrade gracefully (never raise) —
    a pool of size 1 just always returns that one account until IT cools
    down, at which point ``select`` honestly returns None rather than
    pretending an unavailable account is fine to use.
    """

    def __init__(self, accounts: list[Account]) -> None:
        self._accounts = list(accounts)
        self._cooldown_until: dict[str, float] = {}
        self._cursor = 0

    def __len__(self) -> int:
        return len(self._accounts)

    def accounts(self) -> list[Account]:
        return list(self._accounts)

    def available(self, *, now: float | None = None) -> list[Account]:
        now = time.time() if now is None else now
        return [a for a in self._accounts if self._cooldown_until.get(a.name, 0.0) <= now]

    def is_cooling_down(self, name: str, *, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return self._cooldown_until.get(name, 0.0) > now

    def mark_rate_limited(
        self, name: str, *, cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS, now: float | None = None
    ) -> None:
        now = time.time() if now is None else now
        self._cooldown_until[name] = now + cooldown_seconds

    def clear_cooldown(self, name: str) -> None:
        self._cooldown_until.pop(name, None)

    def select(self, *, exclude: str | None = None, now: float | None = None) -> Account | None:
        """The next available account, round-robin. None if every account
        is cooling down (or the pool is empty) — the caller decides what
        "no account left" means for it (usually: surface the real error
        rather than silently degrading, Rule 2.2)."""
        avail = [a for a in self.available(now=now) if a.name != exclude]
        if not avail:
            avail = [] if exclude is None else self.available(now=now)
        if not avail:
            return None
        account = avail[self._cursor % len(avail)]
        self._cursor += 1
        return account


def accounts_from_env(provider: str, env_prefix: str) -> list[Account]:
    """``PREFIX`` (account 1), ``PREFIX_2``, ``PREFIX_3``, ... — a plain
    numbered-suffix convention so a second/third account needs nothing
    but another line in .env, no new config schema. Stops at the first
    gap (PREFIX_2 set but PREFIX_3 unset ends the scan at 2) so a typo'd
    later key can't silently vanish mid-list without at least being
    contiguous with what IS configured.
    """
    accounts: list[Account] = []
    first = os.environ.get(env_prefix, "").strip()
    if first:
        accounts.append(Account(name=f"{provider}-1", provider=provider, api_key=first))
    i = 2
    while True:
        val = os.environ.get(f"{env_prefix}_{i}", "").strip()
        if not val:
            break
        accounts.append(Account(name=f"{provider}-{i}", provider=provider, api_key=val))
        i += 1
    return accounts
