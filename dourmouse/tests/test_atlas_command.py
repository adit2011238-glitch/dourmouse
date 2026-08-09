"""v8.1 ATLAS Command Center tests (atlas_command.py).

Hermetic: a fake forex-data tree with a fake validation_standard.json and
fake pipeline scripts (real subprocess execution of tiny echo scripts), so
run-tool behaviour — success output, honest failure, arg validation — is
tested without touching the real pipeline or the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dourmouse import atlas_command as ac
from dourmouse.general_roster import build_general_registry

STANDARD = {
    "generated_utc": "2026-08-09T13:34:03",
    "script": "scripts/seasonal_validation.py",
    "protocol": {
        "in_sample": ["2000-01-01", "2014-12-31"],
        "oos_start": "2015-01-01",
        "stages": ["1", "2", "3", "4", "5"],
    },
    "config": {"T": 2.5, "min_n": 6, "in_sample_legs": ["HE_2", "HE_8", "ZC_12"]},
    "numbers": {
        "permutation_p": 0.006,
        "portfolio_core": {"terminal": 437.94, "sharpe": 3.34, "maxdd_pct": -4.3},
        "portfolio_all": {"terminal": 409.02, "sharpe": 1.43, "maxdd_pct": -15.8},
        "bootstrap": {"terminal_median": 410.0, "terminal_p5": 325.0,
                      "terminal_p95": 490.0, "p_loss_pct": 0.0},
        "legs": {"HE_8": {"n": 12, "t": 5.07, "mean_pct": 13.99, "win_pct": 92.0}},
    },
    "verdict": "core legs HE_8, HE_4, ZC_12.",
}


def _fake_pipeline(tmp_path: Path) -> Path:
    root = tmp_path / "forex-data"
    (root / "scripts").mkdir(parents=True)
    (root / "reports").mkdir(parents=True)
    (root / "market-data" / "events").mkdir(parents=True)
    (root / "reports" / "validation_standard.json").write_text(
        json.dumps(STANDARD), encoding="utf-8"
    )
    (root / "scripts" / "seasonal_calendar.py").write_text(
        "print('FAKE CALENDAR OUTPUT')\n", encoding="utf-8"
    )
    (root / "scripts" / "paper_log.py").write_text(
        "import sys\nprint('FAKE PAPER ' + ' '.join(sys.argv[1:]))\n", encoding="utf-8"
    )
    return root


class TestStandard:
    def test_reads_locked_standard(self, monkeypatch, tmp_path):
        root = _fake_pipeline(tmp_path)
        monkeypatch.setenv("FOREX_DATA_PATH", str(root))
        s = ac.atlas_standard()
        assert s["configured"] is True
        assert s["config"]["T"] == 2.5
        assert s["numbers"]["permutation_p"] == pytest.approx(0.006)
        assert s["numbers"]["portfolio_core"]["terminal"] == pytest.approx(437.94)

    def test_missing_standard_is_honest(self, monkeypatch, tmp_path):
        root = _fake_pipeline(tmp_path)
        (root / "reports" / "validation_standard.json").unlink()
        monkeypatch.setenv("FOREX_DATA_PATH", str(root))
        s = ac.atlas_standard()
        assert "missing" in s["error"]

    def test_unconfigured(self, monkeypatch):
        monkeypatch.delenv("FOREX_DATA_PATH", raising=False)
        assert ac.atlas_standard()["configured"] is False


class TestRunTools:
    def test_calendar_runs_fake_script(self, monkeypatch, tmp_path):
        root = _fake_pipeline(tmp_path)
        monkeypatch.setenv("FOREX_DATA_PATH", str(root))
        out = ac._atlas_calendar_tool({})
        assert "exit 0" in out
        assert "FAKE CALENDAR OUTPUT" in out

    def test_failing_script_is_honest(self, monkeypatch, tmp_path):
        root = _fake_pipeline(tmp_path)
        (root / "scripts" / "seasonal_calendar.py").write_text(
            "print('boom', file=__import__('sys').stderr)\nraise SystemExit(3)\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("FOREX_DATA_PATH", str(root))
        out = ac._atlas_calendar_tool({})
        assert "exit 3" in out
        assert "boom" in out

    def test_paper_open_arg_validation(self, monkeypatch, tmp_path):
        root = _fake_pipeline(tmp_path)
        monkeypatch.setenv("FOREX_DATA_PATH", str(root))
        out = ac._atlas_paper_open_tool({"leg": "HE_8", "date": "2026-08-10",
                                         "price": "not-a-number", "size": 100})
        assert "ERROR" in out
        out2 = ac._atlas_paper_open_tool({"date": "2026-08-10", "price": 1.0, "size": 100})
        assert "ERROR" in out2  # missing leg

    def test_paper_open_runs(self, monkeypatch, tmp_path):
        root = _fake_pipeline(tmp_path)
        monkeypatch.setenv("FOREX_DATA_PATH", str(root))
        out = ac._atlas_paper_open_tool(
            {"leg": "HE_8", "date": "2026-08-10", "price": 120.5, "size": 120})
        assert "FAKE PAPER" in out
        assert "HE_8" in out

    def test_run_validation_missing_script_is_honest(self, monkeypatch, tmp_path):
        root = _fake_pipeline(tmp_path)
        monkeypatch.setenv("FOREX_DATA_PATH", str(root))
        out = ac._atlas_run_validation_tool({})
        assert "exit" in out  # honest non-zero exit, never fabricated numbers


class TestRosterWiring:
    def test_atlas_cmd_registered(self):
        registry = build_general_registry()
        assert "atlas_cmd" in registry.subagent_names
        sub = registry.get_subagent("atlas_cmd")
        names = {t.name for t in sub.tools}
        assert {"atlas_standard", "atlas_run_validation", "atlas_run_walkforward",
                "atlas_run_backtest", "atlas_calendar", "atlas_refresh_events",
                "atlas_paper_status", "atlas_paper_open", "atlas_paper_close",
                "atlas_full_status"} <= names
