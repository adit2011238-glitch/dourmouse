"""FIX-R R-15: evaluating a trade does not trip the process-wide kill-switch; only latch=True does."""

from __future__ import annotations

from dourmouse.guardrails import AccountState, GuardrailConfig, KillSwitch, ProposedTrade, Side, evaluate_trade

CFG = GuardrailConfig(max_position_pct=1.0, max_sector_concentration_pct=1.0, daily_loss_limit_pct=0.03,
                      trade_confirmation_threshold_usd=1e12)
TRADE = ProposedTrade("MSFT", Side.BUY, 1, 100.0, "tech")


def test_a_preview_with_a_wrong_start_of_day_equity_blocks_the_trade_but_does_not_trip_the_switch():
    switch = KillSwitch(0.03)
    yesterday = AccountState(equity=90_000.0, start_of_day_equity=100_000.0)
    decision = evaluate_trade(TRADE, yesterday, CFG, switch)
    assert decision.approved is False and decision.checks["kill_switch"] is False
    assert switch.tripped is False
    # the right snapshot is accepted right after: nothing was left behind by the preview
    today = AccountState(equity=99_500.0, start_of_day_equity=99_800.0)
    assert evaluate_trade(TRADE, today, CFG, switch).approved is True


def test_the_order_path_latches_and_stays_tripped_after_recovery_until_rearm():
    switch = KillSwitch(0.03)
    bad = AccountState(equity=90_000.0, start_of_day_equity=100_000.0)
    assert evaluate_trade(TRADE, bad, CFG, switch, latch=True).approved is False
    assert switch.tripped is True
    recovered = AccountState(equity=99_900.0, start_of_day_equity=100_000.0)
    assert evaluate_trade(TRADE, recovered, CFG, switch).approved is False, "still blocked: only a manual re-arm clears it"
    switch.rearm()
    assert evaluate_trade(TRADE, recovered, CFG, switch).approved is True


def test_an_already_tripped_switch_blocks_a_preview_too():
    switch = KillSwitch(0.03)
    switch.update(100_000.0, 90_000.0)
    ok_account = AccountState(equity=100_000.0, start_of_day_equity=100_000.0)
    assert evaluate_trade(TRADE, ok_account, CFG, switch).approved is False


def test_would_trip_does_not_change_the_switch():
    switch = KillSwitch(0.03)
    assert switch.would_trip(100_000.0, 90_000.0) is True
    assert switch.tripped is False
