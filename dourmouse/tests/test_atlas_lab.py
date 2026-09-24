"""Tests for the ATLAS LAB window backend (v5.22.6).

Covers the three pieces of the dedicated Atlas window:
1. ``leaderboard()`` — best→worst ordering with full metrics.
2. Auto-sync daemon — idempotent start, live pull loop.
3. The ``/atlas-lab`` page + ``/api/atlas-lab/leaderboard`` routes.
"""

from __future__ import annotations

import json
import threading

import pytest

from dourmouse.atlas import atlas_lab as al

# Reuse the real-server fixture from test_webui (a plain pytest fixture in
# the same package) so these tests exercise the ACTUAL /atlas-lab routes.
from dourmouse.tests.test_webui import server  # noqa: F401, E402

# --------------------------------------------------------------------------- #
# Fixtures: hermetic state (no real repo, no network)
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    """Reset the module state per test so nothing leaks between tests, and
    keep every test off the network.

    Finding #084: this fixture used to reset _LAB_STATE to None and nothing
    else, so any test that reached get_state() (the route tests, and every
    helper that reads state) started a REAL background git pull of the
    GitHub strategy repo into /tmp/atlas-strategy-lab, plus a REAL auto-sync
    daemon that woke 300s later and pulled again after this test's stubs had
    been undone. That real, load-slowed git pull, held under the old
    request-blocking lock, is what timed out test_leaderboard_endpoint under
    full-suite load (X-7). Now: git is stubbed for every test, and any
    daemon a test started is stopped before the next test runs."""
    old_state = al._LAB_STATE
    old_started = al._auto_sync_started
    al._LAB_STATE = None
    al._auto_sync_started = False
    monkeypatch.setattr(al, "_ensure_repo", lambda: "git is disabled in the test suite (hermetic)")
    yield
    al.stop_auto_sync()
    al._LAB_STATE = old_state
    al._auto_sync_started = old_started


def _seed_strategies(rows: list[dict]) -> None:
    """Directly seed the in-memory state with StrategyMetrics (no sync)."""
    al._LAB_STATE = al.StrategyLabState()
    for row in rows:
        al._LAB_STATE.strategies[row["id"]] = al.StrategyMetrics(
            name=row["id"],
            direction=row.get("direction", "LONG"),
            mean_return_pct=row.get("mean_return_pct", 0.0),
            t_stat=row.get("t_stat", 0.0),
            n_observations=row.get("n_observations", 0),
            verdict=row.get("verdict", "HOLD"),
            passed_gates=row.get("passed_gates", ""),
            description=row.get("description", ""),
        )


# --------------------------------------------------------------------------- #
# leaderboard()
# --------------------------------------------------------------------------- #


class TestLeaderboard:
    def test_ranks_by_verdict_then_abs_t(self):
        """PAPER TRADE/STRONG outrank CANDIDATE; within a verdict, |t| wins."""
        _seed_strategies([
            {"id": "A", "verdict": "HOLD", "t_stat": 9.0},
            {"id": "B", "verdict": "CANDIDATE", "t_stat": 1.0},
            {"id": "C", "verdict": "CANDIDATE", "t_stat": 6.0},
            {"id": "D", "verdict": "PAPER TRADE", "t_stat": 2.0},
            {"id": "E", "verdict": "FAIL", "t_stat": 3.0},
        ])
        rows = al.leaderboard()
        assert [r["id"] for r in rows] == ["D", "C", "B", "A", "E"]

    def test_failed_lands_below_holds(self):
        _seed_strategies([
            {"id": "F1", "verdict": "FAIL", "t_stat": 99.0},
            {"id": "H1", "verdict": "HOLD", "t_stat": 0.1},
        ])
        rows = al.leaderboard()
        assert rows[0]["id"] == "H1"
        assert rows[1]["id"] == "F1"

    def test_includes_full_metric_set(self):
        """The window needs p-value/win/sharpe — not just name+t."""
        _seed_strategies([{"id": "S1", "verdict": "CANDIDATE", "t_stat": 2.0}])
        al._LAB_STATE.strategies["S1"].p_value = 0.03
        al._LAB_STATE.strategies["S1"].win_rate_pct = 81.5
        al._LAB_STATE.strategies["S1"].sharpe = 1.2
        al._LAB_STATE.strategies["S1"].median_return_pct = 0.5
        al._LAB_STATE.strategies["S1"].std_dev_pct = 2.0
        row = al.leaderboard()[0]
        assert row["p_value"] == 0.03
        assert row["win_rate_pct"] == 81.5
        assert row["sharpe"] == 1.2
        assert row["median_return_pct"] == 0.5
        assert row["std_dev_pct"] == 2.0
        assert "description" in row

    def test_description_omittable(self):
        _seed_strategies([{"id": "S1", "verdict": "HOLD", "t_stat": 0.5}])
        row = al.leaderboard(include_description=False)[0]
        assert "description" not in row


# --------------------------------------------------------------------------- #
# CSV parsing robustness (the FX battery has quoted commas)
# --------------------------------------------------------------------------- #


class TestCsvParsing:
    def test_quoted_comma_does_not_misalign_verdict(self, tmp_path):
        """A description containing a comma must not shift VERDICT/gates —
        observed live: naive split() put 'FAIL' into the gates column."""
        csv_path = tmp_path / "strict.csv"
        csv_path.write_text(
            "key,label,is_mean,is_t,is_win,p_is,ho_mean,ho_t,ho_win,p_ho,"
            "wf_mean,wf_t,wf_win,wf_median,wf_std,wf_n,p_wf,VERDICT,gates\n"
            'news_EURUSD,"News drift EURUSD, next-day open",0.0001,0.5,0.5,0.1,'
            "0.001,1.9,0.5,0.004,0.0002,1.4,0.5,0.0001,0.001,10,0.11,FAIL,"
            "|t_is|>2; p_is<0.01\n",
            encoding="utf-8",
        )
        strategies = al._parse_strict_battery(csv_path)
        assert "news_EURUSD" in strategies
        s = strategies["news_EURUSD"]
        assert s.verdict == "FAIL"
        assert "p_is<0.01" in s.passed_gates
        assert s.p_value == 0.11
        assert s.win_rate_pct == 50.0

    def test_missing_file_is_honest_empty(self, tmp_path):
        assert al._parse_strict_battery(tmp_path / "nope.csv") == {}
        assert al._parse_fx_leaderboard(tmp_path / "nope.csv") == {}
        assert al._parse_seasonal_leaderboard(tmp_path / "nope.csv") == {}


# --------------------------------------------------------------------------- #
# Auto-sync
# --------------------------------------------------------------------------- #


class TestAutoSync:
    def test_start_auto_sync_is_idempotent(self):
        al.start_auto_sync()
        first = al._auto_sync_started
        al.start_auto_sync()
        assert first is True
        assert al._auto_sync_started is True

    def test_bind_events_hub_stores_hub(self):
        """v5.22.14: the SSE events hub is attached at mount so syncs
        broadcast live to the HUD."""
        # Reset.
        al._hub = None
        events: list[dict] = []
        hub = type("FakeHub", (), {"broadcast": lambda self, p: events.append(p)})()
        al.bind_events_hub(hub)
        assert al._hub is hub
        # Broadcast through the hub works.
        al._hub.broadcast({"type": "strategies_synced", "count": 42})
        assert events and events[-1]["count"] == 42

    def test_sync_broadcasts_via_hub(self, monkeypatch):
        """A successful _sync() must broadcast strategies_synced."""
        events: list[dict] = []
        hub = type("FakeHub", (), {"broadcast": lambda self, p: events.append(p)})()
        al._hub = hub
        # Fake the repo + parse steps so _sync doesn't need real git/files.
        monkeypatch.setattr(al, "_ensure_repo", lambda: None)
        monkeypatch.setattr(al, "_parse_catalog_json", lambda p: {})
        monkeypatch.setattr(al, "_parse_fx_leaderboard", lambda p: {})
        monkeypatch.setattr(al, "_parse_seasonal_leaderboard", lambda p: {})
        monkeypatch.setattr(al, "_parse_strict_battery", lambda p: {})
        monkeypatch.setattr(al, "_read_report_md", lambda p: "")
        # Initialize state so _sync can run.
        al._LAB_STATE = al.StrategyLabState()
        result = al._sync()  # contract: caller must NOT hold _LAB_LOCK
        assert result["ok"] is True
        assert any(e.get("type") == "strategies_synced" for e in events), \
            f"expected strategies_synced broadcast, got {events}"

    def test_auto_sync_loop_survives_failures(self, monkeypatch):
        """A failed pull must not kill the daemon — the next tick retries."""
        calls = {"n": 0}

        def _flaky_sync() -> dict:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("network down")
            return {"ok": True}

        monkeypatch.setattr(al, "_sync", _flaky_sync)
        monkeypatch.setattr(al, "_AUTO_SYNC_INTERVAL_SECONDS", 0.02)
        al._LAB_STATE = al.StrategyLabState()
        # Run the REAL loop on a thread with its own stop signal. It used to be
        # an unstoppable while-True, so this test leaked a live daemon that
        # kept running after the monkeypatches were undone (finding #084).
        stop = threading.Event()
        errors: list[BaseException] = []

        def runner():
            try:
                al._auto_sync_loop(stop)
            except Exception as exc:  # pragma: no cover - safety net
                errors.append(exc)

        t = threading.Thread(target=runner, daemon=True)
        t.start()
        import time
        # Wait for the retry itself, not a fixed nap: a fixed 0.15s was too
        # short on a busy CI runner (macOS job, run 4).
        deadline = time.monotonic() + 5.0
        while calls["n"] < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        stop.set()
        t.join(timeout=2)
        assert not t.is_alive(), "the loop must actually end when stopped"
        assert calls["n"] >= 2, "loop must retry after the failure"
        assert errors == []

    def test_stop_auto_sync_ends_a_started_loop(self, monkeypatch):
        """stop_auto_sync() must genuinely end the daemon start_auto_sync()
        began, and a later start must be a fresh loop, not the old one."""
        monkeypatch.setattr(al, "_AUTO_SYNC_INTERVAL_SECONDS", 0.02)
        monkeypatch.setattr(al, "_sync", lambda: {"ok": True})
        al._LAB_STATE = al.StrategyLabState()
        al.start_auto_sync()
        first_stop = al._auto_sync_stop
        assert al._auto_sync_started is True
        al.stop_auto_sync()
        assert al._auto_sync_started is False
        assert first_stop is not None and first_stop.is_set()
        al.start_auto_sync()
        assert al._auto_sync_stop is not first_stop, "a restart must not reuse the old stop signal"
        assert not al._auto_sync_stop.is_set()


class TestSyncNeverBlocksReaders:
    """Finding #084 regression. The old _sync() ran the git pull while
    holding _LAB_LOCK, and every reader (get_state, leaderboard, the whole
    Atlas API) takes that lock, so a request that arrived mid-sync waited
    for the entire git operation, up to its 60s timeout. That is the real
    cause of the load-dependent leaderboard timeout tracked as X-7."""

    def test_a_reader_is_not_blocked_while_git_is_slow(self, monkeypatch):
        in_git = threading.Event()
        release = threading.Event()

        def slow_git():
            in_git.set()
            release.wait(timeout=10)  # stands in for a slow network pull
            return "stubbed slow git"

        monkeypatch.setattr(al, "_ensure_repo", slow_git)
        al._LAB_STATE = al.StrategyLabState()
        al._auto_sync_started = True  # no daemon for this test
        syncer = threading.Thread(target=al._sync, daemon=True)
        syncer.start()
        assert in_git.wait(timeout=5), "the sync never reached the git step"
        try:
            import time
            t0 = time.perf_counter()
            board = al.leaderboard()
            elapsed = time.perf_counter() - t0
            assert isinstance(board, list)
            assert elapsed < 1.0, f"reader waited {elapsed:.2f}s on an in-flight git pull"
        finally:
            release.set()
            syncer.join(timeout=5)

    def test_two_syncs_never_run_git_at_the_same_time(self, monkeypatch):
        """_SYNC_LOCK must still serialise the slow work, so two syncs can
        never run git on the same checkout concurrently."""
        active = {"now": 0, "peak": 0}
        guard = threading.Lock()

        def counting_git():
            with guard:
                active["now"] += 1
                active["peak"] = max(active["peak"], active["now"])
            import time
            time.sleep(0.05)
            with guard:
                active["now"] -= 1
            return "stubbed"

        monkeypatch.setattr(al, "_ensure_repo", counting_git)
        al._LAB_STATE = al.StrategyLabState()
        threads = [threading.Thread(target=al._sync, daemon=True) for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=5)
        assert active["peak"] == 1, f"{active['peak']} syncs ran git at once"


class TestLatestBacktest:
    """v5.22.15: get_latest_backtest — the briefing reads completed backtests."""

    def test_returns_none_when_no_backtests(self):
        from dourmouse.atlas.atlas_lab import get_latest_backtest
        # Fresh state with an empty requests dict.
        al._LAB_STATE = al.StrategyLabState()
        assert get_latest_backtest() is None

    def test_returns_most_recent_completed(self):
        from dourmouse.atlas.atlas_lab import BacktestRequest, get_latest_backtest
        state = al.StrategyLabState()
        state.backtest_requests["bt_old"] = BacktestRequest(
            id="bt_old", pair="EURUSD", status="done",
            created_at="2026-01-01T00:00:00Z",
            result={"strategy_name": "Old", "verdict": "HOLD",
                    "metrics": {"sharpe_ratio": 0.5}},
        )
        state.backtest_requests["bt_new"] = BacktestRequest(
            id="bt_new", pair="GBPUSD", status="done",
            created_at="2026-02-01T00:00:00Z",
            result={"strategy_name": "New", "verdict": "PAPER TRADE",
                    "metrics": {"sharpe_ratio": 2.1}},
        )
        # A pending request must not be returned.
        state.backtest_requests["bt_pending"] = BacktestRequest(
            id="bt_pending", status="pending", created_at="2026-03-01T00:00:00Z")
        al._LAB_STATE = state
        result = get_latest_backtest()
        assert result is not None, "should return the newest completed"
        assert result["id"] == "bt_new"
        assert result["sharpe_ratio"] == 2.1
        assert result["verdict"] == "PAPER TRADE"

    def test_backtest_completed_broadcasts_via_hub(self, monkeypatch):
        """When a backtest finishes, a backtest_completed event must be
        broadcast on the SSE hub so the HUD shows it live."""
        from dourmouse.atlas import atlas_cli

        events: list[dict] = []
        hub = type("FakeHub", (), {"broadcast": lambda self, p: events.append(p)})()
        al._hub = hub
        monkeypatch.setattr(al, "prompt_to_strategy", lambda p: {
            "strategy_name": "Test", "strategy_type": "momentum",
            "pair": "EURUSD", "explanation": "Test",
            "parameters": {}, "direction": "LONG",
            "entry_condition": "signal", "exit_condition": "reversal",
        })
        monkeypatch.setattr(atlas_cli, "run_atlas_cli",
                            lambda *a, **k: (0, '{"validation": {}}', ""))
        monkeypatch.setattr(al, "_build_report", lambda **k: {
            "strategy_name": "Test", "verdict": "CANDIDATE",
            "summary_markdown": "dummy", "metrics": {},
            "pair": "EURUSD",
        })
        # Run the worker on a fresh state (synchronously).
        state = al.StrategyLabState()
        req_id = "bt_live_test"
        state.backtest_requests[req_id] = al.BacktestRequest(
            id=req_id, prompt="test", pair="EURUSD", status="pending")
        al._LAB_STATE = state
        al._run_backtest_worker(req_id)
        assert any(
            e.get("type") == "backtest_completed" for e in events
        ), f"expected backtest_completed broadcast, got {events}"


# --------------------------------------------------------------------------- #
# Web routes (real server fixture, same shape as test_webui)
# --------------------------------------------------------------------------- #


class TestAtlasLabRoutes:
    """The dedicated Atlas window must serve its page and leaderboard over
    the real server. Requires the ``server`` fixture (the echo-registry
    server from test_webui — importable because it is a plain pytest
    fixture defined in a conftest-visible module)."""

    def _get(self, server, path: str):
        import http.client

        srv, port = server
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        status = resp.status
        conn.close()
        return status, body

    def test_atlas_lab_page_serves(self, server):
        """The dedicated window's page loads (200, HTML)."""
        status, body = self._get(server, "/atlas-lab")
        assert status == 200
        assert b"STRATEGY LAB" in body

    def test_leaderboard_endpoint(self, server):
        """The leaderboard API returns a ranked list."""
        status, body = self._get(server, "/api/atlas-lab/leaderboard")
        assert status == 200
        payload = json.loads(body.decode("utf-8"))
        assert payload["ok"] is True
        assert isinstance(payload["leaderboard"], list)


class TestBuildReportSummary:
    """Real, direct calls to _build_report (not monkeypatched) -- the only
    prior test coverage of this function replaced it entirely
    (monkeypatch.setattr(al, "_build_report", ...) above), so a real,
    live NameError in its summary section (direction/entry/exit_cond
    referenced as bare names that were never assigned, instead of the
    real report["direction"]/["entry_condition"]/["exit_condition"]
    dict keys) went undetected until this exact function was actually
    called with real inputs during the engineering audit."""

    def test_summary_builds_without_crashing_on_a_real_shaped_spec(self):
        spec = {
            "strategy_name": "RSI Reversion",
            "strategy_type": "mean_reversion",
            "direction": "SHORT",
            "entry_condition": "RSI(14) > 70",
            "exit_condition": "RSI(14) < 50",
            "parameters": {"rsi_period": 14},
        }
        report = al._build_report(
            spec=spec, pair="EURUSD", exit_code=0,
            raw_output='{"validation": {"sharpe": 1.2, "win_rate": 0.55}}',
            raw_error="", explanation="Fades RSI overbought/oversold extremes.",
        )
        assert "SHORT" in report["summary_markdown"]
        assert "RSI(14) > 70" in report["summary_markdown"]
        assert "RSI(14) < 50" in report["summary_markdown"]

    def test_summary_handles_missing_entry_and_exit_conditions_honestly(self):
        spec = {"strategy_name": "Untitled", "direction": "LONG"}
        report = al._build_report(
            spec=spec, pair="GBPUSD", exit_code=0,
            raw_output="not json", raw_error="", explanation="No explanation given.",
        )
        assert "See parameters." in report["summary_markdown"]
