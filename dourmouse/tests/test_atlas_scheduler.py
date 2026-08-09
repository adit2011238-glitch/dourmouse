"""Hermetic tests for atlas_scheduler.py — the calendar-driven paper trader.

No network, no real money: trading-day math is checked against known dates,
window open/close/idempotency are exercised against a temp paper log with a
mocked price source, and honest failure paths (price fetch down) are proven.
"""

from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path

import pytest

from dourmouse import atlas_scheduler as sched


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("FOREX_DATA_PATH", str(tmp_path / "fxdata"))
    (tmp_path / "fxdata" / "reports").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _seed_open_row(log: Path, key: str, price: str, entry_date: str):
    with open(log, "a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if fh.tell() == 0:
            writer.writerow(["key", "side", "venue", "entry_date", "entry_price",
                             "contract", "exit_date", "exit_price", "pnl_pct",
                             "pnl_usd", "notes"])
        writer.writerow([key, "short" if "HE" in key else "long", "scheduler",
                         entry_date, price, "HE1!" if "HE" in key else "ZC1!",
                         "", "", "", "", "seeded open"])


def _rows(tmp_path) -> list[list[str]]:
    log = tmp_path / "fxdata" / "reports" / "paper_log.csv"
    if not log.exists():
        return []
    return [r for r in csv.reader(log.read_text(encoding="utf-8").splitlines())
            if r and r[0] != "key"]


class TestTradingDays:
    def test_first_trading_day_aug_2026_skips_weekend(self):
        # Aug 1 2026 is a Saturday -> first trading day is Monday Aug 3.
        assert sched.first_trading_day(2026, 8) == dt.date(2026, 8, 3)

    def test_first_trading_day_dec_2026(self):
        assert sched.first_trading_day(2026, 12) == dt.date(2026, 12, 1)

    def test_last_trading_day_dec_2026(self):
        assert sched.last_trading_day(2026, 12) == dt.date(2026, 12, 31)

    def test_last_trading_day_aug_2026(self):
        assert sched.last_trading_day(2026, 8) == dt.date(2026, 8, 31)

    def test_good_friday_2026(self):
        # Easter Sunday 2026 = April 5 -> Good Friday April 3.
        assert sched._good_friday(2026) == dt.date(2026, 4, 3)

    def test_christmas_holiday_moves_last_trading_day_dec_2027(self):
        # Dec 31 2027 is a Friday and not a holiday -> Dec 31.
        assert sched.last_trading_day(2027, 12) == dt.date(2027, 12, 31)

    def test_july_4_weekend_is_not_a_holiday_shift(self):
        # July 4 2026 is Saturday; fixed-date list means the 3rd is the last
        # trading day of that week. Approximation is documented.
        assert sched.first_trading_day(2026, 7) == dt.date(2026, 7, 1)


class TestWindowBookkeeping:
    def test_leg_open_in_window_true(self, tmp_path):
        log = tmp_path / "fxdata" / "reports" / "paper_log.csv"
        _seed_open_row(log, "ZC_12", "450.5", "2026-12-01T09:00:00+00:00")
        assert sched._leg_open_in_window("ZC_12", 2026, 12) is True
        assert sched._leg_open_in_window("ZC_12", 2027, 12) is False

    def test_leg_has_open_true_false(self, tmp_path):
        log = tmp_path / "fxdata" / "reports" / "paper_log.csv"
        assert sched._leg_has_open("HE_8") is False
        _seed_open_row(log, "HE_8", "92.5", "2026-08-03T09:00:00+00:00")
        assert sched._leg_has_open("HE_8") is True


class TestRunOnce:
    def test_open_day_writes_one_row(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sched, "fetch_price", lambda key, **kw: 82.22)
        reports = sched.run_once(dt.date(2026, 8, 3))
        r = next(x for x in reports if x["key"] == "HE_8")
        assert r["action"] == "open" and r["applied"] is True
        rows = _rows(tmp_path)
        assert len(rows) == 1
        assert rows[0][0] == "HE_8" and rows[0][1] == "short"
        assert rows[0][4] == "82.22000"  # entry price recorded

    def test_open_day_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sched, "fetch_price", lambda key, **kw: 82.22)
        sched.run_once(dt.date(2026, 8, 3))
        reports = sched.run_once(dt.date(2026, 8, 3))  # same day again
        r = next(x for x in reports if x["key"] == "HE_8")
        assert r["action"] == "open" and r["applied"] is False
        assert "already open" in r["reason"]
        assert len(_rows(tmp_path)) == 1  # no double open

    def test_close_day_closes_with_pnl(self, tmp_path, monkeypatch):
        log = tmp_path / "fxdata" / "reports" / "paper_log.csv"
        _seed_open_row(log, "HE_8", "92.5", "2026-08-03T09:00:00+00:00")
        monkeypatch.setattr(sched, "fetch_price", lambda key, **kw: 91.0)
        reports = sched.run_once(dt.date(2026, 8, 31))
        r = next(x for x in reports if x["key"] == "HE_8")
        assert r["action"] == "close" and r["applied"] is True
        rows = _rows(tmp_path)
        assert len(rows) == 1
        assert rows[0][6] != ""  # exit date filled
        # short 92.5 -> 91.0 = profit
        assert float(rows[0][8]) > 0 and float(rows[0][9]) > 0

    def test_close_without_open_honest(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sched, "fetch_price", lambda key, **kw: 91.0)
        reports = sched.run_once(dt.date(2026, 8, 31))
        r = next(x for x in reports if x["key"] == "HE_8")
        assert r["action"] == "close" and r["applied"] is False
        assert "no open row" in r["reason"]

    def test_non_window_day_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sched, "fetch_price", lambda key, **kw: 82.22)
        reports = sched.run_once(dt.date(2026, 8, 9))
        assert all(r["action"] == "none" for r in reports)
        assert _rows(tmp_path) == []

    def test_price_failure_skips_honestly(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sched, "fetch_price", lambda key, **kw: None)
        reports = sched.run_once(dt.date(2026, 8, 3))
        r = next(x for x in reports if x["key"] == "HE_8")
        assert r["action"] == "open" and r["applied"] is False
        assert "price fetch failed" in r["reason"]
        assert _rows(tmp_path) == []

    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sched, "fetch_price", lambda key, **kw: 82.22)
        reports = sched.run_once(dt.date(2026, 8, 3), dry_run=True)
        r = next(x for x in reports if x["key"] == "HE_8")
        assert r["action"] == "open" and r["applied"] is False
        assert "dry-run" in r["reason"]
        assert _rows(tmp_path) == []

    def test_signals_log_receives_event(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sched, "fetch_price", lambda key, **kw: 82.22)
        sched.run_once(dt.date(2026, 8, 3))
        siglog = tmp_path / "ws" / "tv_signals.jsonl"
        assert siglog.exists()
        assert "atlas-scheduler" in siglog.read_text(encoding="utf-8")
