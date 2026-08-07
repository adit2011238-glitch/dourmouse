"""Automation engine (v4.0, spec Phase 11) — Dourmouse is proactive.

Two pieces, both deterministic (Rule 2.8 — no LLM anywhere in this path):

- ``build_morning_report(registry, fetcher=None)`` — assembles the daily
  briefing from the SAME registered tool handlers the roster uses (news,
  markets, tasks, ATLAS telemetry, system health). Every section fails
  honestly (Rule 2.2) — a dead feed never fabricates a line.
- ``DailyReporter`` — a daemon thread that fires at ``DOURMOUSE_REPORT_TIME``
  (default 08:30) each day, posts the report to the inter-agent message bus
  (``dourmouse -> *``) and into the ActivityTracker so it shows up on the
  dashboard COMMS feed, then sleeps until tomorrow. Injectable clock +
  fetcher keep the tests hermetic.

``DOURMOUSE_REPORT=0`` disables the scheduler (default on).
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable

# Sections, in display order. Each is (label, tool_name, args, max_len).
_SECTIONS: list[tuple[str, str, dict[str, Any], int]] = [
    ("MARKET MOVERS — GAINERS", "market_movers", {"direction": "gainers", "count": 5}, 600),
    ("MARKET MOVERS — LOSERS", "market_movers", {"direction": "losers", "count": 5}, 600),
    ("LIVE NEWS HEADLINES", "news_headlines", {"max_results": 5}, 800),
    ("TASKS", "list_tasks", {"include_done": False}, 600),
]

# System-health section is built locally (no roster tool dependency).
def _system_health_block() -> str:
    try:
        import platform

        import psutil  # type: ignore[import-not-found]  # optional; honest if absent
        load = os.getloadavg()
        cpu = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory()
        return (
            "SYSTEM HEALTH:\n"
            f"  host: {platform.node()}\n"
            f"  cpu: {cpu:.0f}%\n"
            f"  mem: {mem.percent:.0f}% used\n"
            f"  load: {load[0]:.2f} / {load[1]:.2f} / {load[2]:.2f}\n"
            f"  at: {datetime.now().isoformat(timespec='seconds')}"
        )
    except Exception as exc:  # honest degradation (Rule 2.2)
        return f"SYSTEM HEALTH (reported honestly): unavailable — {exc}"


def _report_enabled(value: str | None = None) -> bool:
    raw = value if value is not None else os.environ.get("DOURMOUSE_REPORT", "1")
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _report_time() -> str:
    """DOURMOUSE_REPORT_TIME (HH:MM, 24h), validated; default 08:30."""
    raw = os.environ.get("DOURMOUSE_REPORT_TIME", "08:30").strip()
    try:
        datetime.strptime(raw, "%H:%M")
    except ValueError:
        raise ValueError(
            f"DOURMOUSE_REPORT_TIME must be HH:MM (24h), got {raw!r}"
        ) from None
    return raw


def build_morning_report(
    registry: Any,
    fetcher: Callable[[str, dict[str, Any]], str] | None = None,
) -> str:
    """Deterministic daily briefing from the roster's real tool handlers.

    ``fetcher`` is the test seam (same shape as LiveRuntime's): when None,
    the REAL registered handler runs. Sections never crash the report — a
    failing feed contributes an honest failure line.
    """
    lines: list[str] = [
        "╔══════════════════════════════════════════════════╗",
        "║         DOURMOUSE // DAILY BRIEFING           ║",
        "╚══════════════════════════════════════════════════╝",
        f"generated: {datetime.now().isoformat(timespec='seconds')}",
        "",
    ]
    for label, tool, args, cap in _SECTIONS:
        lines.append(f"── {label} ──")
        try:
            if fetcher is not None:
                text = str(fetcher(tool, args))
            else:
                spec = registry.lookup(tool)
                text = (
                    spec.handler(args)
                    if spec is not None
                    else f"ERROR: no such tool: {tool}"
                )
        except Exception as exc:
            text = f"REPORT SECTION FAILED (reported honestly): {exc}"
        lines.append(text[:cap])
        lines.append("")

    # ATLAS telemetry — real, from the registered atlas agent's tools.
    lines.append("── ATLAS QUANT REPO ──")
    try:
        atlas_spec = registry.lookup("atlas_status")
        lines.append(
            atlas_spec.handler({}) if atlas_spec is not None else "(atlas agent missing)"
        )
        bootstrap_spec = registry.lookup("atlas_bootstrap")
        if bootstrap_spec is not None:
            lines.append("")
            lines.append(bootstrap_spec.handler({}))
    except Exception as exc:
        lines.append(f"ATLAS SECTION FAILED (reported honestly): {exc}")
    lines.append("")
    lines.append(_system_health_block())
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# DailyReporter — the proactive scheduler thread
# --------------------------------------------------------------------------- #

def _seconds_until(target_hhmm: str, now: datetime) -> float:
    """Seconds from ``now`` until the next occurrence of HH:MM (today or tomorrow)."""
    hh, mm = (int(x) for x in target_hhmm.split(":"))
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


class DailyReporter:
    """Daemon thread posting the daily briefing to the bus + tracker.

    ``clock`` (test seam) returns the current datetime; the loop computes the
    next fire time from it. ``fetcher`` (test seam) overrides the roster
    handlers. ``enabled=False`` (or DOURMOUSE_REPORT=0) makes start() a no-op.
    """

    def __init__(
        self,
        registry: Any,
        tracker: Any,
        bus: Any,
        *,
        fetcher: Callable[[str, dict[str, Any]], str] | None = None,
        clock: Callable[[], datetime] = datetime.now,
        enabled: bool | None = None,
    ) -> None:
        self._registry = registry
        self._tracker = tracker
        self._bus = bus
        self._fetcher = fetcher
        self._clock = clock
        self._enabled = _report_enabled() if enabled is None else bool(enabled)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self._thread is not None or not self._enabled:
            return
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="dourmouse-daily-report",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    # -- loop ---------------------------------------------------------- #

    def _loop(self) -> None:
        target = _report_time()
        while not self._stop.wait(1):
            # Re-read the schedule each minute (cheap) so a live .env change
            # takes effect without a restart; compute time-to-fire fresh.
            try:
                target = _report_time()
            except ValueError:
                self._stop.wait(60)  # bad config: back off, keep trying
                continue
            wait = _seconds_until(target, self._clock())
            if wait > 1:
                self._stop.wait(min(wait, 30))
                continue
            self._fire()
            # jump to tomorrow so we don't re-fire within the same minute
            self._stop.wait(60)

    def _fire(self) -> None:
        report = build_morning_report(self._registry, fetcher=self._fetcher)
        # 1) into the tracker so the dashboard feed shows it
        try:
            self._tracker.on_event(
                {
                    "type": "live",
                    "name": "atlas_report",
                    "raw_arguments": "{}",
                    "text": report[:600],
                }
            )
        except Exception:
            pass
        # 2) onto the inter-agent bus (dourmouse -> *), capped by the bus itself
        try:
            if self._bus is not None:
                self._bus.post(
                    from_agent="dourmouse",
                    to_agent="*",
                    subject="daily briefing",
                    body=report,
                )
        except Exception:
            pass
