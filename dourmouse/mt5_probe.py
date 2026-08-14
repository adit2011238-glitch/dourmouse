"""mt5_probe.py — subprocess-isolated MT5 worker.

The MetaTrader5 DLL's ``initialize()`` can BLOCK the whole process while
holding the GIL when the terminal sits at the "no account" screen — even a
daemon thread cannot be interrupted, so the caller (HUD panel poll, roster
tool) would freeze forever.

Solution: the caller runs THIS module as a subprocess with a hard timeout
and kills it if it hangs. Whatever the worker prints on stdout is JSON;
anything else means the terminal was not ready.

Usage (all print a single JSON object to stdout):
  python -m dourmouse.mt5_probe panel        # status + universe in one call
  python -m dourmouse.mt5_probe status
  python -m dourmouse.mt5_probe universe
  python -m dourmouse.mt5_probe quote CODE
  python -m dourmouse.mt5_probe order CODE SIDE VOLUME PAPER_CONFIRM
"""

from __future__ import annotations

import json
import sys
from typing import Any

import MetaTrader5 as mt5  # noqa: PLC0415

from dourmouse.mt5_ops import (
    SEASONAL_SYMBOLS,
    _find_symbol,
    _is_demo,
    _quote,
)


def _emit(obj: dict[str, Any]) -> None:
    print(json.dumps(obj, default=str))
    try:
        mt5.shutdown()
    except Exception:  # noqa: BLE001 -- best effort
        pass


def _account() -> dict[str, Any] | None:
    info = mt5.account_info()
    if info is None:
        return None
    return {
        "login": info.login, "server": info.server, "currency": info.currency,
        "equity": float(info.equity), "balance": float(info.balance),
        "margin_free": float(info.margin_free),
    }


def cmd_panel() -> dict[str, Any]:
    status: dict[str, Any] = {"configured": False}
    universe: dict[str, Any] = {"configured": False}
    if not mt5.initialize():
        status = {"configured": False,
                  "error": "MT5 terminal not ready — install the terminal and "
                           "log into a demo account"}
    else:
        acct = _account()
        demo = _is_demo() if acct else False
        status = {"configured": acct is not None, "account": acct,
                  "demo": demo,
                  "environment": "demo" if demo else "live"}
        if acct:
            found, missing = {}, []
            for code in sorted(SEASONAL_SYMBOLS):
                name = _find_symbol(mt5, code)
                (found if name else missing).append(
                    f"{code}={name}" if name else code)
            universe = {"configured": True, "available": found,
                        "not_listed": missing}
    return {"status": status, "universe": universe}


def cmd_status() -> dict[str, Any]:
    if not mt5.initialize():
        return {"configured": False,
                "error": "MT5 terminal not ready — install the terminal and "
                         "log into a demo account"}
    acct = _account()
    if acct is None:
        return {"configured": False,
                "error": "MT5 terminal running but NO account logged in — "
                         "open a demo account in the terminal first"}
    demo = _is_demo()
    return {"configured": True, "account": acct, "demo": demo,
            "environment": "demo" if demo else "live"}


def cmd_universe() -> dict[str, Any]:
    if not mt5.initialize():
        return {"configured": False,
                "error": "MT5 terminal not ready — install the terminal and "
                         "log into a demo account"}
    found, missing = {}, []
    for code in sorted(SEASONAL_SYMBOLS):
        name = _find_symbol(mt5, code)
        if name:
            found[code] = name
        else:
            missing.append(code)
    return {"configured": True, "available": found, "not_listed": missing}


def cmd_quote(code: str) -> dict[str, Any]:
    if not mt5.initialize():
        return {"configured": False,
                "error": "MT5 terminal not ready — log into a demo account"}
    name = _find_symbol(mt5, code)
    if name is None:
        return {"configured": True, "code": code,
                "error": "not listed on this server"}
    q = _quote(mt5, name)
    return {"configured": True, "code": code, "symbol": name, **q}


def cmd_order(code: str, side: str, volume: float,
              paper_confirm: bool) -> dict[str, Any]:
    from dourmouse.mt5_ops import MT5NotConfiguredError

    if not paper_confirm:
        return {"error": "paper confirmation required: pass paper_confirm="
                "true to place an order (paper-first, Rule 2.1)"}
    side = side.upper()
    if side not in ("BUY", "SELL"):
        return {"error": f"side must be BUY or SELL, got {side!r}"}
    try:
        volume = float(volume)
    except (TypeError, ValueError):
        return {"error": f"volume must be a number, got {volume!r}"}
    if volume <= 0:
        return {"error": f"volume must be positive, got {volume}"}
    if not mt5.initialize():
        return {"error": "MT5 terminal not ready — log into a demo account"}
    if not _is_demo():
        import os
        if os.environ.get("MT5_ALLOW_LIVE", "").strip() != "1":
            return {"error": "live account detected: set MT5_ALLOW_LIVE=1 to "
                    "enable real-money orders (demo is the paper default)"}
        if os.environ.get("MT5_CONFIRM_LIVE", "").strip() != "1":
            return {"error": "live order blocked: set MT5_CONFIRM_LIVE=1 as "
                    "the final explicit confirmation"}
    name = _find_symbol(mt5, code)
    if name is None:
        return {"error": f"{code} not listed on this server — cannot order"}
    tick = mt5.symbol_info_tick(name)
    price = tick.ask if side == "BUY" else tick.bid
    if price is None or price != price:
        return {"error": f"no valid price for {name} yet"}
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": name,
        "volume": volume,
        "type": mt5.ORDER_TYPE_BUY if side == "BUY"
        else mt5.ORDER_TYPE_SELL,
        "price": price,
        "deviation": 20,
        "magic": 20260802,
        "comment": "dourmouse seasonal paper",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    if result is None:
        return {"error": f"order_send failed ({mt5.last_error()})"}
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return {"error": f"order rejected: retcode {result.retcode} "
                f"({result.comment})"}
    return {"filled": True, "code": code, "symbol": name, "side": side,
            "volume": volume, "order": result.order, "deal": result.deal,
            "price": result.price}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        _emit({"error": "no command"})
        return 1
    cmd, rest = args[0], args[1:]
    try:
        if cmd == "panel":
            _emit(cmd_panel())
        elif cmd == "status":
            _emit(cmd_status())
        elif cmd == "universe":
            _emit(cmd_universe())
        elif cmd == "quote" and rest:
            _emit(cmd_quote(rest[0].upper()))
        elif cmd == "order" and len(rest) >= 3:
            _emit(cmd_order(rest[0].upper(), rest[1].upper(),
                            float(rest[2]), rest[3].lower() == "true"
                            if len(rest) > 3 else False))
        else:
            _emit({"error": f"bad command: {args!r}"})
            return 1
    except Exception as exc:  # noqa: BLE001 -- worker must never hang or lie
        _emit({"error": f"worker failed (reported honestly): {exc}"})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
