"""FS1 fixes in dourmouse/guardrails.py: P4-24 (evaluate_trade ignored the
start-of-day equity it is handed) and P4-25 (a held symbol under another
sector label escaped the sector-concentration check)."""

from __future__ import annotations

from dourmouse.guardrails import (
    AccountState,
    GuardrailConfig,
    KillSwitch,
    Position,
    ProposedTrade,
    Side,
    evaluate_trade,
)

CFG = GuardrailConfig(
    max_position_pct=1.0, max_sector_concentration_pct=0.30,
    daily_loss_limit_pct=0.03, trade_confirmation_threshold_usd=1e12,
)


def test_daily_loss_is_enforced_from_the_account_snapshot():
    account = AccountState(equity=90_000.0, start_of_day_equity=100_000.0)
    switch = KillSwitch(CFG.daily_loss_limit_pct)
    trade = ProposedTrade("MSFT", Side.BUY, 1, 100.0, "tech")
    decision = evaluate_trade(trade, account, CFG, switch)
    assert decision.approved is False
    assert decision.checks["kill_switch"] is False
    # FIX-R W1R-15: evaluating no longer latches (a preview must not trip the process-wide switch);
    # the order-submission call asks for it with latch=True.
    assert not switch.tripped
    evaluate_trade(trade, account, CFG, switch, latch=True)
    assert switch.tripped  # latched, as the docstring promises


def test_a_small_loss_does_not_trip():
    account = AccountState(equity=99_000.0, start_of_day_equity=100_000.0)
    decision = evaluate_trade(ProposedTrade("MSFT", Side.BUY, 1, 100.0, "tech"), account, CFG, KillSwitch(0.03))
    assert decision.approved is True


def test_sector_check_uses_the_held_positions_sector():
    account = AccountState(
        equity=100_000.0, start_of_day_equity=100_000.0,
        positions={"AAPL": Position("AAPL", 25_000.0, "tech")},
    )
    trade = ProposedTrade("AAPL", Side.BUY, 200, 100.0, "other")  # +20k into a tech holding
    decision = evaluate_trade(trade, account, CFG, KillSwitch(0.03))
    assert decision.checks["max_sector_concentration"] is False
    assert decision.approved is False
    assert "'tech'" in " ".join(decision.reasons)
