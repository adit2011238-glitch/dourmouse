"""Pre-submission risk check for broker orders (finding P4-24).

``trading212_ops.t212_order`` and ``mt5_ops.mt5_order`` (and the MT5 worker's
``mt5_probe.cmd_order``) call :func:`enforce` right before they send an order.
It builds the account snapshot from what the broker reports, asks
``guardrails.evaluate_trade`` (with ``latch=True``: this is the submission
path, so a breach is remembered) and raises :class:`OrderRefused` when the
answer is no. The caller then never contacts the broker's order endpoint.

State kept between calls and between processes (the MT5 worker is a separate
process) lives in one small JSON file in the workspace:

* the kill switch latch, cleared only by :func:`rearm`
  (``python -m dourmouse.order_guard rearm``);
* the equity first seen for each account on each local day, which is the
  start-of-day baseline for the daily-loss limit.

Honest limits, stated where they matter:

* The baseline is the equity at the first order attempt of the day, not at
  the market open, so a loss taken before that first attempt is not counted.
* Brokers report no sector here, so each symbol is its own sector and the
  sector-concentration limit degrades to the per-position limit.
* Notional is quantity x price in the instrument's own currency, with no
  currency conversion.
* A state file that cannot be read refuses every order (fail closed) rather
  than forgetting a latched switch.
"""

from __future__ import annotations

import json
import math
import sys
import threading
from datetime import date
from pathlib import Path
from typing import Any

from dourmouse import config
from dourmouse.guardrails import (
    AccountState,
    KillSwitch,
    Position,
    ProposedTrade,
    RiskDecision,
    Side,
    evaluate_trade,
)
from dourmouse.state_file import atomic_write_text

__all__ = ["OrderRefused", "check_mt5_order", "enforce", "rearm", "state_path"]

_LOCK = threading.Lock()
_KEEP_DAYS = 7


class OrderRefused(ValueError):
    """The order was not sent: a guardrail said no, or the check could not be made."""


def state_path() -> Path:
    return config.workspace_dir() / "order_guard_state.json"


def _load_state() -> dict[str, Any]:
    path = state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OrderRefused(
            f"order not placed: the guardrail state file {path} cannot be read ({exc}); "
            "refusing rather than forgetting a latched kill switch. Fix or delete it, then retry."
        ) from exc
    if not isinstance(data, dict):
        raise OrderRefused(f"order not placed: the guardrail state file {path} is not a JSON object")
    return data


def _save_state(state: dict[str, Any]) -> None:
    days = state.get("day")
    if isinstance(days, dict):
        for old in sorted(days)[:-_KEEP_DAYS]:
            days.pop(old, None)
    try:
        atomic_write_text(state_path(), json.dumps(state, indent=1, sort_keys=True) + "\n", mode=0o600)
    except OSError as exc:
        raise OrderRefused(f"order not placed: could not record the guardrail state ({exc})") from exc


def rearm() -> None:
    """Manual re-arm of the kill switch: the only way to clear a latched one."""
    with _LOCK:
        state = _load_state()
        state.pop("tripped", None)
        state.pop("tripped_reason", None)
        _save_state(state)


def enforce(
    account_key: str,
    symbol: str,
    side: Side,
    quantity: float,
    price: float,
    equity: float,
    held: dict[str, float],
) -> RiskDecision:
    """Raise :class:`OrderRefused` unless the guardrails approve this order.

    ``held`` maps symbol -> signed market value of what the account already holds."""
    try:
        cfg = config.load_guardrail_config()
        qty, px, eq = float(quantity), float(price), float(equity)
    except (TypeError, ValueError) as exc:
        raise OrderRefused(f"order not placed: could not read the guardrail inputs ({exc})") from exc
    if not all(math.isfinite(v) for v in (qty, px, eq)) or qty <= 0 or px <= 0:
        raise OrderRefused(f"order not placed: quantity {quantity!r} and price {price!r} must be positive numbers")
    if eq <= 0:
        raise OrderRefused(f"order not placed: account equity {equity!r} is not positive, so no limit can be checked")
    with _LOCK:
        state = _load_state()
        day = date.today().isoformat()
        baselines = state.setdefault("day", {}).setdefault(day, {})
        changed = account_key not in baselines
        start = float(baselines.setdefault(account_key, eq))
        switch = KillSwitch(cfg.daily_loss_limit_pct, tripped=bool(state.get("tripped")))
        account = AccountState(
            equity=eq,
            start_of_day_equity=start,
            positions={s: Position(s, float(v), s) for s, v in held.items() if v},
        )
        trade = ProposedTrade(symbol, side, qty, px, symbol)
        try:
            decision = evaluate_trade(trade, account, cfg, switch, latch=True)
        except ValueError as exc:
            raise OrderRefused(f"order not placed: {exc}") from exc
        if switch.tripped and not state.get("tripped"):
            state["tripped"] = True
            state["tripped_reason"] = f"{account_key} lost {(start - eq) / start:.1%} of its {day} start equity"
            changed = True
        if changed:
            _save_state(state)
    if not decision.approved:
        raise OrderRefused(
            "order not placed: blocked by the trading guardrails (" + "; ".join(decision.reasons) + "). "
            "Limits come from DOURMOUSE_MAX_POSITION_PCT, DOURMOUSE_MAX_SECTOR_PCT and "
            "DOURMOUSE_DAILY_LOSS_LIMIT_PCT; a latched kill switch is cleared with "
            "`python -m dourmouse.order_guard rearm`."
        )
    return decision


def check_mt5_order(mt5mod: Any, symbol: str, side: str, volume: float, price: float) -> RiskDecision:
    """Guard a MetaTrader 5 order using the connected terminal's own account data.

    ``mt5mod`` is the MetaTrader5 module (a fake in tests). Notional is
    lots x contract size x price; anything that cannot be sized refuses."""
    info = mt5mod.account_info()
    if info is None:
        raise OrderRefused("order not placed: no MT5 account to check risk limits against")

    def contract_size(name: str) -> float:
        sym = mt5mod.symbol_info(name)
        size = float(getattr(sym, "trade_contract_size", 0) or 0)
        if size <= 0:
            raise OrderRefused(f"order not placed: cannot size {name}: the terminal reports no contract size")
        return size

    held: dict[str, float] = {}
    for p in mt5mod.positions_get() or []:
        px = getattr(p, "price_current", None) or getattr(p, "price_open", None)
        if not px:
            raise OrderRefused(f"order not placed: cannot value the open {p.symbol} position (no price)")
        sign = 1.0 if p.type == 0 else -1.0
        held[p.symbol] = held.get(p.symbol, 0.0) + sign * float(p.volume) * contract_size(p.symbol) * float(px)
    lots = float(volume)
    return enforce(
        f"mt5:{getattr(info, 'login', '')}",
        symbol,
        Side.BUY if str(side).upper() == "BUY" else Side.SELL,
        lots * contract_size(symbol),
        price,
        float(info.equity),
        held,
    )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["rearm"]:
        rearm()
        print("kill switch re-armed")
        return 0
    if args == ["status"]:
        state = _load_state()
        print(json.dumps({"tripped": bool(state.get("tripped")), "reason": state.get("tripped_reason", "")}))
        return 0
    print("usage: python -m dourmouse.order_guard status|rearm")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
