"""FI1 fix P3-19: one unparseable pnl_usd cell must not turn the realised P&L
into $0.00."""

from __future__ import annotations

import pytest

from dourmouse import forex_ops
from dourmouse.forex_ops import forex_paper


def _root(tmp_path, rows):
    root = tmp_path / "forex-data"
    (root / "reports").mkdir(parents=True)
    (root / "reports" / "paper_log.csv").write_text(
        "key,entry_date,entry_price,size,exit_date,exit_price,pnl_usd\n" + "\n".join(rows) + "\n",
        encoding="utf-8",
    )
    return root


def test_good_rows_are_summed_when_one_cell_is_bad(monkeypatch, tmp_path):
    root = _root(tmp_path, [
        "A,2026-01-01,1,1,2026-01-05,2,10.50",
        "B,2026-02-01,1,1,2026-02-05,2,N/A",
        "C,2026-03-01,1,1,2026-03-05,2,\"1,234.50\"",
        "D,2026-04-01,1,1,2026-04-05,2,-3.00",
    ])
    monkeypatch.setenv("FOREX_DATA_PATH", str(root))
    p = forex_paper()
    assert p["realised_pnl_usd"] == pytest.approx(7.50)
    assert p["unparseable_pnl_rows"] == 2
    assert p["closed_trades"] == 4


def test_nan_cell_is_unparseable_not_poisoning_the_sum(monkeypatch, tmp_path):
    root = _root(tmp_path, ["A,2026-01-01,1,1,2026-01-05,2,nan", "B,2026-02-01,1,1,2026-02-05,2,4"])
    monkeypatch.setenv("FOREX_DATA_PATH", str(root))
    p = forex_paper()
    assert p["realised_pnl_usd"] == pytest.approx(4.0)
    assert p["unparseable_pnl_rows"] == 1


def test_clean_log_reports_zero_unparseable(monkeypatch, tmp_path):
    root = _root(tmp_path, ["A,2026-01-01,1,1,2026-01-05,2,2.5"])
    monkeypatch.setenv("FOREX_DATA_PATH", str(root))
    assert forex_paper()["unparseable_pnl_rows"] == 0


def test_tool_text_warns_when_rows_were_skipped(monkeypatch, tmp_path):
    root = _root(tmp_path, ["A,2026-01-01,1,1,2026-01-05,2,N/A", "B,2026-02-01,1,1,2026-02-05,2,4"])
    monkeypatch.setenv("FOREX_DATA_PATH", str(root))
    text = forex_ops._forex_paper_tool({})
    assert "realised P&L: $4.00" in text
    assert "1 closed trade(s) with an unreadable pnl_usd" in text
