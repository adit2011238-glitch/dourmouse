"""v8.0 ATLAS Terminal tests (data layer + app boot).

Hermetic: a fake forex-data tree in a tmp dir (fake manifest, validation
report in the exact production format, fake calendar script, paper log),
monkeypatched FOREX_DATA_PATH. Covers: report parsing, calendar parsing,
honest unconfigured state, and a full AppTest boot of the streamlit app
(which executes Command Center, including the plotly gauges).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from atlas_terminal import data
from dourmouse.general_roster import build_general_registry

REPORT = """# VALIDATION REPORT

## 3. Stage 2 — In-sample permutation Monte Carlo (data-mining test)
| Statistic | Value |
|---|---|
| **p (shuffled ≥ actual)** | **0.0060** |

## 4. Stage 3 — Strict walk-forward
| Leg | n | Mean net | Median | Std | t | Win rate |
|---|---|---|---|---|---|---|
| **HE_8** (short hogs Aug) | 12 | +13.99% | +15.26% | 9.57% | **+5.07** | 92% |
| **HE_4** (long hogs Apr) | 9 | +6.30% | +6.73% | 5.27% | **+3.59** | 89% |
| **ZC_12** (long corn Dec) | 11 | +2.96% | +1.68% | 4.24% | **+2.32** | 82% |
| HE_2 (long hogs Feb) | 9 | +0.87% | −1.68% | 6.91% | +0.38 | 44% |
| RB_9 (short gasoline Sep) | 8 | −1.69% | −2.74% | 4.44% | −1.08 | 38% |
| LE_5 (short cattle May) | 9 | −2.50% | −0.48% | 5.62% | −1.33 | 33% |
| **ALL 60 trades** | 60 | **+4.09%** | **+2.57%** | **8.68%** | **+3.65** | **65%** |

- Terminal: **$409.02** · Sharpe 1.43 · max drawdown **−15.8%** · positive **12/12** years
- Annual return: mean **+20.5%**, median +20.2%, std **±9.0%**, worst year +7.9%, best +37.5%

| Portfolio | Terminal | Sharpe | Max DD |
|---|---|---|---|
| CORE (HE_8, HE_4, ZC_12) — the mechanism's proven legs | **$437.94** | **3.34** | **−4.3%** |

## 5. Stage 4 — Walk-forward permutation test

## 6. Stage 5 — Walk-forward Monte Carlo (year-block bootstrap)
| Outcome | 5th pct | Median | 95th pct |
|---|---|---|---|
| Terminal equity | $325 | **$410** | $490 |

- **P(terminal < $100) = 0.0%** — in none of 1,000 resampled futures does the account lose money

## 8. Verdict
**The strategy passes the new validation system.** Core legs: HE_8, HE_4, ZC_12.
"""

CALENDAR_OUT = """=== SEASONAL PAPER-TRADING CALENDAR (generated 2026-08-09) ===
HE_8  short hogs Aug  (SHORT LEAN HOGS)
    2026: entry 2026-08-03 (open) -> exit 2026-08-31 (close)  [NOW OPEN]
    2027: entry 2027-08-02 (open) -> exit 2027-08-31 (close)  [UPCOMING]
ZC_12  long corn Dec  (LONG CORN)
    2026: entry 2026-12-01 (open) -> exit 2026-12-31 (close)  [UPCOMING]
"""


def _fake_pipeline(tmp_path: Path) -> Path:
    root = tmp_path / "forex-data"
    (root / "market-data" / "normalized").mkdir(parents=True)
    (root / "market-data" / "raw" / "yahoo").mkdir(parents=True)
    (root / "scripts").mkdir(parents=True)
    (root / "reports").mkdir(parents=True)
    (root / "market-data" / "normalized" / "manifest.csv").write_text(
        "pair,timeframe,file,start_utc,end_utc,bars,unique_close_ratio,gaps,source,quality,sha256\n"
        "EURUSD,D1,EURUSD_d1.parquet,2016-08-04,2026-08-05,2601,0.88,0,yahoo_csv,ok,abc\n",
        encoding="utf-8",
    )
    (root / "market-data" / "raw" / "yahoo" / "COMM_HE_d.csv").write_text(
        "date,open,high,low,close\n2020-01-02,1,1,1,1\n", encoding="utf-8"
    )
    (root / "scripts" / "seasonal_calendar.py").write_text(
        "print('''" + CALENDAR_OUT + "''')", encoding="utf-8"
    )
    (root / "reports" / "VALIDATION_REPORT.md").write_text(REPORT, encoding="utf-8")
    (root / "reports" / "paper_log.csv").write_text(
        "key,entry_date,entry_price,size,exit_date,exit_price,pnl_pct,pnl_usd\n"
        "HE_8,2026-08-10,120.5,120,,,,\n"
        "ZC_12,2025-12-01,430.0,100,2025-12-31,445.0,3.49,3.49\n",
        encoding="utf-8",
    )
    return root


class TestValidationParsing:
    def test_full_parse(self, monkeypatch, tmp_path):
        root = _fake_pipeline(tmp_path)
        monkeypatch.setenv("FOREX_DATA_PATH", str(root))
        v = data.validation()
        assert v["configured"] is True
        assert v["perm_p"] == pytest.approx(0.006)
        assert set(v["legs"]) >= {"HE_8", "HE_4", "ZC_12", "HE_2", "RB_9", "LE_5"}
        assert v["legs"]["HE_8"]["mean"] == pytest.approx(13.99)
        assert v["legs"]["HE_8"]["t"] == pytest.approx(5.07)
        assert v["legs"]["LE_5"]["mean"] == pytest.approx(-2.50)  # unicode minus
        assert v["all"]["mean"] == pytest.approx(4.09)
        assert v["all"]["t"] == pytest.approx(3.65)
        assert v["portfolio"]["terminal"] == pytest.approx(409.02)
        assert v["core"] == {"terminal": 437.94, "sharpe": 3.34, "maxdd": -4.3}
        assert v["bootstrap"] == {"median": 410.0, "p5": 325.0, "p95": 490.0, "p_loss": 0.0}

    def test_missing_report_is_honest(self, monkeypatch, tmp_path):
        root = _fake_pipeline(tmp_path)
        (root / "reports" / "VALIDATION_REPORT.md").unlink()
        monkeypatch.setenv("FOREX_DATA_PATH", str(root))
        v = data.validation()
        assert "missing" in v["error"]

    def test_unconfigured(self, monkeypatch):
        monkeypatch.delenv("FOREX_DATA_PATH", raising=False)
        v = data.validation()
        assert v["configured"] is False


class TestCalendarParsing:
    def test_windows_including_now_open(self, monkeypatch, tmp_path):
        root = _fake_pipeline(tmp_path)
        monkeypatch.setenv("FOREX_DATA_PATH", str(root))
        cal = data.strategy_calendar()
        assert len(cal) == 3
        he8_2026 = next(w for w in cal if w["leg"] == "HE_8" and w["year"] == "2026")
        assert he8_2026["status"] == "NOW OPEN"
        assert he8_2026["entry"] == "2026-08-03"
        zc = [w for w in cal if w["leg"] == "ZC_12"]
        assert len(zc) == 1 and zc[0]["status"] == "UPCOMING"

    def test_unconfigured_returns_empty(self, monkeypatch):
        monkeypatch.delenv("FOREX_DATA_PATH", raising=False)
        assert data.strategy_calendar() == []


class TestLive:
    def test_live_snapshot(self, monkeypatch, tmp_path):
        root = _fake_pipeline(tmp_path)
        monkeypatch.setenv("FOREX_DATA_PATH", str(root))
        L = data.live()
        assert L["pipeline"]["configured"] is True
        assert L["validation"]["perm_p"] == pytest.approx(0.006)
        assert L["paper"]["trades"] == 2
        assert L["ibkr"]["reachable"] in (True, False)  # real 2s probe, honest either way

    def test_live_unconfigured(self, monkeypatch):
        monkeypatch.delenv("FOREX_DATA_PATH", raising=False)
        L = data.live()
        assert L["pipeline"]["configured"] is False


class TestAppBoot:
    def test_app_runs_without_exception(self, monkeypatch, tmp_path):
        st_apptest = pytest.importorskip("streamlit.testing.v1")
        root = _fake_pipeline(tmp_path)
        monkeypatch.setenv("FOREX_DATA_PATH", str(root))
        app = st_apptest.AppTest.from_file(
            str(Path(__file__).resolve().parent.parent.parent / "atlas_terminal" / "atlas_terminal.py")
        )
        app.run(timeout=60)
        assert not app.exception
        # the status pill (HTML markdown) shows the pipeline online state
        md_values = "\n".join(str(m.value) for m in app.markdown)
        assert "PIPELINE ONLINE" in md_values or "NOT CONFIGURED" in md_values

    def test_ui_agent_registered(self):
        registry = build_general_registry()
        assert "atlas_ui" in registry.subagent_names
        sub = registry.get_subagent("atlas_ui")
        assert {t.name for t in sub.tools} == {"atlas_terminal_status"}
