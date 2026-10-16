"""FI1 fixes for dourmouse/atlas/atlas_lab.py (P5-5, P5-6, P5-7, P5-8, P5-9).

Hermetic: git is stubbed, the strategy-lab paths point at a temp dir, the
CLI bridge and the LLM are stubbed. Nothing touches the network.
"""

from __future__ import annotations

import json

import pytest

from dourmouse.atlas import atlas_lab as al


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    old_state = al._LAB_STATE
    old_started = al._auto_sync_started
    al._LAB_STATE = None
    al._auto_sync_started = False
    lab = tmp_path / "lab"
    (lab / "reports").mkdir(parents=True)
    monkeypatch.setattr(al, "STRATEGY_LAB_DIR", lab)
    monkeypatch.setattr(al, "STRATEGY_CATALOG_PATH", lab / "strategy_catalog.json")
    monkeypatch.setattr(al, "FX_LEADERBOARD_PATH", lab / "reports" / "fx_lb.csv")
    monkeypatch.setattr(al, "SEASONAL_LEADERBOARD_PATH", lab / "reports" / "seasonal_lb.csv")
    monkeypatch.setattr(al, "STRICT_BATTERY_PATH", lab / "reports" / "strict.csv")
    monkeypatch.setattr(al, "FX_STRICT_BATTERY_PATH", lab / "reports" / "fx_strict.csv")
    monkeypatch.setattr(al, "_ensure_repo", lambda: None)
    yield lab
    al.stop_auto_sync()
    al._LAB_STATE = old_state
    al._auto_sync_started = old_started


# --------------------------------------------------------------------------- #
# P5-5: sync() before the lab state exists
# --------------------------------------------------------------------------- #

def test_sync_works_before_state_exists():
    assert al._LAB_STATE is None
    result = al.sync()
    assert result["ok"] is True
    assert al._LAB_STATE is not None
    assert al._LAB_STATE.last_sync


# --------------------------------------------------------------------------- #
# P5-6: malformed catalog entries do not abort the sync
# --------------------------------------------------------------------------- #

def test_bad_catalog_entry_is_skipped_and_reported(_hermetic):
    catalog = {
        "version": "9",
        "seasonal_locked": {
            "good": {"direction": "LONG", "is_mean_pct": 1.5, "is_t": 2.5, "is_n": 30},
            "bad": {"direction": "LONG", "is_mean_pct": 1.0, "is_t": "n/a", "is_n": 5},
        },
        "fx_candidates": {
            "fx_good": {"direction": "SHORT", "holdout_mean_pct": 0.5, "holdout_t": 1.0, "n": 10},
            "fx_bad": "not an object",
        },
    }
    al.STRATEGY_CATALOG_PATH.write_text(json.dumps(catalog), encoding="utf-8")
    result = al.sync()
    assert result["ok"] is True
    names = set(al._LAB_STATE.strategies)
    assert {"good", "fx_good"} <= names
    assert "bad" not in names and "fx_bad" not in names
    err = al._LAB_STATE.sync_error
    assert "bad" in err and "fx_bad" in err, err
    assert al._LAB_STATE.last_sync


def test_list_shaped_catalog_does_not_crash_and_is_reported(_hermetic):
    al.STRATEGY_CATALOG_PATH.write_text("[1, 2, 3]", encoding="utf-8")
    result = al.sync()
    assert result["ok"] is True
    assert "catalog" in al._LAB_STATE.sync_error.lower()


def test_clean_sync_clears_previous_error(_hermetic):
    al.STRATEGY_CATALOG_PATH.write_text("[]", encoding="utf-8")
    al.sync()
    assert al._LAB_STATE.sync_error
    al.STRATEGY_CATALOG_PATH.write_text(json.dumps({"version": "1"}), encoding="utf-8")
    al.sync()
    assert al._LAB_STATE.sync_error == ""


# --------------------------------------------------------------------------- #
# P5-8: the catalog row wins the merge
# --------------------------------------------------------------------------- #

def test_catalog_row_wins_over_strict_battery(_hermetic):
    catalog = {"seasonal_locked": {"dup": {"direction": "LONG", "is_mean_pct": 2.0,
                                           "is_t": 3.0, "is_n": 40, "commodity": "gold",
                                           "month": 4}}}
    al.STRATEGY_CATALOG_PATH.write_text(json.dumps(catalog), encoding="utf-8")
    al.STRICT_BATTERY_PATH.write_text(
        "name,verdict,description\ndup,FAIL,sparse battery text\n", encoding="utf-8")
    al.sync()
    s = al._LAB_STATE.strategies["dup"]
    assert s.verdict == "CANDIDATE"
    assert "gold" in s.description


# --------------------------------------------------------------------------- #
# P5-9: CLI output parsing
# --------------------------------------------------------------------------- #

def _report(stdout: str) -> dict:
    return al._build_report(spec={"strategy_name": "T", "strategy_type": "momentum"},
                            pair="EURUSD", exit_code=0, raw_output=stdout,
                            raw_error="", explanation="x")


def test_progress_line_before_result_is_skipped():
    out = '{"stage": "loading"}\n{"validation": {"sharpe": 1.2, "n": 100, "mean_return": 0.01}}\n'
    rep = _report(out)
    assert rep["metrics"]["sharpe_ratio"] == 1.2


def test_trailing_text_after_result_is_tolerated():
    out = '{"validation": {"sharpe": 0.9, "n": 50}}\nDone in 3.2s\n'
    rep = _report(out)
    assert rep["metrics"]["sharpe_ratio"] == 0.9


def test_multiline_json_result_parses():
    out = 'log line\n{\n  "validation": {\n    "sharpe": 0.7,\n    "n": 40\n  }\n}\n'
    rep = _report(out)
    assert rep["metrics"]["sharpe_ratio"] == 0.7


def test_unparseable_output_is_not_a_plain_hold():
    rep = _report("{not json at all\nmore text")
    assert rep["verdict"] == "UNPARSED"
    assert rep["worth_paper_trading"] is False
    assert "could not parse" in rep["summary_markdown"].lower()


def test_empty_output_stays_hold():
    rep = _report("")
    assert rep["verdict"] == "HOLD"


# --------------------------------------------------------------------------- #
# P5-7: the user's pair wins; LLM pair and strategy are validated
# --------------------------------------------------------------------------- #

def _run_worker(monkeypatch, spec, req_pair):
    from dourmouse.atlas import atlas_cli

    calls: list[list[str]] = []

    def fake_cli(argv, timeout=0):
        calls.append(list(argv))
        return 0, '{"validation": {"sharpe": 1.0, "n": 10}}', ""

    monkeypatch.setattr(atlas_cli, "run_atlas_cli", fake_cli)
    monkeypatch.setattr(al, "prompt_to_strategy", lambda p: spec)
    state = al.StrategyLabState()
    state.backtest_requests["r1"] = al.BacktestRequest(
        id="r1", prompt="p", pair=req_pair, status="pending")
    al._LAB_STATE = state
    al._run_backtest_worker("r1")
    return state.backtest_requests["r1"], calls


_SPEC = {"strategy_name": "N", "strategy_type": "momentum", "pair": "EURUSD",
         "explanation": "e"}


def test_user_supplied_pair_beats_llm_pair(monkeypatch):
    req, calls = _run_worker(monkeypatch, dict(_SPEC), "GBPUSD")
    assert req.status == "done"
    assert calls[0][calls[0].index("--pair") + 1] == "GBPUSD"


def test_default_pair_defers_to_llm_pair(monkeypatch):
    spec = dict(_SPEC, pair="USDJPY")
    req, calls = _run_worker(monkeypatch, spec, "EURUSD")
    assert calls[0][calls[0].index("--pair") + 1] == "USDJPY"


@pytest.mark.parametrize("bad_pair", ["--evil", "eurusd; rm", ["EURUSD"], "EUR", None])
def test_invalid_llm_pair_is_refused_without_running_the_cli(monkeypatch, bad_pair):
    spec = dict(_SPEC, pair=bad_pair)
    req, calls = _run_worker(monkeypatch, spec, "EURUSD" if bad_pair is None else "EURUSD")
    # req pair is the default, so the LLM pair is consulted and rejected
    assert req.status == "failed"
    assert "pair" in req.error.lower()
    assert calls == []


@pytest.mark.parametrize("bad_type", ["--windows", "rm -rf", ["momentum"], 7])
def test_invalid_strategy_type_is_refused(monkeypatch, bad_type):
    spec = dict(_SPEC, strategy_type=bad_type)
    req, calls = _run_worker(monkeypatch, spec, "GBPUSD")
    assert req.status == "failed"
    assert "strategy_type" in req.error
    assert calls == []


def test_non_dict_spec_is_a_clean_runtime_error(monkeypatch):
    monkeypatch.setattr(al, "_llm_chat", lambda prompt, system="": "[1, 2]")
    with pytest.raises(RuntimeError):
        al.prompt_to_strategy("x")
