"""v6.0 forex-data pipeline tests (forex_ops.py + roster wiring).

Exercises the REAL telemetry functions against a fake pipeline tree in a
tmp dir: NOT CONFIGURED degradation, inventory from a fake manifest,
paper-log parsing, the calendar subprocess (real output from a fixture
script), the IBKR probe via a monkeypatched socket, and the roster
registration. All hermetic (no network, no real data, Rule 2.1).
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from dourmouse import forex_ops
from dourmouse.forex_ops import (
    ForexNotConfiguredError,
    forex_events,
    forex_ibkr,
    forex_inventory,
    forex_paper,
    forex_strategy,
    get_forex_data_path,
)
from dourmouse.general_roster import build_general_registry


def _fake_pipeline(tmp_path: Path) -> Path:
    """Build a minimal forex-data-shaped tree."""
    root = tmp_path / "forex-data"
    (root / "market-data" / "normalized").mkdir(parents=True)
    (root / "market-data" / "raw" / "yahoo").mkdir(parents=True)
    (root / "market-data" / "events").mkdir(parents=True)
    (root / "market-data" / "fundamentals").mkdir(parents=True)
    (root / "scripts").mkdir(parents=True)
    (root / "reports").mkdir(parents=True)

    (root / "market-data" / "normalized" / "manifest.csv").write_text(
        "pair,timeframe,file,start_utc,end_utc,bars,unique_close_ratio,gaps,source,quality,sha256\n"
        "EURUSD,D1,EURUSD_d1.parquet,2016-08-04,2026-08-05,2601,0.88,0,yahoo_csv,ok,abc\n"
        "GBPUSD,D1,GBPUSD_d1.parquet,2016-08-04,2026-08-05,2601,0.91,0,yahoo_csv,ok,def\n"
        "EURUSD,H1,EURUSD_h1.parquet,2023-10-18,2026-08-05,17256,0.08,7,yahoo_csv,ok,ghi\n",
        encoding="utf-8",
    )
    (root / "market-data" / "raw" / "yahoo" / "COMM_HE_d.csv").write_text(
        "date,open,high,low,close\n2020-01-02,1,1,1,1\n2020-01-03,1,1,1,1\n",
        encoding="utf-8",
    )
    (root / "market-data" / "raw" / "yahoo" / "COMM_ZC_d.csv").write_text(
        "date,open,high,low,close\n2020-01-02,1,1,1,1\n",
        encoding="utf-8",
    )
    (root / "market-data" / "fundamentals" / "cpi_usd.csv").write_text("x\n", encoding="utf-8")
    (root / "scripts" / "seasonal_calendar.py").write_text(
        "print('=== SEASONAL PAPER-TRADING CALENDAR ===')\nprint('HE_8  SHORT HE  month 8')\n"
        "print('2026: entry 2026-08-03 -> exit 2026-08-31  [NOW OPEN]')\n",
        encoding="utf-8",
    )
    (root / "reports" / "paper_log.csv").write_text(
        "key,entry_date,entry_price,size,exit_date,exit_price,pnl_pct,pnl_usd\n"
        "HE_8,2026-08-10,120.5,120,,,,\n"
        "ZC_12,2025-12-01,430.0,100,2025-12-31,445.0,3.49,3.49\n",
        encoding="utf-8",
    )
    (root / "reports" / "VALIDATION_REPORT.md").write_text(
        "# VALIDATION REPORT\n\n## 8. Verdict\n\nThe strategy passes the new "
        "validation system. Core legs: HE_8, HE_4, ZC_12.\n",
        encoding="utf-8",
    )
    return root


class TestConfig:
    def test_missing_env_raises_honestly(self, monkeypatch):
        monkeypatch.delenv("FOREX_DATA_PATH", raising=False)
        with pytest.raises(ForexNotConfiguredError):
            get_forex_data_path()

    def test_missing_dir_raises_honestly(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FOREX_DATA_PATH", str(tmp_path / "nope"))
        with pytest.raises(ForexNotConfiguredError):
            get_forex_data_path()

    def test_ok(self, monkeypatch, tmp_path):
        root = _fake_pipeline(tmp_path)
        monkeypatch.setenv("FOREX_DATA_PATH", str(root))
        assert get_forex_data_path() == root


class TestInventory:
    def test_reads_manifest_and_commodities(self, monkeypatch, tmp_path):
        root = _fake_pipeline(tmp_path)
        monkeypatch.setenv("FOREX_DATA_PATH", str(root))
        inv = forex_inventory()
        assert inv["configured"] is True
        assert inv["fx_pairs"] == 2            # D1 rows only
        assert inv["commodities"] == 2
        assert inv["total_bars"] == 2601 + 2601 + 17256
        assert inv["timeframe_counts"] == {"D1": 2, "H1": 1}
        assert any("EURUSD" in row for row in inv["d1_coverage"])
        assert inv["fundamentals_files"] == 1

    def test_missing_manifest_is_empty_not_crash(self, monkeypatch, tmp_path):
        root = _fake_pipeline(tmp_path)
        (root / "market-data" / "normalized" / "manifest.csv").unlink()
        monkeypatch.setenv("FOREX_DATA_PATH", str(root))
        inv = forex_inventory()
        assert inv["fx_pairs"] == 0
        assert inv["total_bars"] == 0


class TestStrategy:
    def test_verdict_and_calendar(self, monkeypatch, tmp_path):
        root = _fake_pipeline(tmp_path)
        monkeypatch.setenv("FOREX_DATA_PATH", str(root))
        s = forex_strategy()
        assert "passes the new validation system" in s["verdict"]
        assert "HE_8" in s["calendar"]
        assert "NOW OPEN" in s["calendar"]

    def test_calendar_failure_is_honest(self, monkeypatch, tmp_path):
        root = _fake_pipeline(tmp_path)
        (root / "scripts" / "seasonal_calendar.py").write_text(
            "raise SystemExit(3)\n", encoding="utf-8"
        )
        monkeypatch.setenv("FOREX_DATA_PATH", str(root))
        s = forex_strategy()
        assert "calendar" in s


class TestPaper:
    def test_open_and_closed(self, monkeypatch, tmp_path):
        root = _fake_pipeline(tmp_path)
        monkeypatch.setenv("FOREX_DATA_PATH", str(root))
        p = forex_paper()
        assert p["trades"] == 2
        assert len(p["open_positions"]) == 1
        assert p["open_positions"][0]["key"] == "HE_8"
        assert p["closed_trades"] == 1
        assert p["realised_pnl_usd"] == pytest.approx(3.49)

    def test_missing_log_is_honest(self, monkeypatch, tmp_path):
        root = _fake_pipeline(tmp_path)
        (root / "reports" / "paper_log.csv").unlink()
        monkeypatch.setenv("FOREX_DATA_PATH", str(root))
        p = forex_paper()
        assert p["log_file"] is None
        assert p["trades"] == 0


class TestIbkr:
    def test_reachable(self, monkeypatch):
        class _Conn:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(socket, "create_connection", lambda *a, **k: _Conn())
        out = forex_ibkr()
        assert out["reachable"] is True

    def test_unreachable_reports_error(self, monkeypatch):
        def _boom(*a, **k):
            raise OSError("connection refused")
        monkeypatch.setattr(socket, "create_connection", _boom)
        out = forex_ibkr()
        assert out["reachable"] is False
        assert "connection refused" in out["error"]


class TestEvents:
    def test_missing_archive_is_honest(self, monkeypatch, tmp_path):
        root = _fake_pipeline(tmp_path)
        monkeypatch.setenv("FOREX_DATA_PATH", str(root))
        ev = forex_events()
        assert ev["rows"] == []

    def test_upcoming_parquet_rows(self, monkeypatch, tmp_path):
        pd = pytest.importorskip("pandas")
        root = _fake_pipeline(tmp_path)
        now = pd.Timestamp.utcnow()
        df = pd.DataFrame(
            [
                {"event_id": "e1", "title": "NFP", "country": "US", "currency": "USD",
                 "date_utc": (now + pd.Timedelta(hours=3)).isoformat(),
                 "impact": "High", "forecast": 180.0, "previous": 170.0,
                 "actual": None, "tier": 1},
                {"event_id": "e2", "title": "Old release", "country": "US", "currency": "USD",
                 "date_utc": (now - pd.Timedelta(hours=3)).isoformat(),
                 "impact": "High", "forecast": 1.0, "previous": 1.0,
                 "actual": 1.0, "tier": 1},
            ]
        )
        df.to_parquet(root / "market-data" / "events" / "events.parquet")
        monkeypatch.setenv("FOREX_DATA_PATH", str(root))
        ev = forex_events()
        assert len(ev["rows"]) == 1
        assert ev["rows"][0]["title"] == "NFP"


class TestHandlers:
    def test_not_configured(self, monkeypatch):
        monkeypatch.delenv("FOREX_DATA_PATH", raising=False)
        for fn in (forex_ops._forex_inventory_tool, forex_ops._forex_strategy_tool,
                   forex_ops._forex_events_tool, forex_ops._forex_paper_tool):
            assert "NOT CONFIGURED" in fn({})

    def test_report_consolidates(self, monkeypatch, tmp_path):
        root = _fake_pipeline(tmp_path)
        monkeypatch.setenv("FOREX_DATA_PATH", str(root))
        monkeypatch.setattr(socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(OSError("refused")))
        out = forex_ops._forex_report_tool({})
        assert "FOREX DATA INVENTORY" in out
        assert "FOREX STRATEGY" in out
        assert "FOREX EVENTS" in out
        assert "FOREX PAPER LOG" in out
        assert "FOREX IBKR" in out


class TestRosterWiring:
    def test_forex_agent_registered(self):
        registry = build_general_registry()
        assert "forex" in registry.subagent_names
        sub = registry.get_subagent("forex")
        assert sub is not None
        tool_names = {t.name for t in sub.tools}
        assert {"forex_inventory", "forex_strategy", "forex_events",
                "forex_paper", "forex_ibkr", "forex_report"} <= tool_names

    def test_tool_handler_runs(self):
        registry = build_general_registry()
        for name in ("forex_inventory", "forex_strategy", "forex_events",
                     "forex_paper", "forex_ibkr", "forex_report"):
            spec = registry.lookup(name)
            assert spec is not None
            assert callable(spec.handler)
