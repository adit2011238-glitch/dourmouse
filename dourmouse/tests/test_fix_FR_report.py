"""FR fixes P5-40 and P5-41: the scheduled briefing must fire whatever the
sampling phase, and a bad DOURMOUSE_REPORT_TIME must not kill the thread."""

from __future__ import annotations

from datetime import datetime, timedelta

from dourmouse.report import DailyReporter


class _SimClock:
    """A clock and a stop event in one: wait(t) advances the simulated clock."""

    def __init__(self, start: datetime, hours: float) -> None:
        self.now = start
        self.end = start + timedelta(hours=hours)

    def __call__(self) -> datetime:
        return self.now

    def wait(self, timeout: float | None = None) -> bool:
        self.now += timedelta(seconds=timeout or 0)
        return self.now >= self.end

    def set(self) -> None:
        self.end = self.now


def _run(start: datetime, hours: float, monkeypatch, report_time: str = "08:30"):
    monkeypatch.setenv("DOURMOUSE_REPORT_TIME", report_time)
    sim = _SimClock(start, hours)
    rep = DailyReporter(None, None, None, clock=sim, enabled=True)
    rep._stop = sim  # type: ignore[assignment]
    fired: list[datetime] = []
    rep._fire = lambda: fired.append(sim.now)  # type: ignore[method-assign]
    rep._loop()
    return fired


def test_fires_once_per_day_for_every_sampling_phase(monkeypatch):
    # Old loop fired on about 1 start offset in 31. Every offset must fire.
    for offset in (0.0, 0.4, 7.3, 15.0, 22.9, 29.7):
        start = datetime(2026, 8, 6, 6, 0, 0) + timedelta(seconds=offset)
        fired = _run(start, 30, monkeypatch)  # 06:00 day 1 to 12:00 day 2
        assert len(fired) == 2, (offset, fired)
        assert all(abs((f - f.replace(hour=8, minute=30, second=0)).total_seconds()) < 5 for f in fired), fired


def test_a_bad_report_time_does_not_kill_the_thread(monkeypatch):
    monkeypatch.setenv("DOURMOUSE_REPORT_TIME", "8.30")
    sim = _SimClock(datetime(2026, 8, 6, 6, 0, 0), 0.05)
    rep = DailyReporter(None, None, None, clock=sim, enabled=True)
    rep._stop = sim  # type: ignore[assignment]
    rep._loop()  # returns normally instead of raising ValueError
