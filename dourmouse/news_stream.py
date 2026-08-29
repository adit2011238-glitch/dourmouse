"""News stream (v13) — a forever-refreshing, push-on-important feed.

The gap this closes, stated directly by the user: the NEWS screen was a
static "ask below — the news agent fetches them live" placeholder (see
ui/console.html's paintNews) — nothing updated on its own, and nothing
ever surfaced an important event without being explicitly prompted first.
This module is the backend half of "our own customizable news mile app
within Dourmouse that updates without us having to prompt": a background
daemon thread polls real, already-proven sources on an interval, and
PUSHES every new item — and specifically flags "important" ones — over
the same real-time broadcast hub (``dourmouse.webui``'s ``_SSEBroadcast``,
the exact mechanism the Freebuff live-activity feed already uses) that
every connected browser tab is already listening on via ``GET /api/events``.

Deliberately reuses world_pulse.py's already-real, already-tested source
functions rather than inventing a second data-fetching layer:
``_fetch_disasters`` (GDACS — real alert levels Green/Orange/Red, mapped
to this module's own severity scale below), ``_fetch_conflict_events``
(GDELT), and ``_fetch_news`` (Google News, WORLD/EUROPE/APAC editions).
All three are genuinely real, keyless (or already-keyed) and live — no
new external dependency, no new API key, no new failure surface beyond
what world_pulse.py already carries and has already been verified against
live data (see the project's own change log for the verification history
of each of these three fetchers).

"Important" is deliberately simple and stated plainly rather than
oversold: an item is important when its own ``severity`` field (already a
real signal every world_pulse source computes today) is ``"critical"`` or
``"high"``. No sentiment analysis, no keyword scoring invented here — the
severity signal already exists and is already real; layering a second,
unproven "importance" heuristic on top would be exactly the kind of
fabricated-precision this codebase's own rules (2.1/2.2) exist to prevent.
"""

from __future__ import annotations

import hashlib
import threading
import time
from typing import Any, Callable

#: How often to poll every source, in seconds. 180s (3 minutes) — frequent
#: enough that "forever refreshing" is true in practice, far short of any
#: rate-limit concern for RSS-shaped feeds already polled at similar or
#: tighter intervals elsewhere in this codebase (world_pulse.py's own
#: cache TTL defaults to a comparable window).
_POLL_INTERVAL = 180.0
#: Backoff after a fully-failed poll cycle (every source raised) before
#: retrying — matches the spirit of freebuff_events.py's own backoff
#: table without importing it (a different failure shape: RSS polling has
#: no persistent connection to reconnect, just a next-tick retry).
_ERROR_BACKOFF = 30.0
#: Bounded memory: only the most recent N deduplication keys are
#: remembered, and only the most recent M items are kept for a fresh
#: subscriber's initial catch-up list. Long-running, unbounded growth is
#: exactly the kind of thing a "forever refreshing" background process
#: must never do.
_MAX_SEEN = 2_000
_MAX_RECENT = 200

#: Real, already-proven severities world_pulse.py's own sources emit.
#: Deliberately a closed set matched against exactly, not a substring
#: check — an unrecognized/absent severity is honestly "not important"
#: rather than guessed either way.
_IMPORTANT_SEVERITIES = {"critical", "high"}


def _default_sources() -> list[tuple[str, Callable[[], list[dict[str, Any]]]]]:
    """The real source functions, imported lazily so importing this module
    never requires world_pulse.py's own heavier dependencies up front, and
    so tests can substitute fakes without touching the real one."""
    from dourmouse import world_pulse

    return [
        ("disasters", world_pulse._fetch_disasters),
        ("conflict_events", world_pulse._fetch_conflict_events),
        ("news", world_pulse._fetch_news),
    ]


def _dedup_key(channel: str, item: dict[str, Any]) -> str:
    """A stable id for an item that has no guid of its own across these
    three source shapes — title+link is what world_pulse.py's own items
    reliably carry (see ``_item()``'s docstring), hashed so the seen-set
    stores fixed-size keys regardless of title length."""
    raw = f"{channel}|{item.get('title', '')}|{item.get('link', '')}"
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:24]


class NewsStreamWatcher:
    """Background poller — one real daemon thread, real HTTP fetches on a
    real interval, pushing over an injectable ``sink``.

    ``sink`` receives ``{"type": "news_item", "item": {...}}`` for every
    genuinely NEW item (deduplicated across restarts of the poll loop, not
    across a real process restart — matching ActivityTracker/AttentionQueue's
    own already-accepted in-memory, process-lifetime tradeoff). The item
    dict carries everything world_pulse.py's own ``_item()`` already
    produces (title/summary/link/at/severity, optionally lat/lon/country)
    plus ``channel`` (which source it came from) and ``important`` (bool,
    per this module's own severity-based rule above). The sink must never
    raise — a broken subscriber must never take down the poll loop, the
    same discipline FreebuffEventWatcher already establishes.
    """

    def __init__(
        self,
        sink: Callable[[dict[str, Any]], None],
        *,
        sources: list[tuple[str, Callable[[], list[dict[str, Any]]]]] | None = None,
        poll_interval: float = _POLL_INTERVAL,
    ) -> None:
        self._sink = sink
        self._sources = sources if sources is not None else _default_sources()
        self._poll_interval = poll_interval
        self._seen: list[str] = []  # insertion-ordered for the bounded trim below
        self._seen_set: set[str] = set()
        self._recent: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_poll_ok: bool | None = None
        self._last_error: str = ""

    # -- lifecycle -------------------------------------------------------- #

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="news-stream", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def recent(self, limit: int = 50, *, important_only: bool = False) -> list[dict[str, Any]]:
        """Most recently seen items, newest first — the catch-up list a
        freshly-opened NEWS screen reads once before live pushes take over."""
        with self._lock:
            items = list(reversed(self._recent))
        if important_only:
            items = [i for i in items if i.get("important")]
        return items[: max(1, int(limit))]

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._thread is not None and self._thread.is_alive(),
                "last_poll_ok": self._last_poll_ok,
                "last_error": self._last_error,
                "seen_count": len(self._seen),
                "poll_interval_seconds": self._poll_interval,
            }

    # -- internals -------------------------------------------------------- #

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll_once()
                with self._lock:
                    self._last_poll_ok = True
                    self._last_error = ""
                wait = self._poll_interval
            except Exception as exc:  # noqa: BLE001 -- a poll cycle must never kill the thread
                with self._lock:
                    self._last_poll_ok = False
                    self._last_error = str(exc)[:300]
                wait = _ERROR_BACKOFF
            if self._stop.wait(timeout=wait):
                return

    def _poll_once(self) -> None:
        any_source_ok = False
        last_exc: Exception | None = None
        for channel, fetch in self._sources:
            try:
                items = fetch()
            except Exception as exc:  # noqa: BLE001 -- one dead source must not skip the rest
                last_exc = exc
                continue
            any_source_ok = True
            for raw_item in items:
                key = _dedup_key(channel, raw_item)
                with self._lock:
                    if key in self._seen_set:
                        continue
                    self._seen_set.add(key)
                    self._seen.append(key)
                    if len(self._seen) > _MAX_SEEN:
                        stale = self._seen[: len(self._seen) - _MAX_SEEN]
                        self._seen = self._seen[len(self._seen) - _MAX_SEEN :]
                        self._seen_set.difference_update(stale)
                item = dict(raw_item)
                item["channel"] = channel
                item["important"] = item.get("severity") in _IMPORTANT_SEVERITIES
                with self._lock:
                    self._recent.append(item)
                    if len(self._recent) > _MAX_RECENT:
                        del self._recent[: len(self._recent) - _MAX_RECENT]
                self._emit({"type": "news_item", "item": item})
        # Every source failing in the same cycle is the honest "poll
        # failed" case the outer loop's except-block backs off on; ANY
        # source succeeding means real progress was made this cycle even
        # if another one is currently down.
        if not any_source_ok and last_exc is not None:
            raise last_exc

    def _emit(self, payload: dict[str, Any]) -> None:
        try:
            self._sink(payload)
        except Exception:  # noqa: BLE001 -- a broken subscriber never breaks the poller
            pass
