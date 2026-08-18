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

Scope honesty: the official API covers Invest/ISA *equity* accounts
(stocks/ETFs) — it does NOT expose CFDs, so the commodity-seasonal CFD
strategy cannot be executed through it. That path stays manual (T212 app)
or via IBKR when the gateway is up. This module manages the portfolio
and paper-orders the equity side; the CFD legs are logged separately in
the forex paper log.
"""

from __future__ import annotations

import json
import os
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
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = " · " + exc.read().decode()[:200]
        except Exception:  # noqa: BLE001 -- best-effort detail
            pass
        raise RuntimeError(f"T212 API {exc.code} on {path}: {exc.reason}{detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"T212 API unreachable ({path}): {exc.reason}") from exc


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
    """
    base, key = _config()
    if not paper_confirm:
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
    payload: dict[str, Any] = {
        "ticker": ticker,
        "quantity": quantity,
        "type": order_type,
    }
    if limit_price is not None:
        payload["limitPrice"] = limit_price
    if stop_price is not None:
        payload["stopPrice"] = stop_price
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
            "executes against the practice account. REQUIRES human "
            "confirmation before it places any order.",
            _wrap(t212_order),
            {
                "ticker": {"type": "string", "description": "Instrument ticker, e.g. AAPL, VOO"},
                "quantity": {"type": "number", "description": "Number of shares"},
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
