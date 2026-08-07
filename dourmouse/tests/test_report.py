"""v4.0 automation-engine tests (report.py) — morning report + scheduler.

Exercises build_morning_report with an injected fetcher (no network), honest
section failures, ATLAS section wiring, the schedule math (_seconds_until),
the DailyReporter lifecycle with an injected clock (hermetic — fires
immediately instead of waiting for 08:30), and the run_server wiring seam.
All deterministic (Rule 2.8), all hermetic (Rule 2.1).
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any

import pytest

from dourmouse import report as report_module
from dourmouse.report import (
    DailyReporter,
    _report_enabled,
    _report_time,
    _seconds_until,
    build_morning_report,
)
from dourmouse.message_bus import MessageBus
from dourmouse.general_roster import build_general_registry


class _FakeTracker:
    def __init__(self):
        self.events: list[dict[str, Any]] = []

    def on_event(self, entry: dict[str, Any]) -> None:
        self.events.append(entry)


def _fake_fetcher(tool: str, args: dict[str, Any]) -> str:
    if tool == "market_movers":
        return "MARKET MOVERS (fake): AAPL +2.1%\nMSFT +1.4%"
    if tool == "news_headlines":
        return "NEWS (fake): Fed holds rates."
    if tool == "list_tasks":
        return "TASKS: none (honest)."
    return f"{tool}: {args}"


class TestReportContent:
    def test_assembles_all_sections(self):
        registry = build_general_registry()
        report = build_morning_report(registry, fetcher=_fake_fetcher)
        assert "DAILY BRIEFING" in report
        assert "MARKET MOVERS — GAINERS" in report
        assert "MARKET MOVERS — LOSERS" in report
        assert "LIVE NEWS HEADLINES" in report
        assert "TASKS" in report
        assert "ATLAS QUANT REPO" in report
        assert "SYSTEM HEALTH" in report
        # fake content flowed through
        assert "AAPL" in report
        assert "Fed holds rates" in report

    def test_failing_section_is_honest(self):
        registry = build_general_registry()

        def _boom(tool, args):  # noqa: ARG001
            raise RuntimeError("feed down")

        report = build_morning_report(registry, fetcher=_boom)
        assert "REPORT SECTION FAILED" in report
        assert "feed down" in report
        # the report still assembles — one dead feed never kills it
        assert "SYSTEM HEALTH" in report

    def test_no_fetcher_uses_real_handlers(self):
        registry = build_general_registry()
        report = build_morning_report(registry, fetcher=None)
        # real handlers run; ATLAS section honest-errors if repo unset or
        # reports real data if configured — either way the section exists.
        assert "ATLAS QUANT REPO" in report
        assert "SYSTEM HEALTH" in report

    def test_atlas_section_uses_registered_handlers(self, monkeypatch):
        registry = build_general_registry()

        calls: list[str] = []

        class _FakeSpec:
            def __init__(self, name, text):
                self.name = name
                self._text = text

            def handler(self, args):  # noqa: ARG001
                calls.append(self.name)
                return self._text

        monkeypatch.setattr(
            registry,
            "lookup",
            lambda name: _FakeSpec(name, f"ATLAS {name} (fake)") if name.startswith("atlas_") else None,
        )
        report = build_morning_report(registry, fetcher=_fake_fetcher)
        assert "ATLAS atlas_status (fake)" in report
        assert "atlas_status" in calls and "atlas_bootstrap" in calls


class TestSchedule:
    def test_seconds_until_later_today(self):
        now = datetime(2026, 8, 6, 7, 0, 0)
        wait = _seconds_until("08:30", now)
        assert wait == 90 * 60

    def test_seconds_until_rolls_to_tomorrow(self):
        now = datetime(2026, 8, 6, 9, 0, 0)
        wait = _seconds_until("08:30", now)
        assert wait == 23 * 3600 + 30 * 60

    def test_exact_time_is_today(self):
        now = datetime(2026, 8, 6, 8, 30, 0)
        assert _seconds_until("08:30", now) == 24 * 3600

    def test_bad_time_raises(self, monkeypatch):
        monkeypatch.setenv("DOURMOUSE_REPORT_TIME", "banana")
        with pytest.raises(ValueError):
            _report_time()

    def test_enabled_matrix(self, monkeypatch):
        for val, expect in [("1", True), ("0", False), ("off", False), ("", False)]:
            monkeypatch.setenv("DOURMOUSE_REPORT", val)
            assert _report_enabled() is expect, val


class TestDailyReporter:
    def test_disabled_start_is_noop(self):
        rep = DailyReporter(None, None, None, enabled=False)
        rep.start()
        assert rep.running is False

    def test_fires_when_due(self):
        registry = build_general_registry()
        tracker = _FakeTracker()
        bus = MessageBus()
        now = datetime(2026, 8, 6, 8, 29, 59)  # just BEFORE the default 08:30
        rep = DailyReporter(
            registry,
            tracker,
            bus,
            fetcher=_fake_fetcher,
            clock=lambda: now,
            enabled=True,
        )
        rep.start()
        try:
            # The loop fires within a few seconds (wait=1s poll, then _fire).
            deadline = 10
            while not bus.snapshot() and deadline > 0:
                import time

                time.sleep(0.2)
                deadline -= 1
            msgs = bus.snapshot()
            assert msgs, "expected the daily report on the bus"
            msg = msgs[-1]
            assert msg["from"] == "dourmouse"
            assert msg["to"] == "*"
            assert msg["subject"] == "daily briefing"
            assert "DAILY BRIEFING" in msg["body"]
            assert tracker.events, "expected a tracker event too"
        finally:
            rep.stop()
            assert rep.running is False

    def test_reporter_wired_via_run_server(self, monkeypatch):
        """run_server(reporting=True) starts the scheduler; False does not."""
        from dourmouse.webui import run_server

        monkeypatch.setenv("DOURMOUSE_REPORT", "1")
        registry = build_general_registry()

        server = run_server(registry, port=0, reporting=False)
        try:
            assert server.daily_reporter is None
        finally:
            server.server_close()

        server2 = run_server(registry, port=0, reporting=True)
        try:
            assert server2.daily_reporter is not None
            assert server2.daily_reporter.running is True
        finally:
            server2.daily_reporter.stop()
            server2.server_close()
