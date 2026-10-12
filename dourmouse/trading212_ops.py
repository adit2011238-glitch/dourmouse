"""Trading 212 broker integration (v8.2) — real account + order access.

Uses Trading 212's official Public API (beta) — the same API their web
app is built on. Real HTTP against ``demo.trading212.com`` (practice /
paper) or ``live.trading212.com`` (real money), authenticated with the
``X-Api-Token`` header from an API key generated in the T212 app
(Settings → API (Beta); switch to Practice first for a demo key).

Honest rules (2.1 / 2.2 / 2.8):

- No ``T212_API_KEY`` in .env -> every tool reports NOT CONFIGURED.
- ``T212_ENV`` chooses demo (default) vs live; live is only honoured when
  ``T212_ALLOW_LIVE=1`` is ALSO set — a deliberate double gate so a stray
  env value can never turn the paper integration into real orders.
- Every HTTP call is a real request; failures surface as errors, never
  as fabricated positions.
- ``t212_order`` is paper-first by construction: it refuses (a) without
  an explicit ``paper_confirm=true`` argument AND (b) on live unless the
  double gate above is open. It is the one tool that WRITES.
- ``t212_order`` also runs the trading guardrails (``order_guard.enforce``,
  finding P4-24) right before it sends: position size, sector (each ticker
  its own sector here), daily-loss kill switch with a persisted latch. A
  breach, or an account that cannot be read, means NO request reaches the
  order endpoint. An order that only reduces a position the broker reports
  (a sell of at most what is held) still goes through when the kill switch
  is tripped or other account data is missing (W3R-2). The check is sized
  with the highest price the order could fill at: the held position's
  current price and the order's own limit / stop price; a MARKET order
  takes no price of its own (W3R-3), so a MARKET order for a ticker you do
  not hold has no known price and is refused: use a LIMIT order.

Scope honesty: the official API covers Invest/ISA *equity* accounts
(stocks/ETFs) — it does NOT expose CFDs, so the commodity-seasonal CFD
strategy cannot be executed through it. That path stays manual (T212 app)
or via IBKR when the gateway is up. This module manages the portfolio
and paper-orders the equity side; the CFD legs are logged separately in
the forex paper log.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

_API_KEY_ENV = "T212_API_KEY"
_API_KEY_DEMO_ENV = "T212_API_KEY_DEMO"
_API_KEY_LIVE_ENV = "T212_API_KEY_LIVE"
_ENV_ENV = "T212_ENV"  # demo | live
_ALLOW_LIVE_ENV = "T212_ALLOW_LIVE"
_BASE_DEMO = "https://demo.trading212.com"
_BASE_LIVE = "https://live.trading212.com"
_TIMEOUT = 15


class T212NotConfiguredError(NotImplementedError):
    pass


def _config() -> tuple[str, str]:
    """(base_url, api_key) or raise honestly (Rule 2.2).

    Key resolution (per-environment keys let demo and live coexist in
    .env at the same time):

    - demo:  ``T212_API_KEY_DEMO`` if set, else ``T212_API_KEY``
    - live:  ``T212_API_KEY_LIVE`` if set, else ``T212_API_KEY``

    ``T212_ENV`` chooses which environment the tools hit. Live is only
    honoured when ``T212_ALLOW_LIVE=1`` is ALSO set (double gate).
    """
    env = (os.environ.get(_ENV_ENV, "demo").strip().lower() or "demo")
    if env not in ("demo", "live"):
        raise T212NotConfiguredError(
            f"{_ENV_ENV} must be 'demo' or 'live', got {env!r}"
        )
    fallback = os.environ.get(_API_KEY_ENV, "").strip()
    if env == "live":
        key = os.environ.get(_API_KEY_LIVE_ENV, "").strip() or fallback
        missing = _API_KEY_LIVE_ENV
    else:
        key = os.environ.get(_API_KEY_DEMO_ENV, "").strip() or fallback
        missing = _API_KEY_DEMO_ENV
    if not key:
        raise T212NotConfiguredError(
            f"{missing} (or {_API_KEY_ENV}) is not set. Generate an API key "
            "in the T212 app (Settings → API (Beta); switch to Practice "
            "first for a demo key) and add it to .env to enable broker access."
        )
    if env == "live" and os.environ.get(_ALLOW_LIVE_ENV, "").strip() != "1":
        raise T212NotConfiguredError(
            f"live trading is gated: set {_ALLOW_LIVE_ENV}=1 AND {_ENV_ENV}=live "
            "to enable real-money orders. The default (demo) is paper-only."
        )
    return (_BASE_LIVE if env == "live" else _BASE_DEMO), key


def _order_status_unknown(path: str, why: object) -> RuntimeError:
    # A POST may have reached T212 before the connection failed, the read timed
    # out, a gateway answered 5xx, or the answer could not be read. Reporting a
    # plain failure invites a retry that places a second order, so say the
    # outcome is unknown.
    return RuntimeError(
        f"order status UNKNOWN: the request to {path} did not complete cleanly ({why}) but T212 "
        "may have executed it. Check t212_positions / t212_portfolio (open orders) "
        "before retrying; do not place the order again until you have."
    )


def _request(base: str, key: str, path: str, payload: dict | None = None) -> Any:
    """One real T212 API call; returns parsed JSON or raises honestly."""
    url = base + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "X-Api-Token": key,
            "Content-Type": "application/json" if data else "application/json",
        },
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = " · " + exc.read().decode()[:200]
        except Exception:  # noqa: BLE001 -- best-effort detail
            pass
        if payload is not None and (exc.code == 408 or exc.code >= 500):
            # A timeout or server/gateway error on the order POST says nothing about whether the
            # order was filled (W2R-5).
            raise _order_status_unknown(path, f"HTTP {exc.code} {exc.reason}{detail}") from exc
        raise RuntimeError(f"T212 API {exc.code} on {path}: {exc.reason}{detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        reason = getattr(exc, "reason", None) or exc
        if payload is not None:
            raise _order_status_unknown(path, reason) from exc
        if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError):
            raise RuntimeError(f"T212 API timed out ({path})") from exc
        raise RuntimeError(f"T212 API unreachable ({path}): {reason}") from exc
    if payload is not None and not raw.strip():
        # A 2xx with no body: the order endpoint always answers with the order.
        raise _order_status_unknown(path, "empty response body")
    try:
        return json.loads(raw) if raw.strip() else None
    except ValueError as exc:
        if payload is not None:
            raise _order_status_unknown(path, "response was not valid JSON") from exc
        raise RuntimeError(f"T212 API returned a response that is not JSON ({path})") from exc


def t212_status() -> dict[str, Any]:
    """Config state: key present, demo/live, and a real account probe."""
    try:
        base, key = _config()
    except T212NotConfiguredError as exc:
        return {"configured": False, "error": str(exc)}
    try:
        info = _request(base, key, "/api/v0/equity/account/info")
        return {
            "configured": True,
            "environment": "live" if base == _BASE_LIVE else "demo",
            "account": info,
        }
    except Exception as exc:  # noqa: BLE001 -- honest failure surface
        return {"configured": True, "environment": "demo" if base == _BASE_DEMO else "live",
                "error": f"probe failed (reported honestly): {exc}"}


def t212_account() -> dict[str, Any]:
    """Real account summary: equity, cash (free/blocked/invested), P&L."""
    base, key = _config()
    return _request(base, key, "/api/v0/equity/account/info")


def t212_positions() -> dict[str, Any]:
    """Real open positions with live P&L."""
    base, key = _config()
    return _request(base, key, "/api/v0/equity/positions")


def t212_portfolio() -> dict[str, Any]:
    """Composed real view: account summary + open positions + cash."""
    base, key = _config()
    return {
        "account": _request(base, key, "/api/v0/equity/account/info"),
        "positions": _request(base, key, "/api/v0/equity/positions"),
        "orders": _request(base, key, "/api/v0/equity/orders"),
    }


def _number(obj: Any, names: tuple[str, ...]) -> float | None:
    """First of ``names`` found in the dict ``obj`` as a finite number."""
    if isinstance(obj, dict):
        for name in names:
            v = obj.get(name)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf"):
                return float(v)
    return None


_ORDER_TYPES = frozenset({"MARKET", "LIMIT", "STOP", "STOP_LIMIT"})
#: Which price fields each order type takes; anything else is refused rather than sent.
_PRICE_FIELDS = {
    "MARKET": (False, False),
    "LIMIT": (True, False),
    "STOP": (False, True),
    "STOP_LIMIT": (True, True),
}
_TICKER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def _account_state(base: str, key: str) -> tuple[float | None, dict[str, tuple[float, float | None]], list[str]]:
    """(account equity or None, {ticker: (quantity, current price or None)}, problems) from the broker.

    The key names tried are the ones the T212 account and positions payloads use. A positions
    response that cannot be read at all raises (no order can then be judged, not even a close);
    anything narrower (no equity, one position without a price) is returned as a problem, so a
    pure reduction of a position the broker does report can still go ahead (W3R-2) while every
    other order is refused."""
    problems: list[str] = []
    equity: float | None = None
    try:
        info = _request(base, key, "/api/v0/equity/account/info")
        equity = _number(info, ("totalValue", "total", "equity", "accountValue"))
        if equity is None:
            cash = _request(base, key, "/api/v0/equity/account/cash")
            equity = _number(cash, ("total", "totalValue"))
        if equity is None:
            problems.append("no account total (totalValue / total / equity) in the T212 account response")
    except RuntimeError as exc:
        problems.append(f"the account total could not be read ({exc})")
    raw = _request(base, key, "/api/v0/equity/positions")
    if isinstance(raw, dict):
        raw = raw.get("items", raw.get("positions"))
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise RuntimeError("the T212 positions response is not a list")
    held: dict[str, tuple[float, float | None]] = {}
    for item in raw:
        if not isinstance(item, dict):
            problems.append("a T212 position entry is not an object")
            continue
        nested = item.get("instrument")
        instrument: dict[str, Any] = nested if isinstance(nested, dict) else {}
        ticker = str(item.get("ticker") or instrument.get("ticker") or "").strip()
        qty = _number(item, ("quantity",))
        price = _number(item, ("currentPrice",))
        if not ticker or qty is None:
            problems.append(f"cannot read the position {ticker or item!r} (ticker or quantity missing)")
            continue
        if price is None:
            problems.append(f"cannot value the position {ticker} (currentPrice missing)")
        old_qty, old_price = held.get(ticker, (0.0, None))
        held[ticker] = (old_qty + qty, price if price is not None else old_price)
    return equity, held, problems


def _order_number(value: Any, name: str, *, positive: bool) -> float | None:
    """A model-supplied number as a finite float (None stays None); strings such as "5" are read,
    booleans, NaN, infinities and anything else are refused."""
    from dourmouse import order_guard

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise order_guard.OrderRefused(f"order not placed: {name} must be a number, got {value!r}")
    try:
        number = float(value)
    except ValueError:
        raise order_guard.OrderRefused(f"order not placed: {name} must be a number, got {value!r}") from None
    if number != number or abs(number) == float("inf"):
        raise order_guard.OrderRefused(f"order not placed: {name} must be a finite number, got {value!r}")
    if positive and number <= 0:
        raise order_guard.OrderRefused(f"order not placed: {name} must be positive, got {value!r}")
    if not positive and number == 0:
        raise order_guard.OrderRefused(f"order not placed: {name} must be non-zero, got {value!r}")
    return number


def _normalise_order(
    ticker: Any, quantity: Any, order_type: Any, limit_price: Any, stop_price: Any,
) -> dict[str, Any]:
    """Validate the model's order fields once; what is checked is exactly what is sent (W3R-3)."""
    from dourmouse import order_guard

    name = ticker.strip() if isinstance(ticker, str) else ""
    if not _TICKER_RE.fullmatch(name):
        raise order_guard.OrderRefused(f"order not placed: {ticker!r} is not a ticker (letters, digits, . _ -)")
    kind = order_type.strip().upper() if isinstance(order_type, str) else ""
    if kind not in _ORDER_TYPES:
        raise order_guard.OrderRefused(
            f"order not placed: order_type must be one of {', '.join(sorted(_ORDER_TYPES))}, got {order_type!r}"
        )
    qty = _order_number(quantity, "quantity", positive=False)
    if qty is None:
        raise order_guard.OrderRefused("order not placed: quantity is required (negative to sell)")
    limit = _order_number(limit_price, "limit_price", positive=True)
    stop = _order_number(stop_price, "stop_price", positive=True)
    wants_limit, wants_stop = _PRICE_FIELDS[kind]
    for given, wanted, field in ((limit, wants_limit, "limit_price"), (stop, wants_stop, "stop_price")):
        if wanted and given is None:
            raise order_guard.OrderRefused(f"order not placed: a {kind} order needs {field}")
        if given is not None and not wanted:
            raise order_guard.OrderRefused(
                f"order not placed: a {kind} order does not take {field} (the broker would ignore it, so it "
                "cannot size the risk check); leave it out or use an order type that uses it"
            )
    return {"ticker": name, "quantity": qty, "type": kind, "limitPrice": limit, "stopPrice": stop}


def _broker_ticker(ticker: str, held: dict[str, tuple[float, float | None]]) -> str:
    """The broker's own spelling of ``ticker`` when the account holds it under a case variant."""
    from dourmouse import order_guard

    if ticker in held:
        return ticker
    folded = [t for t in held if t.casefold() == ticker.casefold()]
    if len(folded) > 1:
        raise order_guard.OrderRefused(f"order not placed: {ticker!r} matches several held tickers {folded}")
    return folded[0] if folded else ticker


def _guard_order(base: str, key: str, order: dict[str, Any]) -> str:
    """Run the trading guardrails (P4-24) on a normalised order. Raises order_guard.OrderRefused;
    returns the ticker spelled as the broker spells it, which is what must be sent."""
    from dourmouse import order_guard
    from dourmouse.guardrails import Side

    try:
        equity, held, problems = _account_state(base, key)
    except RuntimeError as exc:
        raise order_guard.OrderRefused(
            f"order not placed: could not read the account to check the risk limits ({exc})"
        ) from exc
    ticker = _broker_ticker(order["ticker"], held)
    qty = order["quantity"]
    side = Side.SELL if qty < 0 else Side.BUY  # T212 sells are negative quantities
    env = "live" if base == _BASE_LIVE else "demo"
    held_qty, held_price = held.get(ticker, (0.0, None))
    if order_guard.is_pure_reduction(side, abs(qty), held_qty):
        # W3R-2: closing or trimming what the broker says is held is allowed even with the kill
        # switch tripped or other account data missing; nothing here can add exposure.
        order_guard.approve_reduction(f"t212:{env}", ticker, side, abs(qty), held_qty, equity)
        return ticker
    if problems or equity is None:
        raise order_guard.OrderRefused(
            "order not placed: could not read the account to check the risk limits ("
            + "; ".join(problems or ["no account total"]) + "). Only an order that reduces a position "
            "the broker reports can go ahead without that."
        )
    # W3R-3: size with the highest price the order could fill at. A MARKET order takes no price of
    # its own, so only the broker's current price of a held ticker can size it.
    candidates = [p for p in (order["limitPrice"], order["stopPrice"], held_price) if p is not None]
    if not candidates:
        raise order_guard.OrderRefused(
            f"order not placed: {ticker} is not held and a MARKET order has no price of its own, so its "
            "size cannot be checked against the risk limits. Place a LIMIT order with limit_price."
        )
    held_values = {t: q * p for t, (q, p) in held.items() if p is not None}
    order_guard.enforce(f"t212:{env}", ticker, side, abs(qty), max(candidates), equity, held_values)
    return ticker


def t212_order(
    ticker: str,
    quantity: float,
    order_type: str = "MARKET",
    limit_price: float | None = None,
    stop_price: float | None = None,
    paper_confirm: bool = False,
) -> dict[str, Any]:
    """Place a real order via the T212 API (paper-first).

    Refuses without ``paper_confirm=true`` AND (for live) the
    T212_ALLOW_LIVE=1 double gate. On demo this is genuine paper trading
    against T212's practice account — a real market order executes there.

    The order fields are validated first (order type in MARKET, LIMIT, STOP,
    STOP_LIMIT; only the price fields that type uses; finite numbers). Then
    ``order_guard`` runs the trading guardrails against the live account; a
    breach (or an account that cannot be read, unless the order only reduces
    a held position) raises ``order_guard.OrderRefused`` (a ValueError) and
    the order endpoint is never contacted. The payload carries exactly the
    normalised values the guard checked.
    """
    base, key = _config()
    # Literally True: the value is model-supplied, and a truthy string such as
    # "false" must not pass.
    if paper_confirm is not True:
        raise ValueError(
            "paper confirmation required: pass paper_confirm=true to place "
            "an order (paper-first, Rule 2.1)."
        )
    if base == _BASE_LIVE:
        # _config() already raised without the gate; reaching here means the
        # operator explicitly opened live. Still require an extra flag.
        if os.environ.get("T212_CONFIRM_LIVE", "").strip() != "1":
            raise ValueError(
                "live order blocked: set T212_CONFIRM_LIVE=1 as the final "
                "explicit confirmation before real-money orders."
            )
    order = _normalise_order(ticker, quantity, order_type, limit_price, stop_price)
    order["ticker"] = _guard_order(base, key, order)
    payload = {k: v for k, v in order.items() if v is not None}
    return _request(base, key, "/api/v0/equity/orders", payload)


# --------------------------------------------------------------------------- #
# Tool wrappers (roster-friendly, string-returning)
# --------------------------------------------------------------------------- #

def _wrap(fn):
    def tool(arguments: dict[str, Any]) -> str:
        try:
            return json.dumps(fn(**arguments), indent=1, default=str)
        except T212NotConfiguredError as exc:
            return f"NOT CONFIGURED: {exc}"
        except (ValueError, RuntimeError) as exc:
            return f"ERROR: {exc}"
    return tool


def build_t212_tool_specs() -> list[Any]:
    """ToolSpecs for the ``t212`` subagent (lazy ToolSpec import)."""
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
            "t212_status",
            "Trading 212 connection status: configured?, demo/live, and a real "
            "account probe result (or the honest failure reason).",
            _wrap(t212_status),
            {},
        ),
        _spec(
            "t212_account",
            "Real T212 account summary: equity, cash (free/blocked/invested), P&L.",
            _wrap(t212_account),
            {},
        ),
        _spec(
            "t212_positions",
            "Real open T212 positions with live P&L.",
            _wrap(t212_positions),
            {},
        ),
        _spec(
            "t212_portfolio",
            "Composed real view: account summary + open positions + open orders.",
            _wrap(t212_portfolio),
            {},
        ),
        # v8.15: gated on top of paper_confirm/the live env double-gate —
        # trade execution, even simulated, deserves the same human check as
        # this system's other consequential actions.
        _spec(
            "t212_order",
            "Place a REAL order via the T212 API. Paper-first: refuses without "
            "paper_confirm=true, and live requires the double gate. Demo "
            "executes against the practice account. The trading guardrails "
            "(position size, daily-loss kill switch) are checked first and a "
            "breach refuses the order (an order that only reduces a held "
            "position is still allowed). A MARKET order takes no limit_price or "
            "stop_price, and for a ticker not held it cannot be sized: use LIMIT "
            "with limit_price. REQUIRES human confirmation before it places any order.",
            _wrap(t212_order),
            {
                "ticker": {"type": "string", "description": "Instrument ticker, e.g. AAPL, VOO"},
                "quantity": {"type": "number", "description": "Number of shares; negative to sell"},
                "order_type": {"type": "string", "description": "MARKET (default) | LIMIT | STOP | STOP_LIMIT"},
                "limit_price": {"type": "number", "description": "Required for LIMIT/STOP_LIMIT"},
                "stop_price": {"type": "number", "description": "Required for STOP/STOP_LIMIT"},
                "paper_confirm": {"type": "boolean", "description": "Must be true to place any order (paper-first)"},
            },
            permission=Permission.REQUIRES_CONFIRMATION,
            confirm_prompt=lambda a: (
                f"Place T212 order: {a.get('order_type', 'MARKET')} "
                f"{a.get('quantity', '?')} shares of {a.get('ticker', '?')}? "
                "(practice account unless T212 live is explicitly opened)"
            ),
        ),
    ]
