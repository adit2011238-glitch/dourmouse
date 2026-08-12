"""Inter-agent message bus (v3.0) — the agents talk to each other.

The General roster is driven by ONE LLM brain (the orchestrator), so real
agent-to-agent communication happens on two planes, both supported here:

- DATA plane (deterministic, Rule 2.8): the always-on live agents broadcast
  their REAL poll results to the bus (``news -> *``), and any agent can read
  its inbox. No LLM in this path — the data is whatever the real feed
  handlers returned.
- TOOL plane (LLM-mediated): the ``messenger`` subagent's ``send_message`` /
  ``read_inbox`` tools let the orchestrator explicitly route knowledge
  between agents mid-task (e.g. research -> markets).

The bus itself is deterministic and thread-safe (stdlib only): monotonic ids,
timestamps, a bounded ring (oldest evicted), per-message read flags, and
broadcast support (``to == "*"``). Messages are real objects with real
bodies — never fabricated (Rule 2.1), never silently dropped.

A process-wide singleton (``get_message_bus``) lets the tools, the live
runtime, and the web UI share ONE channel, exactly like the workspace root.
Tests can isolate with ``set_message_bus(MessageBus())``.
"""

from __future__ import annotations

import itertools
import threading
from datetime import datetime
from typing import Any, Callable

BROADCAST = "*"
_MAX_MESSAGES = 500


class MessageBus:
    """Thread-safe, bounded store of inter-agent messages."""

    def __init__(self, max_messages: int = _MAX_MESSAGES) -> None:
        self._max = max(1, int(max_messages))
        self._messages: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._observers: list[Callable[[dict[str, Any]], None]] = []

    # -- observers -------------------------------------------------------- #

    def on_post(self, fn: Callable[[dict[str, Any]], None]) -> None:
        """Register an observer fired after every post (e.g. the memory
        mirror in webui). A raising observer is swallowed — the bus must
        never break dispatch (same principle as the event_sink)."""
        with self._lock:
            self._observers.append(fn)

    def _notify(self, message: dict[str, Any]) -> None:
        for fn in list(self._observers):
            try:
                fn(message)
            except Exception:
                pass

    # -- write ------------------------------------------------------------ #

    def post(
        self,
        from_agent: str,
        to_agent: str,
        subject: str,
        body: str,
    ) -> dict[str, Any]:
        """Post one message. ``to_agent`` may be a subagent name or BROADCAST.

        Returns the stored message dict. The body is capped at 1200 chars so
        a live feed dump can never blow up the ring.
        """
        from_agent = (from_agent or "?").strip()[:80] or "?"
        to_agent = (to_agent or BROADCAST).strip()[:80] or BROADCAST
        subject = (subject or "").strip()[:200]
        body = (body or "").strip()[:1200]
        message = {
            "id": f"msg-{next(self._ids)}",
            "from": from_agent,
            "to": to_agent,
            "subject": subject,
            "body": body,
            "at": datetime.now().isoformat(timespec="seconds"),
            # Per-recipient read state: a broadcast stays UNREAD for every
            # agent until THAT agent reads it. One agent opening its inbox
            # must never clear another agent's badge (reviewer-caught).
            "read_by": set(),
        }
        with self._lock:
            self._messages.append(message)
            if len(self._messages) > self._max:
                del self._messages[: len(self._messages) - self._max]
        # Observers and callers both get the JSON-safe copy (read_by as a
        # list), never the internal dict whose read_by is a set — no code
        # path can json.dumps a set.
        public = self._public(message)
        self._notify(public)
        return public

    # -- read ------------------------------------------------------------- #

    def _public(self, msg: dict[str, Any], viewer: str | None = None) -> dict[str, Any]:
        """JSON-safe copy for API consumers.

        ``read`` is computed relative to the requesting agent when known
        (``viewer``) so inboxes reflect THAT agent's read state; without a
        viewer (snapshot/outbox) it means "read by anyone". The internal
        ``read_by`` set is serialized as a sorted list so json.dumps can
        never choke on it."""
        out = dict(msg)
        out["read_by"] = sorted(msg["read_by"])
        if viewer is not None:
            out["read"] = viewer in msg["read_by"]
        else:
            out["read"] = bool(msg["read_by"])
        return out

    def inbox(self, agent: str, limit: int = 20) -> list[dict[str, Any]]:
        """Messages addressed to ``agent`` (direct or broadcast), newest
        first, with ``read`` computed for THAT agent. Each entry is a copy;
        callers may not mutate the bus."""
        limit = max(1, min(int(limit), 200))
        with self._lock:
            rows = [
                m for m in reversed(self._messages)
                if m["to"] in (agent, BROADCAST)
            ][:limit]
            return [self._public(m, agent) for m in rows]

    def outbox(self, agent: str, limit: int = 20) -> list[dict[str, Any]]:
        """Messages sent BY ``agent``, newest first."""
        limit = max(1, min(int(limit), 200))
        with self._lock:
            rows = [m for m in reversed(self._messages) if m["from"] == agent][:limit]
            return [self._public(m) for m in rows]

    def unread_count(self, agent: str) -> int:
        """Messages addressed to ``agent`` (direct or broadcast) that THAT
        agent has not read yet — per-recipient, never global."""
        with self._lock:
            return sum(
                1
                for m in self._messages
                if m["to"] in (agent, BROADCAST) and agent not in m["read_by"]
            )

    def mark_read(self, msg_id: str, agent: str) -> bool:
        """Mark one message as read BY ``agent``; returns whether it existed.
        Only that agent's read state changes — other recipients (including
        other readers of a broadcast) keep their own unread status."""
        with self._lock:
            for m in self._messages:
                if m["id"] == msg_id:
                    m["read_by"].add(agent)
                    return True
            return False

    def snapshot(self, limit: int = 50) -> list[dict[str, Any]]:
        """Recent bus traffic (all messages), newest first, JSON-safe."""
        limit = max(1, min(int(limit), 200))
        with self._lock:
            return [self._public(m) for m in reversed(self._messages)][:limit]

    def count(self) -> int:
        with self._lock:
            return len(self._messages)

    def clear(self) -> None:
        """Test helper: wipe all messages (observers kept)."""
        with self._lock:
            self._messages.clear()


# --------------------------------------------------------------------------- #
# Process-wide singleton — tools, live runtime, and web UI share one channel
# --------------------------------------------------------------------------- #

_lock = threading.Lock()
_DEFAULT_BUS: MessageBus | None = None


def get_message_bus() -> MessageBus:
    global _DEFAULT_BUS
    if _DEFAULT_BUS is None:
        with _lock:
            if _DEFAULT_BUS is None:
                _DEFAULT_BUS = MessageBus()
    return _DEFAULT_BUS


def set_message_bus(bus: MessageBus | None) -> None:
    """Replace the process singleton (test isolation). None resets it so the
    next get_message_bus() creates a fresh bus."""
    global _DEFAULT_BUS
    with _lock:
        _DEFAULT_BUS = bus
