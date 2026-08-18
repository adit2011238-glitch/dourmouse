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
    info = mt5.account_info()
    if info is None:
        return False
    # Demo servers report trade accounts on demo; the safest honest signal is
    # the server name containing 'demo' — combined with the double gate below
    # for live, this never lets a stray flag turn paper into real orders.
    server = (info.server or "").lower()
    return "demo" in server or "practice" in server


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
    positions = []
    for p in mt5mod.positions_get():
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


def mt5_order(code: str, side: str, volume: float = 0.01,
              paper_confirm: bool = False) -> dict[str, Any]:
    """Place a real order through the local MT5 terminal (paper-first).

    Refuses without ``paper_confirm=true`` AND (for live accounts) the
    MT5_ALLOW_LIVE=1 + MT5_CONFIRM_LIVE=1 double gate. Demo accounts are
    genuine paper trading with simulated fills.
    """
    try:
        mt5mod, _ = _config()
    except MT5NotConfiguredError as exc:
        return {"configured": False, "error": str(exc)}
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
    sym = mt5mod.symbol_info(name)
    request = {
        "action": mt5mod.TRADE_ACTION_DEAL,
        "symbol": name,
        "volume": volume,
        "type": mt5mod.ORDER_TYPE_BUY if side == "BUY"
        else mt5mod.ORDER_TYPE_SELL,
        "price": mt5mod.symbol_info_tick(name).ask if side == "BUY"
        else mt5mod.symbol_info_tick(name).bid,
        "deviation": 20,
        "magic": 20260801,
        "comment": "dourmouse seasonal paper",
        "type_time": mt5mod.ORDER_TIME_GTC,
        "type_filling": mt5mod.ORDER_FILLING_IOC,
    }
    result = mt5mod.order_send(request)
    if result is None:
        return {"error": f"order_send failed ({mt5mod.last_error()})"}
    if result.retcode != mt5mod.TRADE_RETCODE_DONE:
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

import subprocess  # noqa: E402
import sys  # noqa: E402
import json  # noqa: E402
from pathlib import Path  # noqa: E402

_ROSTER_ROOT = Path(__file__).resolve().parent.parent


def _run_worker(args: list[str], timeout: float = 12.0) -> dict[str, Any]:
    cmd = [sys.executable, "-m", "dourmouse.mt5_probe", *args]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            cwd=str(_ROSTER_ROOT),
        )
    except subprocess.TimeoutExpired:
        return {"configured": False, "error": "MT5 probe timed out — "
                "terminal not ready (log into a demo account)"}
    if out.returncode != 0:
        return {"configured": False,
                "error": (out.stdout or out.stderr or "worker failed").strip()}
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return {"configured": False, "error": "MT5 worker returned no JSON"}


# Panel snapshot cache: the worker probe can take up to ~12s when the
# terminal is stuck, and the webui is single-threaded — serving the probe
# inline would block every other HUD poll. Serve the last-known snapshot
# instantly and refresh in a background thread instead.
import threading  # noqa: E402
import time  # noqa: E402

_PANEL_LOCK = threading.Lock()
_PANEL_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}


def _panel_shape(data: dict[str, Any]) -> dict[str, Any]:
    """Always return the nested {status, universe} shape the HUD renderer
    expects — a flat failure dict would render as a blank fallback."""
    if "status" in data:
        return data
    return {"status": data, "universe": {"configured": False}}


def _refresh_panel() -> None:
    data = _panel_shape(_run_worker(["panel"], timeout=15.0))
    with _PANEL_LOCK:
        _PANEL_CACHE["data"] = data
        _PANEL_CACHE["ts"] = time.time()


def mt5_panel_snapshot() -> dict[str, Any]:
    """Bounded status+universe for the HUD panel (never hangs, never blocks
    the single-threaded webui: returns instantly from the cache and refreshes
    in a background thread)."""
    now = time.time()
    with _PANEL_LOCK:
        cached = _PANEL_CACHE["data"]
        fresh = cached is not None and now - _PANEL_CACHE["ts"] < 20
    if not fresh:
        threading.Thread(target=_refresh_panel, daemon=True).start()
        if cached is None:
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
    volume = float(arguments.get("volume", 0.01))
    confirm = bool(arguments.get("paper_confirm", False))
    if not code or side not in ("BUY", "SELL"):
        return "MT5 ORDER: need code (e.g. ZC) and side BUY|SELL"
    r = _run_worker(["order", code, side, str(volume),
                     "true" if confirm else "false"])
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
            "REQUIRES human confirmation before it places any order.",
            _mt5_order_tool,
            {
                "code": {"type": "string", "description": "Seasonal universe code, e.g. ZC"},
                "side": {"type": "string", "description": "BUY or SELL"},
                "volume": {"type": "number", "description": "Lots (default 0.01 micro lot)"},
                "paper_confirm": {"type": "boolean", "description": "Must be true to place any order (paper-first)"},
            },
            permission=Permission.REQUIRES_CONFIRMATION,
            confirm_prompt=lambda a: (
                f"Place MT5 order: {str(a.get('side', '?')).upper()} "
                f"{a.get('volume', '?')} lots of {a.get('code', '?')}? "
                "(paper account unless MT5_ALLOW_LIVE is set)"
            ),
        ),
    ]
