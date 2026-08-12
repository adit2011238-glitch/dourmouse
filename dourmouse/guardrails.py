"""Deterministic guardrail / risk engine (Dourmouse — Phase 0).

This module is the SAFETY BOUNDARY. Everything here is plain, deterministic
Python — no LLM calls, no network, no file I/O. The engine is *parameterized*:
the user's risk numbers arrive via ``GuardrailConfig`` (loaded from env in
``config.py``) and are never hardcoded into the logic below.

Design decisions (see conversation / PROGRESS.md):
- Position-size and sector-concentration limits only *block increases* in
  exposure. A trade that reduces absolute exposure (e.g. trimming a long) is
  never rejected by those two checks — de-risking must always be allowed.
- "At or below the limit passes; strictly above fails." A position exactly at
  the max is allowed.
- The daily-loss kill-switch measures loss against the START-OF-DAY equity
  snapshot. Loss *at or beyond* the limit trips it (conservative). Once
  tripped it LATCHES and only ``KillSwitch.rearm()`` clears it.
- The trade-size confirmation threshold is *not* a rejection: a trade at or
  above the notional threshold is APPROVED but flagged ``requires_confirmation``
  so a human must say yes before Execution acts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class GuardrailConfig:
    """User-supplied risk limits (fractions are of capital, 0.10 == 10%)."""

    max_position_pct: float = 0.10
    max_sector_concentration_pct: float = 0.30
    daily_loss_limit_pct: float = 0.03
    trade_confirmation_threshold_usd: float = 1000.0

    def __post_init__(self) -> None:
        for name in (
            "max_position_pct",
            "max_sector_concentration_pct",
            "daily_loss_limit_pct",
        ):
            val = getattr(self, name)
            if not (0.0 < val <= 1.0):
                raise ValueError(f"{name} must be in (0, 1], got {val!r}")
        if self.trade_confirmation_threshold_usd < 0:
            raise ValueError(
                "trade_confirmation_threshold_usd must be >= 0, "
                f"got {self.trade_confirmation_threshold_usd!r}"
            )


@dataclass(frozen=True)
class Position:
    """A currently-held position. ``market_value`` is signed (long > 0)."""

    symbol: str
    market_value: float
    sector: str


@dataclass(frozen=True)
class AccountState:
    """Snapshot of the account used for risk evaluation.

    - ``equity``: current total account equity, the capital base for
      position-size and sector-concentration limits.
    - ``start_of_day_equity``: equity at market open, the base for the
      daily-loss kill-switch.
    - ``positions``: symbol -> Position for everything currently held.
    """

    equity: float
    start_of_day_equity: float
    positions: Dict[str, Position] = field(default_factory=dict)

    def sector_of(self, symbol: str) -> str | None:
        pos = self.positions.get(symbol)
        return pos.sector if pos else None


@dataclass(frozen=True)
class ProposedTrade:
    symbol: str
    side: Side
    quantity: float
    price: float
    sector: str

    @property
    def notional(self) -> float:
        return abs(self.quantity * self.price)

    @property
    def signed_delta(self) -> float:
        """Change to the symbol's signed market value if this trade fills."""
        magnitude = self.quantity * self.price
        return magnitude if self.side == Side.BUY else -magnitude


@dataclass
class RiskDecision:
    approved: bool
    requires_confirmation: bool
    reasons: List[str] = field(default_factory=list)
    checks: Dict[str, bool] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "approved": self.approved,
            "requires_confirmation": self.requires_confirmation,
            "reasons": list(self.reasons),
            "checks": dict(self.checks),
        }


# --------------------------------------------------------------------------- #
# Pure predicate helpers
# --------------------------------------------------------------------------- #

def daily_loss_fraction(start_of_day_equity: float, current_equity: float) -> float:
    """Fraction of start-of-day equity lost today (positive == a loss).

    Raises ValueError on non-positive start equity rather than silently
    returning a plausible number (Rule 2.2 — no silent stubs).
    """
    if start_of_day_equity <= 0:
        raise ValueError(
            f"start_of_day_equity must be > 0, got {start_of_day_equity!r}"
        )
    return (start_of_day_equity - current_equity) / start_of_day_equity


def daily_loss_exceeded(
    start_of_day_equity: float, current_equity: float, limit_pct: float
) -> bool:
    """True when today's loss is AT OR BEYOND the limit (trips the switch)."""
    return daily_loss_fraction(start_of_day_equity, current_equity) >= limit_pct


def _position_value_after(account: AccountState, trade: ProposedTrade) -> float:
    existing = account.positions.get(trade.symbol)
    existing_value = existing.market_value if existing else 0.0
    return existing_value + trade.signed_delta


def _sector_value_after(account: AccountState, trade: ProposedTrade) -> float:
    """Absolute sector exposure after the trade fills."""
    total = 0.0
    for sym, pos in account.positions.items():
        if pos.sector != trade.sector:
            continue
        if sym == trade.symbol:
            total += _position_value_after(account, trade)
        else:
            total += pos.market_value
    # Symbol not yet held but belongs to this sector via the trade.
    if trade.symbol not in account.positions:
        total += _position_value_after(account, trade)
    return abs(total)


# --------------------------------------------------------------------------- #
# Kill-switch (the one stateful, latching object)
# --------------------------------------------------------------------------- #

class KillSwitch:
    """Latching daily-loss kill-switch with manual re-arm.

    ``update`` evaluates the daily loss and trips (permanently, until
    ``rearm``) if the loss is at/beyond the configured limit. Once tripped it
    stays tripped even if the account later recovers intraday — that is the
    point of a manual re-arm.
    """

    def __init__(self, daily_loss_limit_pct: float) -> None:
        if not (0.0 < daily_loss_limit_pct <= 1.0):
            raise ValueError(
                f"daily_loss_limit_pct must be in (0, 1], got {daily_loss_limit_pct!r}"
            )
        self._limit = daily_loss_limit_pct
        self._tripped = False

    @property
    def tripped(self) -> bool:
        return self._tripped

    @property
    def limit_pct(self) -> float:
        return self._limit

    def update(self, start_of_day_equity: float, current_equity: float) -> bool:
        """Evaluate current equity; latch tripped if loss >= limit. Returns tripped."""
        if daily_loss_exceeded(start_of_day_equity, current_equity, self._limit):
            self._tripped = True
        return self._tripped

    def rearm(self) -> None:
        """Manual re-arm. The ONLY way to clear a tripped switch."""
        self._tripped = False


# --------------------------------------------------------------------------- #
# Top-level trade evaluation
# --------------------------------------------------------------------------- #

def evaluate_trade(
    trade: ProposedTrade,
    account: AccountState,
    config: GuardrailConfig,
    kill_switch: KillSwitch,
) -> RiskDecision:
    """Deterministically approve/reject a proposed trade.

    Order of checks: kill-switch first (a tripped switch blocks everything),
    then exposure-increasing limits, then the confirmation-threshold flag.
    """
    decision = RiskDecision(approved=True, requires_confirmation=False)

    # 1. Kill-switch — blocks all trading while tripped.
    ks_ok = not kill_switch.tripped
    decision.checks["kill_switch"] = ks_ok
    if not ks_ok:
        decision.approved = False
        decision.reasons.append("kill-switch tripped: trading halted until manual re-arm")

    if account.equity <= 0:
        raise ValueError(f"account.equity must be > 0, got {account.equity!r}")

    # 2. Max position size — only blocks EXPOSURE INCREASES.
    existing = account.positions.get(trade.symbol)
    existing_abs = abs(existing.market_value) if existing else 0.0
    new_abs = abs(_position_value_after(account, trade))
    increasing_position = new_abs > existing_abs
    pos_limit_value = config.max_position_pct * account.equity
    pos_ok = not (increasing_position and new_abs > pos_limit_value)
    decision.checks["max_position_size"] = pos_ok
    if not pos_ok:
        decision.approved = False
        decision.reasons.append(
            f"position size ${new_abs:,.2f} exceeds max "
            f"{config.max_position_pct:.0%} of ${account.equity:,.2f} "
            f"(=${pos_limit_value:,.2f})"
        )

    # 3. Max sector concentration — only blocks EXPOSURE INCREASES.
    existing_sector_abs = abs(
        sum(
            p.market_value
            for p in account.positions.values()
            if p.sector == trade.sector
        )
    )
    new_sector_abs = _sector_value_after(account, trade)
    increasing_sector = new_sector_abs > existing_sector_abs
    sector_limit_value = config.max_sector_concentration_pct * account.equity
    sector_ok = not (increasing_sector and new_sector_abs > sector_limit_value)
    decision.checks["max_sector_concentration"] = sector_ok
    if not sector_ok:
        decision.approved = False
        decision.reasons.append(
            f"sector '{trade.sector}' exposure ${new_sector_abs:,.2f} exceeds max "
            f"{config.max_sector_concentration_pct:.0%} of ${account.equity:,.2f} "
            f"(=${sector_limit_value:,.2f})"
        )

    # 4. Trade-size confirmation threshold — flags, does not reject.
    needs_confirm = trade.notional >= config.trade_confirmation_threshold_usd
    decision.checks["under_confirmation_threshold"] = not needs_confirm
    if needs_confirm:
        decision.requires_confirmation = True
        decision.reasons.append(
            f"trade notional ${trade.notional:,.2f} >= confirmation threshold "
            f"${config.trade_confirmation_threshold_usd:,.2f}: human approval required"
        )

    return decision


# --------------------------------------------------------------------------- #
# Paper-trading pass/fail gate (Phase 4 eligibility for live)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class PaperGateCriteria:
    """Objective criteria a paper-trading window must satisfy to be live-eligible."""

    min_trading_days: int
    max_kill_switch_trips: int = 0
    max_drawdown_pct: float = 1.0  # 1.0 == no drawdown constraint by default

    def __post_init__(self) -> None:
        if self.min_trading_days < 1:
            raise ValueError("min_trading_days must be >= 1")
        if self.max_kill_switch_trips < 0:
            raise ValueError("max_kill_switch_trips must be >= 0")
        if not (0.0 < self.max_drawdown_pct <= 1.0):
            raise ValueError("max_drawdown_pct must be in (0, 1]")


@dataclass(frozen=True)
class PaperGateResult:
    passed: bool
    checks: Dict[str, bool]
    reasons: List[str]


def evaluate_paper_gate(
    trading_days_completed: int,
    kill_switch_trips: int,
    observed_max_drawdown_pct: float,
    criteria: PaperGateCriteria,
) -> PaperGateResult:
    """Deterministic pass/fail gate for promoting paper -> live.

    Reports each criterion independently so an honest report can show exactly
    which one failed (Rule 2.1). Passing requires ALL criteria to pass.
    """
    if trading_days_completed < 0:
        raise ValueError("trading_days_completed must be >= 0")
    if kill_switch_trips < 0:
        raise ValueError("kill_switch_trips must be >= 0")

    checks: Dict[str, bool] = {}
    reasons: List[str] = []

    days_ok = trading_days_completed >= criteria.min_trading_days
    checks["min_trading_days"] = days_ok
    if not days_ok:
        reasons.append(
            f"only {trading_days_completed} trading days completed, "
            f"need >= {criteria.min_trading_days}"
        )

    trips_ok = kill_switch_trips <= criteria.max_kill_switch_trips
    checks["kill_switch_trips"] = trips_ok
    if not trips_ok:
        reasons.append(
            f"{kill_switch_trips} kill-switch trips exceeds max "
            f"{criteria.max_kill_switch_trips}"
        )

    dd_ok = observed_max_drawdown_pct <= criteria.max_drawdown_pct
    checks["max_drawdown"] = dd_ok
    if not dd_ok:
        reasons.append(
            f"observed max drawdown {observed_max_drawdown_pct:.1%} exceeds max "
            f"{criteria.max_drawdown_pct:.1%}"
        )

    return PaperGateResult(passed=all(checks.values()), checks=checks, reasons=reasons)
