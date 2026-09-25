"""The standing-agent runtime (INFRA-1, finding #114).

Most agents are on-demand: they act only when dispatched. A *standing*
agent runs without prompting, forever:

    1. drain its inbox: answer every message other agents sent it on the bus;
    2. do one bounded unit of its standing work (a ``tick``);
    3. sleep until its interval passes OR a message for it arrives.

Design rules (docs: REMAINING_WORK INFRA-1), each enforced here:

- **Opt-in per agent.** Only agents registered here get a loop.
- **The always-hot tier is deterministic.** ``tick`` and ``handle_message``
  are plain Python; an agent that wants a model escalates inside its own
  code (as the security analyst does) and only on a real trigger.
- **Inbox-woken, not only timed.** A bus post addressed to the agent wakes
  its loop at once.
- **The tightest leash.** A standing agent declares its capabilities, and
  anything beyond ``read`` and ``propose`` is refused at registration: a
  standing agent may suggest a change, and the owner's approval (a gated
  tool or a console click) carries it out. Every tick and every answered
  message is recorded in a visible activity log.
- **A broken agent never takes the runtime down.** Exceptions are recorded
  on the agent's own status and the loop continues.
"""

from __future__ import annotations

import contextlib
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Protocol

ALLOWED_CAPABILITIES = frozenset({"read", "propose"})


class StandingAgent(Protocol):
    name: str
    interval_s: float
    capabilities: frozenset[str]

    def tick(self) -> str:
        """One bounded unit of standing work; returns a one-line summary
        (empty when there was nothing to do)."""
        ...

    def handle_message(self, subject: str, body: str, sender: str) -> str | None:
        """Answer a bus message; None means "not for me, leave it"."""
        ...


@dataclass
class AgentState:
    name: str
    interval_s: float
    capabilities: list[str]
    ticks: int = 0
    answered: int = 0
    last_tick_at: float | None = None
    last_summary: str = ""
    last_error: str = ""
    errors: int = 0
    activity: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=200))


class StandingRuntime:
    def __init__(self, bus: Any, log: Any = None) -> None:
        self._bus = bus
        self._log = log  # optional OfficeLogger-like: .log_message(dict)
        self._agents: dict[str, StandingAgent] = {}
        self._state: dict[str, AgentState] = {}
        self._wake: dict[str, threading.Event] = {}
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        bus.on_post(self._on_post)

    def register(self, agent: StandingAgent) -> None:
        extra = set(agent.capabilities) - ALLOWED_CAPABILITIES
        if extra:
            raise ValueError(f"standing agent {agent.name} asks for {sorted(extra)}; standing agents may only "
                             "read and propose (every change goes through the owner's approval)")
        if agent.name in self._agents:
            raise ValueError(f"standing agent {agent.name} is already registered")
        self._agents[agent.name] = agent
        self._state[agent.name] = AgentState(agent.name, agent.interval_s, sorted(agent.capabilities))
        self._wake[agent.name] = threading.Event()

    def _on_post(self, msg: dict[str, Any]) -> None:
        ev = self._wake.get(msg.get("to", ""))
        if ev is not None and msg.get("from") != msg.get("to"):
            ev.set()

    def _record(self, st: AgentState, kind: str, text: str) -> None:
        at = time.time()
        entry: dict[str, Any] = {"at": at, "agent": st.name, "kind": kind, "text": text[:500]}
        st.activity.appendleft(entry)
        if self._log is not None:
            with contextlib.suppress(Exception):  # logging must never stop an agent
                self._log.log_message({"id": f"standing-{st.name}-{int(at * 1000)}", "from": st.name,
                                       "to": "standing-log", "subject": kind, "body": text[:1200],
                                       "at": time.strftime("%Y-%m-%dT%H:%M:%S")})

    def drain_inbox(self, name: str) -> int:
        agent, st = self._agents[name], self._state[name]
        answered = 0
        for msg in self._bus.inbox(name, limit=50):
            if msg.get("read") or msg.get("from") == name:
                continue
            if msg.get("to") != name:
                # A broadcast (the live feeds post every poll to "*") is not a
                # request: answering it flooded the bus and the notification
                # center with "Re: live:..." replies (found live, finding #121).
                self._bus.mark_read(msg["id"], name)
                continue
            try:
                reply = agent.handle_message(msg.get("subject", ""), msg.get("body", ""), msg.get("from", ""))
            except Exception as exc:  # noqa: BLE001 -- recorded, the loop goes on
                reply = f"ERROR: {type(exc).__name__}: {exc}"
                st.errors += 1
                st.last_error = reply
            self._bus.mark_read(msg["id"], name)
            if reply is None:
                continue
            self._bus.post(name, msg.get("from") or "*", "Re: " + (msg.get("subject") or ""), reply)
            answered += 1
            st.answered += 1
            self._record(st, "answered", f"{msg.get('from')}: {msg.get('subject')}")
        return answered

    def run_tick(self, name: str) -> str:
        agent, st = self._agents[name], self._state[name]
        try:
            summary = agent.tick() or ""
            st.last_error = ""
        except Exception as exc:  # noqa: BLE001 -- recorded, the loop goes on
            summary = ""
            st.errors += 1
            st.last_error = f"{type(exc).__name__}: {exc}"
            self._record(st, "error", st.last_error + "\n" + traceback.format_exc(limit=3))
        st.ticks += 1
        st.last_tick_at = time.time()
        if summary:
            st.last_summary = summary
            self._record(st, "tick", summary)
        return summary

    def _loop(self, name: str) -> None:
        st, wake = self._state[name], self._wake[name]
        next_tick = 0.0
        while not self._stop.is_set():
            self.drain_inbox(name)
            if time.monotonic() >= next_tick:
                self.run_tick(name)
                next_tick = time.monotonic() + st.interval_s
            wake.wait(max(0.05, next_tick - time.monotonic()))
            wake.clear()

    def start(self) -> None:
        for name in self._agents:
            t = threading.Thread(target=self._loop, args=(name,), daemon=True, name=f"standing-{name}")
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        for ev in self._wake.values():
            ev.set()

    def status(self) -> list[dict[str, Any]]:
        return [{
            "name": st.name, "interval_s": st.interval_s, "capabilities": st.capabilities, "ticks": st.ticks,
            "answered": st.answered, "last_tick_at": st.last_tick_at, "last_summary": st.last_summary,
            "last_error": st.last_error, "errors": st.errors, "activity": list(st.activity)[:30],
        } for st in self._state.values()]


def standing_agents_enabled() -> bool:
    import os

    return os.environ.get("DOURMOUSE_STANDING_AGENTS", "1").strip().lower() not in ("0", "false", "no", "off")
