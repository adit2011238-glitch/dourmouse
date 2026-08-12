"""Unit tests for the Phase 0 guardrail engine.

Focus: deliberate edge cases at the safety boundary — exactly-at-threshold,
negative P&L days, concentration exactly at max, the kill-switch latch/re-arm,
and invalid inputs that must raise rather than silently return.
"""

from __future__ import annotations

import pytest

from dourmouse.guardrails import (
    AccountState,
    GuardrailConfig,
    KillSwitch,
    PaperGateCriteria,
    Position,
    ProposedTrade,
    Side,
    daily_loss_exceeded,
    daily_loss_fraction,
    evaluate_paper_gate,
    evaluate_trade,
)


# Default config the user confirmed for Phase 0.
CFG = GuardrailConfig(
    max_position_pct=0.10,
    max_sector_concentration_pct=0.30,
    daily_loss_limit_pct=0.03,
    trade_confirmation_threshold_usd=1000.0,
)


def fresh_switch() -> KillSwitch:
    return KillSwitch(CFG.daily_loss_limit_pct)


def account(equity=100_000.0, sod=100_000.0, positions=None) -> AccountState:
    return AccountState(
        equity=equity,
        start_of_day_equity=sod,
        positions=positions or {},
    )


# --------------------------------------------------------------------------- #
# Config validation
# --------------------------------------------------------------------------- #

class TestConfigValidation:
    @pytest.mark.parametrize("bad", [0.0, -0.1, 1.5])
    def test_out_of_range_pct_rejected(self, bad):
        with pytest.raises(ValueError):
            GuardrailConfig(max_position_pct=bad)

    def test_negative_confirmation_threshold_rejected(self):
        with pytest.raises(ValueError):
            GuardrailConfig(trade_confirmation_threshold_usd=-1.0)

    def test_pct_of_exactly_one_allowed(self):
        cfg = GuardrailConfig(max_position_pct=1.0)
        assert cfg.max_position_pct == 1.0


# --------------------------------------------------------------------------- #
# Daily loss / kill-switch
# --------------------------------------------------------------------------- #

class TestDailyLoss:
    def test_loss_fraction_positive_on_loss(self):
        assert daily_loss_fraction(100_000, 97_000) == pytest.approx(0.03)

    def test_loss_fraction_negative_on_gain(self):
        assert daily_loss_fraction(100_000, 101_000) == pytest.approx(-0.01)

    def test_exactly_at_limit_trips(self):
        # 3% loss with a 3% limit -> tripped (at-or-beyond).
        assert daily_loss_exceeded(100_000, 97_000, 0.03) is True

    def test_just_under_limit_does_not_trip(self):
        assert daily_loss_exceeded(100_000, 97_001, 0.03) is False

    def test_beyond_limit_trips(self):
        assert daily_loss_exceeded(100_000, 90_000, 0.03) is True

    def test_non_positive_start_equity_raises(self):
        with pytest.raises(ValueError):
            daily_loss_fraction(0.0, 100.0)
        with pytest.raises(ValueError):
            daily_loss_fraction(-5.0, 100.0)


class TestKillSwitch:
    def test_starts_untripped(self):
        assert fresh_switch().tripped is False

    def test_trips_at_exact_limit(self):
        ks = fresh_switch()
        assert ks.update(100_000, 97_000) is True
        assert ks.tripped is True

    def test_latches_after_intraday_recovery(self):
        ks = fresh_switch()
        ks.update(100_000, 96_000)  # -4%, trips
        assert ks.tripped is True
        # Account recovers above the line — must STAY tripped.
        ks.update(100_000, 100_000)
        assert ks.tripped is True

    def test_manual_rearm_clears(self):
        ks = fresh_switch()
        ks.update(100_000, 95_000)
        assert ks.tripped is True
        ks.rearm()
        assert ks.tripped is False

    def test_does_not_trip_on_small_loss(self):
        ks = fresh_switch()
        assert ks.update(100_000, 98_000) is False  # -2%
        assert ks.tripped is False

    def test_invalid_limit_raises(self):
        with pytest.raises(ValueError):
            KillSwitch(0.0)
        with pytest.raises(ValueError):
            KillSwitch(1.5)


# --------------------------------------------------------------------------- #
# Max position size
# --------------------------------------------------------------------------- #

class TestPositionSize:
    def test_buy_under_limit_approved(self):
        trade = ProposedTrade("AAPL", Side.BUY, 10, 100.0, "tech")  # $1,000 = 1%
        d = evaluate_trade(trade, account(), CFG, fresh_switch())
        assert d.checks["max_position_size"] is True

    def test_buy_exactly_at_limit_approved(self):
        # $10,000 on $100k equity == exactly 10%. At-limit passes.
        trade = ProposedTrade("AAPL", Side.BUY, 100, 100.0, "tech")
        d = evaluate_trade(trade, account(), CFG, fresh_switch())
        assert d.checks["max_position_size"] is True
        assert d.approved is True

    def test_buy_one_cent_over_limit_rejected(self):
        # $10,000.01 == just over 10%.
        trade = ProposedTrade("AAPL", Side.BUY, 1, 10_000.01, "tech")
        d = evaluate_trade(trade, account(), CFG, fresh_switch())
        assert d.checks["max_position_size"] is False
        assert d.approved is False
        assert any("position size" in r for r in d.reasons)

    def test_adding_to_existing_position_counts_total(self):
        positions = {"AAPL": Position("AAPL", 9_500.0, "tech")}
        # +$600 -> $10,100 total == over 10%.
        trade = ProposedTrade("AAPL", Side.BUY, 6, 100.0, "tech")
        d = evaluate_trade(trade, account(positions=positions), CFG, fresh_switch())
        assert d.checks["max_position_size"] is False

    def test_sell_reducing_oversized_position_not_blocked(self):
        # An already-oversized $20k position; selling some must NOT be rejected
        # by the position-size check (de-risking is always allowed).
        positions = {"AAPL": Position("AAPL", 20_000.0, "tech")}
        trade = ProposedTrade("AAPL", Side.SELL, 50, 100.0, "tech")  # -$5,000
        d = evaluate_trade(trade, account(positions=positions), CFG, fresh_switch())
        assert d.checks["max_position_size"] is True


# --------------------------------------------------------------------------- #
# Sector concentration
# --------------------------------------------------------------------------- #

class TestSectorConcentration:
    def test_exactly_at_max_approved(self):
        # Existing $25k tech; add $5k tech -> $30k == exactly 30%.
        positions = {"MSFT": Position("MSFT", 25_000.0, "tech")}
        trade = ProposedTrade("AAPL", Side.BUY, 50, 100.0, "tech")  # $5,000
        d = evaluate_trade(trade, account(positions=positions), CFG, fresh_switch())
        assert d.checks["max_sector_concentration"] is True
        # Note: position-size check independently allows $5k (<10%).

    def test_one_cent_over_max_rejected(self):
        positions = {"MSFT": Position("MSFT", 25_000.0, "tech")}
        trade = ProposedTrade("AAPL", Side.BUY, 1, 5_000.01, "tech")
        d = evaluate_trade(trade, account(positions=positions), CFG, fresh_switch())
        assert d.checks["max_sector_concentration"] is False
        assert d.approved is False
        assert any("sector" in r for r in d.reasons)

    def test_other_sector_unaffected(self):
        positions = {"MSFT": Position("MSFT", 29_000.0, "tech")}
        # Buying in 'energy' should not be blocked by tech concentration.
        trade = ProposedTrade("XOM", Side.BUY, 50, 100.0, "energy")
        d = evaluate_trade(trade, account(positions=positions), CFG, fresh_switch())
        assert d.checks["max_sector_concentration"] is True

    def test_sell_reducing_sector_not_blocked(self):
        positions = {
            "MSFT": Position("MSFT", 25_000.0, "tech"),
            "AAPL": Position("AAPL", 20_000.0, "tech"),  # sector already $45k > 30%
        }
        trade = ProposedTrade("AAPL", Side.SELL, 100, 100.0, "tech")  # -$10,000
        d = evaluate_trade(trade, account(positions=positions), CFG, fresh_switch())
        assert d.checks["max_sector_concentration"] is True


# --------------------------------------------------------------------------- #
# Confirmation threshold
# --------------------------------------------------------------------------- #

class TestConfirmationThreshold:
    def test_below_threshold_no_confirm(self):
        trade = ProposedTrade("AAPL", Side.BUY, 1, 999.99, "tech")
        d = evaluate_trade(trade, account(), CFG, fresh_switch())
        assert d.requires_confirmation is False
        assert d.checks["under_confirmation_threshold"] is True

    def test_exactly_at_threshold_requires_confirm(self):
        trade = ProposedTrade("AAPL", Side.BUY, 10, 100.0, "tech")  # exactly $1,000
        d = evaluate_trade(trade, account(), CFG, fresh_switch())
        assert d.requires_confirmation is True
        assert d.checks["under_confirmation_threshold"] is False
        # Still approved — confirmation is a gate, not a rejection.
        assert d.approved is True

    def test_above_threshold_requires_confirm(self):
        trade = ProposedTrade("AAPL", Side.BUY, 50, 100.0, "tech")  # $5,000
        d = evaluate_trade(trade, account(), CFG, fresh_switch())
        assert d.requires_confirmation is True


# --------------------------------------------------------------------------- #
# Kill-switch integration with evaluate_trade
# --------------------------------------------------------------------------- #

class TestKillSwitchBlocksTrades:
    def test_tripped_switch_rejects_any_trade(self):
        ks = fresh_switch()
        ks.update(100_000, 90_000)  # trip it
        trade = ProposedTrade("AAPL", Side.BUY, 1, 10.0, "tech")  # tiny, harmless
        d = evaluate_trade(trade, account(), CFG, ks)
        assert d.approved is False
        assert d.checks["kill_switch"] is False
        assert any("kill-switch" in r for r in d.reasons)

    def test_rearmed_switch_allows_trades_again(self):
        ks = fresh_switch()
        ks.update(100_000, 90_000)
        ks.rearm()
        trade = ProposedTrade("AAPL", Side.BUY, 1, 10.0, "tech")
        d = evaluate_trade(trade, account(), CFG, ks)
        assert d.approved is True
        assert d.checks["kill_switch"] is True


# --------------------------------------------------------------------------- #
# evaluate_trade input validation
# --------------------------------------------------------------------------- #

class TestEvaluateTradeValidation:
    def test_non_positive_equity_raises(self):
        trade = ProposedTrade("AAPL", Side.BUY, 1, 10.0, "tech")
        with pytest.raises(ValueError):
            evaluate_trade(trade, account(equity=0.0), CFG, fresh_switch())

    def test_clean_trade_fully_approved_no_confirm(self):
        trade = ProposedTrade("AAPL", Side.BUY, 5, 100.0, "tech")  # $500
        d = evaluate_trade(trade, account(), CFG, fresh_switch())
        assert d.approved is True
        assert d.requires_confirmation is False
        assert all(d.checks.values())


# --------------------------------------------------------------------------- #
# Paper-trading gate
# --------------------------------------------------------------------------- #

class TestAccessors:
    def test_sector_of_known_and_unknown(self):
        acct = account(positions={"AAPL": Position("AAPL", 1_000.0, "tech")})
        assert acct.sector_of("AAPL") == "tech"
        assert acct.sector_of("NVDA") is None

    def test_kill_switch_limit_pct_exposed(self):
        assert fresh_switch().limit_pct == CFG.daily_loss_limit_pct

    def test_decision_as_dict_roundtrip(self):
        trade = ProposedTrade("AAPL", Side.BUY, 5, 100.0, "tech")
        d = evaluate_trade(trade, account(), CFG, fresh_switch())
        dumped = d.as_dict()
        assert dumped["approved"] is True
        assert set(dumped) == {"approved", "requires_confirmation", "reasons", "checks"}
        # Returned collections are copies, not internal references.
        dumped["reasons"].append("mutation")
        assert d.reasons == []


class TestPaperGate:
    CRIT = PaperGateCriteria(min_trading_days=20, max_kill_switch_trips=0, max_drawdown_pct=0.10)

    def test_passes_when_all_criteria_met(self):
        r = evaluate_paper_gate(20, 0, 0.05, self.CRIT)
        assert r.passed is True
        assert all(r.checks.values())

    def test_fails_on_insufficient_days(self):
        r = evaluate_paper_gate(19, 0, 0.05, self.CRIT)
        assert r.passed is False
        assert r.checks["min_trading_days"] is False
        assert any("trading days" in x for x in r.reasons)

    def test_exact_minimum_days_passes(self):
        r = evaluate_paper_gate(20, 0, 0.05, self.CRIT)
        assert r.checks["min_trading_days"] is True

    def test_fails_on_kill_switch_trip(self):
        r = evaluate_paper_gate(30, 1, 0.05, self.CRIT)
        assert r.passed is False
        assert r.checks["kill_switch_trips"] is False

    def test_fails_on_excess_drawdown(self):
        r = evaluate_paper_gate(30, 0, 0.11, self.CRIT)
        assert r.passed is False
        assert r.checks["max_drawdown"] is False

    def test_drawdown_exactly_at_max_passes(self):
        r = evaluate_paper_gate(30, 0, 0.10, self.CRIT)
        assert r.checks["max_drawdown"] is True

    def test_invalid_criteria_raise(self):
        with pytest.raises(ValueError):
            PaperGateCriteria(min_trading_days=0)
        with pytest.raises(ValueError):
            PaperGateCriteria(min_trading_days=20, max_kill_switch_trips=-1)
        with pytest.raises(ValueError):
            PaperGateCriteria(min_trading_days=20, max_drawdown_pct=1.5)

    def test_negative_runtime_inputs_raise(self):
        with pytest.raises(ValueError):
            evaluate_paper_gate(-1, 0, 0.05, self.CRIT)
        with pytest.raises(ValueError):
            evaluate_paper_gate(20, -1, 0.05, self.CRIT)
