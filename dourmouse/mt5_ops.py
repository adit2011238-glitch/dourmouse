"""MetaTrader 5 broker integration (paper-first) — the low-friction
paper-trading venue for the seasonal commodity universe.

Why MT5: free demo accounts come with real-time quotes and simulated fills
(no market-data subscriptions, no futures margin floors). Brokers on MT5
commonly list the ag CFDs the seasonal strategy trades (CORN, WHEAT,
SOYBEAN, SUGAR, COTTON, LIVE_CATTLE, LEAN_HOGS, GASOLINE, ...) at micro-lot
sizes a $100 account can hold — the same dated-CFD universe Trading 212
offers, but with a real Python API instead of a GUI or an equity-only API.

Honest rules (mirror trading212_ops.py):

- No MetaTrader 5 terminal installed / no account logged in -> every tool
  reports NOT CONFIGURED with the fix, never a fabricated number.
- ``MT5_ALLOW_LIVE=1`` + ``MT5_CONFIRM_LIVE=1`` double-gate live accounts;
  demo accounts (the default) are genuine paper trading.
- Every call is a real call into the local MT5 terminal; failures surface
  as errors.
- ``mt5_order`` runs the trading guardrails (``order_guard.check_mt5_order``,
  finding P4-24) right before ``order_send``: position size, sector (each
  symbol its own sector), and the daily-loss kill switch with a persisted
  latch. A breach, or an account that cannot be sized, means ``order_send``
  is never called. The tool path uses the same check inside the worker
  (``mt5_probe.cmd_order``).

Scope honesty: symbol availability is broker/server dependent — the module
queries the connected server (``mt5.symbols_get``) rather than assuming a
hard-coded list. The seasonal universe map below is the *candidate* set
with common MT5 names; ``mt5_universe`` reports exactly what the demo
server actually offers.
"""

from __future__ import annotations

import os
from typing import Any

MT5_IMPORT_ERROR: Exception | None = None
try:
    import MetaTrader5 as mt5  # noqa: PLC0415
except ImportError as ex:  # noqa: BLE001
    MT5_IMPORT_ERROR = ex

# Seasonal-universe candidates -> common MT5 symbol names (server-dependent).
# Keys are the backtest universe codes; values are ordered candidate names.
SEASONAL_SYMBOLS: dict[str, list[str]] = {
    "ZC": ["CORN", "CORN_USD", "ZC"],
    "ZW": ["WHEAT", "WHEAT_USD", "ZW"],
    "ZS": ["SOYBEAN", "SOYBEAN_USD", "ZS"],
    "ZL": ["SOYBEAN_OIL", "SOYBEANOIL", "ZL"],
    "ZR": ["RICE", "ROUGH_RICE", "ZR"],
    "HE": ["LEAN_HOGS", "LEANHOGS", "HE"],
    "LE": ["LIVE_CATTLE", "LIVECATTLE", "LE"],
    "GF": ["FEEDER_CATTLE", "FEEDERCATTLE", "GF"],
    "GC": ["GOLD", "XAUUSD", "GC"],
    "SI": ["SILVER", "XAGUSD", "SI"],
    "HG": ["COPPER", "XCUUSD", "HG"],
    "PL": ["PLATINUM", "XPTUSD", "PL"],
    "PA": ["PALLADIUM", "XPDUSD", "PA"],
    "CL": ["WTI_OIL", "CRUDE_OIL", "USOIL", "CL"],
    "BZ": ["BRENT_OIL", "UKOIL", "BRENT", "BZ"],
    "NG": ["NATGAS", "NATURAL_GAS", "NG"],
    "HO": ["HEATING_OIL", "HO"],
    "RB": ["GASOLINE", "RBOB", "RB"],
    "KC": ["COFFEE", "COFFEE_USD", "KC"],
    "SB": ["SUGAR", "SUGAR_USD", "SB"],
    "CC": ["COCOA", "COCOA_USD", "CC"],
    "CT": ["COTTON", "COTTON_USD", "CT"],
    "OJ": ["ORANGE_JUICE", "OJ"],
}

_ALLOW_LIVE_ENV = "MT5_ALLOW_LIVE"
_CONFIRM_LIVE_ENV = "MT5_CONFIRM_LIVE"
_TIMEOUT_TICKS = 40  # ~4s at 100ms sleeps


class MT5NotConfiguredError(NotImplementedError):
    pass


def _require_mt5() -> None:
    if MT5_IMPORT_ERROR is not None:
        raise MT5NotConfiguredError(
            "MetaTrader5 package missing: pip install MetaTrader5 "
            f"(import failed: {MT5_IMPORT_ERROR})"
        )


def _safe_initialize(timeout: float = 5.0) -> bool:
    """mt5.initialize() with a hard timeout.

    The MT5 terminal BLOCKS inside initialize() when it is sitting at the
    "no account" screen — unbounded it would hang every tool and freeze the
    HUD panel. Run it in a daemon thread and give up after `timeout` sec.
    """
    import threading

    result: dict[str, bool] = {}

    def _run() -> None:
        try:
            result["ok"] = mt5.initialize()
        except Exception:  # noqa: BLE001 -- defensive
            result["ok"] = False

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return False  # terminal busy / waiting for account selection
    return bool(result.get("ok"))


def _require_terminal() -> Any:
    """Initialize the local MT5 terminal; raise honestly if unavailable."""
    _require_mt5()
    if not _safe_initialize():
        raise MT5NotConfiguredError(
            "MT5 terminal is not running / no account logged in. Install the "
            "free MetaTrader 5 terminal, open a demo account (File -> Open an "
            "Account -> open a demo account), log in, then retry. "
            f"initialize() error: {mt5.last_error()}"
        )
    return mt5


def _is_demo() -> bool:
    """True only when the terminal itself says this is a DEMO account.

    ``account_info().trade_mode`` (ACCOUNT_TRADE_MODE_DEMO / CONTEST / REAL) is
    the authoritative field. A server NAME is not: a real-money account on a
    server called "...Demo..." must still go through the MT5_ALLOW_LIVE /
    MT5_CONFIRM_LIVE double gate. A missing or unrecognised trade_mode, a
    contest account, and no account at all are all treated as live."""
    info = mt5.account_info()
    if info is None:
        return False
    demo_mode = getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", 0)
    mode = getattr(info, "trade_mode", None)
    return isinstance(mode, int) and not isinstance(mode, bool) and mode == demo_mode


def _config() -> tuple[Any, dict[str, Any]]:
    """(mt5_module, account_info) or raise honestly (Rule 2.2)."""
    mt5mod = _require_terminal()
    info = mt5mod.account_info()
    if info is None:
        raise MT5NotConfiguredError(
            f"no account logged into the MT5 terminal ({mt5mod.last_error()})"
        )
    return mt5mod, info


def _find_symbol(mt5mod: Any, code: str) -> str | None:
    """First live symbol on this server matching a seasonal candidate."""
    candidates = SEASONAL_SYMBOLS.get(code, [code])
    for name in candidates:
        sym = mt5mod.symbol_info(name)
        if sym is not None and sym.visible:
            return name
    return None


def _quote(mt5mod: Any, name: str) -> dict[str, Any]:
    tick = mt5mod.symbol_info_tick(name)
    if tick is None:
        return {"error": f"no tick for {name} ({mt5mod.last_error()})"}
    bid, ask = tick.bid, tick.ask
    # nan guard — a bare truthiness check is NOT safe on prices.
    if bid is None or ask is None or bid != bid or ask != ask:
        return {"error": f"no valid bid/ask for {name} yet"}
    return {"bid": float(bid), "ask": float(ask),
            "spread": float(ask - bid), "time": tick.time}


def mt5_status() -> dict[str, Any]:
    """Config state: package, terminal, account, demo/live, universe size."""
    if MT5_IMPORT_ERROR is not None:
        return {"configured": False,
                "error": "MetaTrader5 package not installed (pip install "
                         "MetaTrader5)"}
    try:
        mt5mod, info = _config()
    except MT5NotConfiguredError as exc:
        return {"configured": False, "error": str(exc)}
    demo = _is_demo()
    return {
        "configured": True,
        "terminal": "running",
        "account": info.login,
        "server": info.server,
        "currency": info.currency,
        "demo": demo,
        "environment": "demo" if demo else "live",
        "equity": float(info.equity),
        "balance": float(info.balance),
        "margin_free": float(info.margin_free),
    }


def mt5_universe() -> dict[str, Any]:
    """Which seasonal-universe symbols this server actually lists."""
    try:
        mt5mod, _ = _config()
    except MT5NotConfiguredError as exc:
        return {"configured": False, "error": str(exc)}
    found, missing = {}, []
    for code in sorted(SEASONAL_SYMBOLS):
        name = _find_symbol(mt5mod, code)
        if name:
            found[code] = name
        else:
            missing.append(code)
    return {
        "configured": True,
        "available": found,
        "not_listed_on_this_server": missing,
    }


def mt5_account() -> dict[str, Any]:
    """Real account summary from the connected terminal."""
    try:
        mt5mod, info = _config()
    except MT5NotConfiguredError as exc:
        return {"configured": False, "error": str(exc)}
    raw = mt5mod.positions_get()
    if raw is None:  # an error, not "no positions"
        return {"configured": True, "error": f"could not read the open positions ({mt5mod.last_error()})"}
    positions = []
    for p in raw:
        positions.append({
            "symbol": p.symbol, "side": "BUY" if p.type == 0 else "SELL",
            "volume": p.volume, "open_price": p.price_open,
            "pnl": p.profit,
        })
    return {
        "configured": True,
        "balance": float(info.balance),
        "equity": float(info.equity),
        "margin_free": float(info.margin_free),
        "open_positions": positions,
    }


def mt5_quote(code: str) -> dict[str, Any]:
    """Live bid/ask for one seasonal code (e.g. 'ZC' -> CORN on the server)."""
    try:
        mt5mod, _ = _config()
    except MT5NotConfiguredError as exc:
        return {"configured": False, "error": str(exc)}
    name = _find_symbol(mt5mod, code)
    if name is None:
        return {"configured": True, "code": code, "error": "not listed on "
                "this server"}
    q = _quote(mt5mod, name)
    return {"configured": True, "code": code, "symbol": name, **q}


def _ticket(position: Any) -> int | None:
    """A model-supplied position ticket as an int (None stays None); raises ValueError otherwise."""
    if position is None or position == "":
        return None
    if isinstance(position, bool):
        raise ValueError(f"position must be a ticket number, got {position!r}")
    try:
        ticket = int(str(position).strip())
    except ValueError:
        raise ValueError(f"position must be a ticket number, got {position!r}") from None
    if ticket <= 0:
        raise ValueError(f"position must be a ticket number, got {position!r}")
    return ticket


def _tick_price(mt5mod: Any, name: str, side: str) -> float | None:
    """The one price this order is both checked and sent at: ask for BUY, bid for SELL."""
    tick = mt5mod.symbol_info_tick(name)
    if tick is None:
        return None
    price = tick.ask if side == "BUY" else tick.bid
    if not isinstance(price, (int, float)) or isinstance(price, bool) or price != price or price <= 0:
        return None
    try:
        return float(price)
    except OverflowError:  # an integer too big for a float
        return None


def mt5_order(code: str, side: str, volume: float = 0.01,
              paper_confirm: bool = False, position: Any = None) -> dict[str, Any]:
    """Place a real order through the local MT5 terminal (paper-first).

    Refuses without ``paper_confirm=true`` AND (for live accounts) the
    MT5_ALLOW_LIVE=1 + MT5_CONFIRM_LIVE=1 double gate. Demo accounts are
    genuine paper trading with simulated fills. The trading guardrails
    (``order_guard.check_mt5_order``) run before ``order_send``; a breach
    returns an error and nothing is sent. ``position`` (a ticket) closes or
    trims that one position; on a hedging account that is the only way an
    order reduces exposure (W3R-4).
    """
    try:
        mt5mod, _ = _config()
    except MT5NotConfiguredError as exc:
        return {"configured": False, "error": str(exc)}
    if paper_confirm is not True:
        return {"error": "paper confirmation required: pass paper_confirm="
                "true to place an order (paper-first, Rule 2.1)"}
    code = str(code).strip().upper()
    side = str(side).strip().upper()
    if side not in ("BUY", "SELL"):
        return {"error": f"side must be BUY or SELL, got {side!r}"}
    if isinstance(volume, bool):
        return {"error": f"volume must be a number, got {volume!r}"}
    try:
        volume = float(volume)
    except (TypeError, ValueError, OverflowError):  # OverflowError: an integer too big for a float
        return {"error": f"volume must be a number, got {volume!r}"}
    if volume != volume or volume <= 0 or volume == float("inf"):
        return {"error": f"volume must be a positive number, got {volume}"}
    try:
        ticket = _ticket(position)
    except ValueError as exc:
        return {"error": str(exc)}
    if not _is_demo():
        if os.environ.get(_ALLOW_LIVE_ENV, "").strip() != "1":
            return {"error": f"live account detected: set {_ALLOW_LIVE_ENV}=1 "
                    "to enable real-money orders (demo is the paper default)"}
        if os.environ.get(_CONFIRM_LIVE_ENV, "").strip() != "1":
            return {"error": f"live order blocked: set {_CONFIRM_LIVE_ENV}=1 "
                    "as the final explicit confirmation"}
    name = _find_symbol(mt5mod, code)
    if name is None:
        return {"error": f"{code} not listed on this server — cannot order"}
    # R4-5: one order at a time. The check and the send share one critical section, so a second order
    # cannot be approved against the positions this one is about to change.
    return _guarded_send(mt5mod, name, code, side, volume, ticket, magic=20260801)


#: order_send retcodes that do NOT say the order was refused: placed (10008), partly filled (10010),
#: request timed out (10012), no connection (10031). The order may be live.
_MT5_UNKNOWN_RETCODES = frozenset({10008, 10010, 10012, 10031})


def order_status_unknown(why: object) -> dict[str, Any]:
    """The reply for an order that may or may not have been executed (R4-6, like T212's W2R-5)."""
    return {
        "error": f"order status UNKNOWN: {why}. The order may have been executed. Check the open positions and "
                 "pending orders in the MT5 terminal (mt5_status shows the account) before retrying; do not "
                 "place the order again until you have.",
        "status_unknown": True,
    }


def _guarded_send(mt5mod: Any, name: str, code: str, side: str, volume: float, ticket: int | None,
                  *, magic: int) -> dict[str, Any]:
    """Check an order against the guardrails and send it, holding the order lock through the answer."""
    from dourmouse import order_guard

    try:
        with order_guard.order_section():
            # W3R-5: one tick, read once; the guard and the request use the same price.
            price = _tick_price(mt5mod, name, side)
            if price is None:
                return {"error": f"no valid price for {name} yet ({mt5mod.last_error()})"}
            order_guard.check_mt5_order(mt5mod, name, side, volume, price, position=ticket)
            # Engineering audit, 2026-09-17: this used to fetch mt5mod.symbol_info(name)
            # here and never use it (dead code -- volume_min/volume_max/volume_step/
            # digits were never checked). Client-side validation against those
            # constraints, and price rounding to the symbol's real tick size, is a
            # real, deferred improvement, NOT added here: getting MT5's own rounding
            # semantics wrong for live order placement is worse than relying on the
            # broker's own honest rejection (handled below via result.retcode) until
            # someone can verify the exact behavior against a real MT5 server.
            request = {
                "action": mt5mod.TRADE_ACTION_DEAL,
                "symbol": name,
                "volume": volume,
                "type": mt5mod.ORDER_TYPE_BUY if side == "BUY"
                else mt5mod.ORDER_TYPE_SELL,
                "price": price,
                "deviation": 20,
                "magic": magic,
                "comment": "dourmouse seasonal paper",
                "type_time": mt5mod.ORDER_TIME_GTC,
                "type_filling": mt5mod.ORDER_FILLING_IOC,
            }
            if ticket is not None:
                request["position"] = ticket
            try:
                result = mt5mod.order_send(request)
            except Exception as exc:  # noqa: BLE001 -- anything after the request left is "unknown", not "refused"
                return order_status_unknown(f"order_send raised {exc!r}")
    except order_guard.OrderRefused as exc:
        return {"error": str(exc)}
    if result is None:
        return order_status_unknown(f"order_send returned nothing ({mt5mod.last_error()})")
    if result.retcode != mt5mod.TRADE_RETCODE_DONE:
        if result.retcode in _MT5_UNKNOWN_RETCODES:
            return order_status_unknown(f"retcode {result.retcode} ({result.comment})")
        return {"error": f"order rejected: retcode {result.retcode} "
                f"({result.comment})"}
    return {
        "filled": True, "code": code, "symbol": name, "side": side,
        "volume": volume, "order": result.order, "deal": result.deal,
        "price": result.price,
    }


# --------------------------------------------------------------------------- #
# Tool wrappers (roster-friendly, string-returning)
#
# These go through the SUBPROCESS-isolated worker (mt5_probe.py) with a hard
# timeout: the MT5 DLL can block forever while holding the GIL when the
# terminal sits at the no-account screen, so in-process calls would freeze
# the roster / HUD. The worker is killed on timeout; honesty preserved.

import json  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

_ROSTER_ROOT = Path(__file__).resolve().parent.parent


#: The order worker may wait up to ORDER_LOCK_WAIT_SECONDS for another order, then talks to the terminal.
_ORDER_WORKER_TIMEOUT = 60.0


def _run_worker(args: list[str], timeout: float = 12.0) -> dict[str, Any]:
    # An "order" worker that times out, dies or prints no JSON may have sent the order before it did:
    # that is "status UNKNOWN", never a refusal (R4-6). Every other command only reads.
    is_order = bool(args) and args[0] == "order"
    cmd = [sys.executable, "-m", "dourmouse.mt5_probe", *args]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_ORDER_WORKER_TIMEOUT if is_order else timeout, cwd=str(_ROSTER_ROOT),
        )
    except subprocess.TimeoutExpired:
        if is_order:
            return order_status_unknown("the MT5 worker did not answer in time and was stopped")
        return {"configured": False, "error": "MT5 probe timed out — "
                "terminal not ready (log into a demo account)"}
    if out.returncode != 0:
        detail = (out.stdout or out.stderr or "worker failed").strip()
        if is_order:
            return order_status_unknown(f"the MT5 worker failed ({detail[:300]})")
        return {"configured": False, "error": detail}
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        if is_order:
            return order_status_unknown("the MT5 worker returned no readable answer")
        return {"configured": False, "error": "MT5 worker returned no JSON"}


# Panel snapshot cache: the worker probe can take up to ~12s when the
# terminal is stuck, and the webui is single-threaded — serving the probe
# inline would block every other HUD poll. Serve the last-known snapshot
# instantly and refresh in a background thread instead.
import threading  # noqa: E402
import time  # noqa: E402

_PANEL_LOCK = threading.Lock()
_PANEL_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
#: True while a refresh thread is running, so a stuck terminal and a fast HUD
#: poll cannot pile up concurrent probe subprocesses against the same terminal.
_PANEL_REFRESHING = False


def _panel_shape(data: dict[str, Any]) -> dict[str, Any]:
    """Always return the nested {status, universe} shape the HUD renderer
    expects — a flat failure dict would render as a blank fallback."""
    if "status" in data:
        return data
    return {"status": data, "universe": {"configured": False}}


def _refresh_panel() -> None:
    global _PANEL_REFRESHING
    try:
        data = _panel_shape(_run_worker(["panel"], timeout=15.0))
        with _PANEL_LOCK:
            _PANEL_CACHE["data"] = data
            _PANEL_CACHE["ts"] = time.time()
    finally:
        with _PANEL_LOCK:
            _PANEL_REFRESHING = False


def mt5_panel_snapshot() -> dict[str, Any]:
    """Bounded status+universe for the HUD panel (never hangs, never blocks
    the single-threaded webui: returns instantly from the cache and refreshes
    in a background thread)."""
    global _PANEL_REFRESHING
    now = time.time()
    with _PANEL_LOCK:
        cached = _PANEL_CACHE["data"]
        fresh = cached is not None and now - _PANEL_CACHE["ts"] < 20
        start = not fresh and not _PANEL_REFRESHING
        if start:
            _PANEL_REFRESHING = True
    if start:
        threading.Thread(target=_refresh_panel, daemon=True).start()
    if not fresh and cached is None:
        return _panel_shape({"configured": False,
                             "error": "MT5 probe pending — check "
                                      "again shortly"})
    return cached


def _mt5_status_tool(arguments: dict[str, Any]) -> str:
    s = _run_worker(["status"])
    if not s.get("configured"):
        return f"MT5 PAPER: NOT CONFIGURED — {s.get('error', 'unknown')}"
    acct = s.get("account", {})
    return (f"MT5 PAPER: {s['environment'].upper()} acct {acct.get('login')} "
            f"on {acct.get('server')} · equity {acct.get('equity')} "
            f"{acct.get('currency')} · free margin {acct.get('margin_free')}")


def _mt5_universe_tool(arguments: dict[str, Any]) -> str:
    s = _run_worker(["universe"])
    if not s.get("configured"):
        return f"MT5 UNIVERSE: NOT CONFIGURED — {s.get('error', 'unknown')}"
    avail = ", ".join(f"{c}={n}" for c, n in s["available"].items())
    miss = ", ".join(s["not_listed"]) or "none"
    return (f"MT5 UNIVERSE: available [{avail}] · not listed on this "
            f"server [{miss}]")


def _mt5_quote_tool(arguments: dict[str, Any]) -> str:
    code = str(arguments.get("code", "")).strip().upper()
    if not code:
        return "MT5 QUOTE: need a code (e.g. ZC, HE, GC)"
    q = _run_worker(["quote", code])
    if not q.get("configured"):
        return f"MT5 QUOTE {code}: NOT CONFIGURED — {q.get('error', 'unknown')}"
    if "error" in q:
        return f"MT5 QUOTE {code}: {q['error']}"
    return (f"MT5 QUOTE {code} ({q['symbol']}): bid={q['bid']} "
            f"ask={q['ask']} spread={q['spread']}")


def _mt5_order_tool(arguments: dict[str, Any]) -> str:
    code = str(arguments.get("code", "")).strip().upper()
    side = str(arguments.get("side", "")).strip().upper()
    raw_volume = arguments.get("volume", 0.01)
    try:
        if isinstance(raw_volume, bool):
            raise ValueError
        volume = float(raw_volume)
    except (TypeError, ValueError, OverflowError):  # OverflowError: an integer too big for a float
        return f"MT5 ORDER: volume must be a number, got {raw_volume!r}"
    # Literally True: a model-supplied "false" string is truthy and must not confirm.
    confirm = arguments.get("paper_confirm") is True
    try:
        ticket = _ticket(arguments.get("position"))
    except ValueError as exc:
        return f"MT5 ORDER: {exc}"
    if not code or side not in ("BUY", "SELL"):
        return "MT5 ORDER: need code (e.g. ZC) and side BUY|SELL"
    r = _run_worker(["order", code, side, repr(volume),
                     "true" if confirm else "false"] + ([str(ticket)] if ticket is not None else []))
    if r.get("status_unknown"):
        return f"MT5 ORDER {code} {side}: STATUS UNKNOWN, NOT A REFUSAL — {r['error']}"
    if "error" in r:
        return f"MT5 ORDER {code} {side}: REFUSED — {r['error']}"
    return (f"MT5 ORDER FILLED: {r['side']} {r['volume']} {r['code']} "
            f"({r['symbol']}) @ {r['price']} deal {r['deal']}")


def build_mt5_tool_specs() -> list[Any]:
    """ToolSpecs for the ``mt5`` subagent (lazy ToolSpec import)."""
    from dourmouse.dispatch import Permission, ToolSpec

    def _spec(
        name: str,
        description: str,
        handler,
        props: dict[str, Any],
        *,
        permission: Any = Permission.REGULAR,
        confirm_prompt: Any = None,
    ) -> Any:
        return ToolSpec(
            name=name,
            description=description,
            parameters={"type": "object", "properties": props, "required": []},
            handler=handler,
            permission=permission,
            confirm_prompt=confirm_prompt,
        )

    return [
        _spec(
            "mt5_status",
            "MetaTrader 5 paper status: terminal running?, account, demo/live, "
            "equity — or the honest failure reason.",
            _mt5_status_tool,
            {},
        ),
        _spec(
            "mt5_universe",
            "Which seasonal-universe symbols (ZC, HE, GC, ...) this MT5 server "
            "actually lists, and which it does not.",
            _mt5_universe_tool,
            {},
        ),
        _spec(
            "mt5_quote",
            "Live bid/ask/spread for one seasonal code (e.g. ZC -> CORN).",
            _mt5_quote_tool,
            {"code": {"type": "string", "description": "Seasonal universe code, e.g. ZC, HE, GC"}},
        ),
        # v8.15: gated on top of paper_confirm/the live env double-gate —
        # trade execution, even simulated, deserves the same human check as
        # this system's other consequential actions.
        _spec(
            "mt5_order",
            "Place a REAL order through the local MT5 terminal. Paper-first: "
            "refuses without paper_confirm=true, and live accounts are double-"
            "gated (MT5_ALLOW_LIVE=1 + MT5_CONFIRM_LIVE=1). Demo = paper. "
            "The trading guardrails (position size, daily-loss kill switch) "
            "are checked first and a breach refuses the order; closing or "
            "trimming an open position (pass its ticket as position) stays "
            "allowed. "
            "REQUIRES human confirmation before it places any order.",
            _mt5_order_tool,
            {
                "code": {"type": "string", "description": "Seasonal universe code, e.g. ZC"},
                "side": {"type": "string", "description": "BUY or SELL"},
                "volume": {"type": "number", "description": "Lots (default 0.01 micro lot)"},
                "position": {"type": "integer", "description": "Optional ticket of an open position to close or trim "
                             "(opposite side, at most its volume); on a hedging account a SELL without it opens a new short"},
                "paper_confirm": {"type": "boolean", "description": "Must be true to place any order (paper-first)"},
            },
            permission=Permission.REQUIRES_CONFIRMATION,
            confirm_prompt=lambda a: (
                f"Place MT5 order: {str(a.get('side', '?')).upper()} "
                f"{a.get('volume', '?')} lots of {a.get('code', '?')}"
                f"{' closing position ' + str(a['position']) if a.get('position') else ''}? "
                "(paper account unless MT5_ALLOW_LIVE is set)"
            ),
        ),
    ]
