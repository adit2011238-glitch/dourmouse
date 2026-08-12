"""Always-on live agent loops (v2.8) — the roster actually RUNS.

The preloaded Live-domain agents (news, markets, rnd, mail, tasks) get a
daemon background loop each: on a fixed interval (and IMMEDIATELY at start)
the loop polls the agent's REAL feed through the SAME registered tool
handlers the orchestrator uses, then emits a ``live`` event into the
ActivityTracker. That makes every agent's own live window (and the map,
and the dashboard cluster) show real, current activity the moment DOURMOUSE
starts — no chat prompt required.

v3.0: the agents COMMUNICATE. Each poll result is also broadcast onto the
inter-agent message bus (``<agent> -> *``) — the same real handler text the
window shows — so every other agent can read what news/markets/rnd/mail/
tasks just learned. This is the deterministic data plane (Rule 2.8): no LLM
between poll and bus, real data only (Rule 2.1), and failures are broadcast
honestly too (Rule 2.2).

Design rules, mapped to the project conventions:

- Rule 2.1 (anti-fabrication): results come from the real registry tool
  handlers (``registry.lookup(tool).handler(args)``) — the exact data path
  a dispatched task would use, so a live poll and a chat tool-call agree.
- Rule 2.2 (no silent stubs): a poll that raises (network down, unconfigured
  IMAP, unknown symbol) emits an honest ``LIVE POLL FAILED ...`` line into
  the feed — the window shows the failure, never a fabricated result.
- Rule 2.8 (determinism): plain polling loops, no LLM anywhere in this
  path. The schedule is a fixed table; nothing here is a model judgment.
- Hermetic tests: ``fetcher`` is injectable; tests pass a fake callable so
  the suite never touches the network. With no fetcher and no registered
  handler for a scheduled tool, that poll is skipped (never silently stubbed).

Env control: ``DOURMOUSE_LIVE=0`` disables all live polling (default on).
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Callable

# agent -> [(tool_name, arguments, interval_seconds)]
# Every tool below exists in the v2.3 Live roster (general_roster.py); tools
# for agents absent from the registry are skipped automatically.
DEFAULT_SCHEDULE: dict[str, list[tuple[str, dict[str, Any], int]]] = {
    "news": [("news_headlines", {"max_results": 5}, 120)],
    "markets": [
        ("market_movers", {"direction": "gainers", "count": 5}, 120),
        ("market_movers", {"direction": "losers", "count": 5}, 120),
    ],
    "rnd": [
        ("research_news", {"max_results": 5}, 180),
        ("research_movers", {"direction": "gainers", "count": 5}, 180),
    ],
    "mail": [("read_inbox", {"max_items": 5}, 300)],
    "tasks": [("list_tasks", {}, 60)],
}


def live_enabled(value: str | None = None) -> bool:
    """Whether always-on polling is enabled (DOURMOUSE_LIVE, default on)."""
    raw = value if value is not None else os.environ.get("DOURMOUSE_LIVE", "1")
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


class LiveRuntime:
    """Background poll loops for every Live-domain agent in a registry.

    ``fetcher`` (optional, test seam): ``callable(tool_name, args) -> str``.
    When None, the runtime calls the REAL registered tool handler, so
    production polling reuses the exact data path of dispatched tasks.
    """

    def __init__(
        self,
        registry: Any,
        tracker: Any,
        *,
        fetcher: Callable[[str, dict[str, Any]], str] | None = None,
        schedule: dict[str, list[tuple[str, dict[str, Any], int]]] | None = None,
        bus: Any | None = None,
    ) -> None:
        self._registry = registry
        self._tracker = tracker
        self._fetcher = fetcher
        self._schedule = dict(schedule or DEFAULT_SCHEDULE)
        self._bus = bus  # v3.0: optional inter-agent bus; None = no posting
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._polls = self._build_polls()

    # ------------------------------------------------------------------ #
    # Poll table
    # ------------------------------------------------------------------ #

    def _build_polls(self) -> list[tuple[str, str, dict[str, Any], int]]:
        """Only agents present in the registry get loops (no ghosts).

        A scheduled tool with no registered handler AND no injected fetcher
        is skipped — never polled into a silent stub.
        """
        polls: list[tuple[str, str, dict[str, Any], int]] = []
        for agent, entries in self._schedule.items():
            if agent not in self._registry.subagent_names:
                continue
            for tool, args, interval in entries:
                if self._registry.lookup(tool) is None and self._fetcher is None:
                    continue
                polls.append((agent, tool, args, int(interval)))
        return polls

    @property
    def poll_count(self) -> int:
        return len(self._polls)

    @property
    def running(self) -> bool:
        return any(t.is_alive() for t in self._threads)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        if self._threads:
            return
        for agent, tool, args, interval in self._polls:
            thread = threading.Thread(
                target=self._loop,
                args=(tool, args, interval),
                daemon=True,
                name=f"live-{agent}-{tool}",
            )
            self._threads.append(thread)
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2)
        self._threads = []

    # ------------------------------------------------------------------ #
    # Loop
    # ------------------------------------------------------------------ #

    def _loop(self, tool: str, args: dict[str, Any], interval: int) -> None:
        # Poll IMMEDIATELY at start so windows show activity right away
        # ("all agents live and immediately working"), then on the interval.
        self._poll_once(tool, args)
        while not self._stop.wait(interval):
            self._poll_once(tool, args)

    def _poll_once(self, tool: str, args: dict[str, Any]) -> None:
        try:
            if self._fetcher is not None:
                text = str(self._fetcher(tool, args))
            else:
                spec = self._registry.lookup(tool)
                text = (
                    spec.handler(args)
                    if spec is not None
                    else f"ERROR: no such tool: {tool}"
                )
        except Exception as exc:  # honest failure — never a fabricated poll
            text = f"LIVE POLL FAILED (reported honestly): {exc}"
        self._tracker.on_event(
            {
                "type": "live",
                "name": tool,
                "raw_arguments": json.dumps(args),
                "text": text[:600],
            }
        )
        # v3.0: broadcast the SAME real result (or honest failure) onto the
        # inter-agent bus so every other agent can read what this one learned.
        if self._bus is not None:
            try:
                agent = self._agent_for_tool(tool)
                if agent is not None:
                    self._bus.post(
                        from_agent=agent,
                        to_agent="*",
                        subject=f"live:{tool}",
                        body=text[:600],
                    )
            except Exception:
                pass  # a broken bus must never kill the poll loop

    def _agent_for_tool(self, tool: str) -> str | None:
        """Which registered subagent owns ``tool`` (for the bus broadcast)."""
        for agent in self._registry.all_subagents():
            for spec in agent.tools:
                if spec.name == tool:
                    return agent.name
        return None
