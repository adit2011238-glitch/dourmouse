"""Freebuff live activity watcher (v5.9) — thread activity as it happens.

The Freebuff Desktop app streams its own UI events over a loopback SSE
endpoint (``GET /api/events`` on ``FREEBUFF_API_URL``). This module turns
that firehose into a small, bounded stream of HUMAN-MEANINGFUL activity
events: a thread starting a turn, finishing one, being created, or changing
status. Dourmouse's HUD subscribes (via GET /api/events fan-out) and renders
them live in the pipeline feed.

Design (Rule 2.2 — honest, never fabricated):

- ``FreebuffEventWatcher`` runs one background thread that reads the SSE
  stream line by line, tracking the last-known state per thread
  (``{status, turnState, updatedAt}``). It emits an activity event ONLY on
  a real transition: idle->running (turn started), running->idle (turn
  finished), open<->closed (status), or a brand-new thread. Repeated
  identical snapshots emit nothing — no spam.
- Every emitted event carries the real thread id/title/status/turnState
  from the app; titles are truncated and newline-collapsed only.
- Reconnect: if the app is unreachable or the stream drops, the watcher
  backs off and retries; it emits a single ``watch_status`` event on
  connect/disconnect so the HUD honestly shows the watch is offline
  instead of silently going quiet.
- Bounded: only the most recent events are kept in the ring (for tests and
  late subscribers), and per-thread transition spam is rate-limited.

Tests point the watcher at a fake SSE server via FREEBUFF_API_URL — never a
live expectation.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from typing import Any

_FREEBUFF_BASE = os.environ.get("FREEBUFF_API_URL", "http://127.0.0.1:51819").strip().rstrip("/")
_STREAM_PATH = "/api/events"

# Backoff schedule for reconnect attempts (seconds).
_BACKOFF = (1.0, 2.0, 4.0, 8.0)
# Keep the most recent N activity events for late subscribers.
_MAX_EVENTS = 50
# A thread that transitions within this many seconds of its last emitted
# event is rate-limited (prevents a flapping turn from flooding the feed).
_MIN_EVENT_GAP = 2.0

# Freebuff thread states (observed live on the app's own stream).
_STATE_RUNNING = "running"
_STATE_IDLE = "idle"


class FreebuffWatchError(RuntimeError):
    """The Freebuff events stream is not reachable."""


def _collapse_title(title: Any) -> str:
    """Newline-collapse + truncate a thread title for feed display."""
    if not isinstance(title, str):
        return ""
    return " ".join(title.split())[:120]


def _thread_key(thread: dict[str, Any]) -> tuple[str, str, Any]:
    """The state fields we diff on: status, turnState, updatedAt."""
    return (
        str(thread.get("status") or ""),
        str(thread.get("turnState") or ""),
        thread.get("updatedAt"),
    )


class FreebuffEventWatcher:
    """Background SSE consumer emitting bounded, meaningful activity events.

    ``sink`` receives ``{"type": "freebuff_activity", "activity": {...}}``
    and ``{"type": "freebuff_watch", "state": "online"|"offline"}``. The
    sink must never raise (a broken subscriber never kills the watcher).
    """

    def __init__(
        self,
        sink: Callable[[dict[str, Any]], None],
        *,
        base_url: str | None = None,
        stream_path: str = _STREAM_PATH,
    ) -> None:
        self._sink = sink
        self._base = (base_url or _FREEBUFF_BASE).rstrip("/")
        self._path = stream_path
        self._known: dict[str, tuple[str, str, Any]] = {}
        self._last_emit: dict[tuple[str, str], float] = {}
        self._events: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._online = False
        self._offline_reported = False
        self._baselined = False

    # -- lifecycle -------------------------------------------------------- #

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="freebuff-events", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """Most recent emitted events (activity + watch status)."""
        with self._lock:
            return list(reversed(self._events))[: max(1, int(limit))]

    @property
    def online(self) -> bool:
        return self._online

    # -- internals -------------------------------------------------------- #

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                self._consume_stream()
                # A clean EOF (app closing the stream) is not an error —
                # reconnect after a short pause, with backoff reset (the app
                # was up long enough to stream, so we're healthy again).
                attempt = 0
            except (FreebuffWatchError, OSError) as exc:  # unreachable / mid-read drop
                self._mark_offline(str(exc))
                attempt = min(attempt + 1, len(_BACKOFF) - 1)
            if self._stop.wait(timeout=_BACKOFF[attempt]):
                return

    def _mark_online(self) -> None:
        if self._online:
            return
        self._online = True
        self._offline_reported = False
        self._emit({"type": "freebuff_watch", "state": "online"})

    def _mark_offline(self, detail: str) -> None:
        """Honestly report going offline — including a first failure that
        never connected (the HUD must know the watch can't start), but never
        spamming repeated offline events while already offline."""
        if self._offline_reported:
            return
        self._online = False
        self._offline_reported = True
        self._emit(
            {"type": "freebuff_watch", "state": "offline", "detail": detail}
        )

    def _consume_stream(self) -> None:
        """Open the SSE stream and process data lines until it closes."""
        import urllib.error
        import urllib.request

        url = self._base + self._path
        req = urllib.request.Request(url, headers={"User-Agent": "dourmouse/0.1"})
        try:
            resp = urllib.request.urlopen(req, timeout=30)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise FreebuffWatchError(f"Freebuff events stream unreachable: {exc}") from exc

        # First successful connect (or reconnect) -> tell the HUD we're live.
        self._mark_online()
        buf = ""
        try:
            while not self._stop.is_set():
                chunk = resp.read(4096)
                if not chunk:
                    break  # clean EOF -> reconnect
                buf += chunk.decode("utf-8", errors="replace")
                while "\n\n" in buf:
                    block, buf = buf.split("\n\n", 1)
                    for line in block.splitlines():
                        if line.startswith("data:"):
                            payload = line[len("data:"):].strip()
                            if payload:
                                self._handle_payload(payload)
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001, S110 - closing is best-effort
                pass

    def _handle_payload(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return  # malformed line — skip, never crash the watcher
        if not isinstance(payload, dict):
            return
        # The app sends full "state" snapshots and single-thread "thread"
        # events; both carry the fields we diff on. Process whatever came.
        if payload.get("type") == "thread":
            thread = payload.get("thread")
            if isinstance(thread, dict) and thread.get("id"):
                # Robust baseline: if the very first payload is a thread
                # event (no state snapshot yet), record it silently too so a
                # pre-existing thread is never mislabeled "created".
                if not self._baselined:
                    self._baselined = True
                    self._baseline_thread(thread)
                else:
                    self._diff_thread(thread)
        else:
            snapshot = payload.get("snapshot") if payload.get("type") == "state" else None
            threads = (snapshot or payload).get("threads") if isinstance(snapshot or payload, dict) else None
            if isinstance(threads, list):
                # The FIRST state snapshot is the baseline: record every
                # pre-existing thread silently so connect-time state never
                # floods the feed with "created" for threads that were there
                # all along — only NEW activity after this point surfaces.
                if not self._baselined:
                    self._baselined = True
                    for t in threads:
                        if isinstance(t, dict) and t.get("id"):
                            self._baseline_thread(t)
                    return
                for t in threads:
                    if isinstance(t, dict) and t.get("id"):
                        self._diff_thread(t)

    def _baseline_thread(self, thread: dict[str, Any]) -> None:
        """Record a thread's current state on first connect, emit nothing."""
        with self._lock:
            self._known[str(thread["id"])] = _thread_key(thread)

    def _diff_thread(self, thread: dict[str, Any]) -> None:
        tid = str(thread["id"])
        key = _thread_key(thread)
        with self._lock:
            prev = self._known.get(tid)
            self._known[tid] = key
        status, turn, _updated = key
        prev_status, prev_turn, _ = prev if prev else (None, None, None)
        title = _collapse_title(thread.get("title"))
        project = str(thread.get("projectPath") or thread.get("projectId") or "")
        project_name = project.rstrip("/").rsplit("/", 1)[-1] if project else ""

        # Brand-new thread.
        if prev is None:
            self._emit_activity(
                tid, title, project_name, "thread_created", status=status, turn=turn
            )
            return
        # Turn started / finished (the valuable transitions).
        if turn == _STATE_RUNNING and prev_turn != _STATE_RUNNING:
            self._emit_activity(
                tid, title, project_name, "turn_started", status=status, turn=turn
            )
            return
        if turn == _STATE_IDLE and prev_turn == _STATE_RUNNING:
            self._emit_activity(
                tid, title, project_name, "turn_finished", status=status, turn=turn
            )
            return
        # Status flip (open <-> closed) without a turn change.
        if prev_status and status != prev_status:
            self._emit_activity(
                tid, title, project_name, "status_changed", status=status, turn=turn
            )

    def _emit_activity(
        self,
        thread_id: str,
        title: str,
        project: str,
        kind: str,
        *,
        status: str,
        turn: str,
    ) -> None:
        now = time.time()
        # Rate-limit per (thread, kind): a thread flapping the SAME transition
        # (idle->running->idle->running) can't flood the feed, but a real
        # sequence (created -> started -> finished) always gets through.
        key = (thread_id, kind)
        with self._lock:
            last = self._last_emit.get(key, 0.0)
            if now - last < _MIN_EVENT_GAP:
                return  # rate-limited — a flapping thread never floods
            self._last_emit[key] = now
        activity = {
            "thread_id": thread_id,
            "title": title,
            "project": project,
            "kind": kind,
            "status": status,
            "turnState": turn,
            "ts": int(now),
        }
        self._emit({"type": "freebuff_activity", "activity": activity})

    def _emit(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._events.append(payload)
            if len(self._events) > _MAX_EVENTS:
                del self._events[: len(self._events) - _MAX_EVENTS]
        try:
            self._sink(payload)
        except Exception:  # noqa: BLE001, S110 - a broken sink never kills the watcher
            pass
