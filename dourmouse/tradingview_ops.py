"""tradingview_ops.py — TradingView <-> dourmouse signal bridge.

TradingView has NO order-execution API for retail. What it does have is
alerts with webhooks: an alert fires on a chart/strategy and TradingView
POSTs the alert message to a URL. That URL is this module's endpoint
(POST /api/tv-webhook in webui.py).

Pipeline:
    TradingView strategy alert
        -> POST (form-encoded `payload=` or raw JSON)
        -> /api/tv-webhook
        -> handle_tv_webhook()  [validates secret + shape]
        -> workspace/tv_signals.jsonl   (append-only signal log)
        -> AGENT COMMS bus broadcast     (visible in the HUD)

This module is deliberately DUMB and honest (Rules 2.1/2.2): it logs what
it received and validates it; it does NOT fabricate fills, prices, or
positions. Execution (paper or live) is a separate step owned by the
paper engine — the webhook only ever records a signal.

Secrets:
    TV_WEBHOOK_SECRET  — string the alert message must carry in its JSON
        (TradingView cannot add headers, so the secret rides in the body:
        {"secret":"...", ...}). Empty/absent = open webhook (loopback OK,
        not for a public tunnel).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Locked seasonal legs from the strict walk-forward survivors (three trades
# that survived selection, blind holdout, Monte Carlo permutations and the
# walk-forward battery). Ticker = TradingView continuous front-month.
LEGS: dict[str, dict[str, Any]] = {
    "HE_8": {
        "name": "Lean Hogs August SHORT",
        "ticker": "HE1!",
        "month": 8,
        "direction": "short",
        "exchange": "CME",
    },
    "HE_4": {
        "name": "Lean Hogs April LONG",
        "ticker": "HE1!",
        "month": 4,
        "direction": "long",
        "exchange": "CME",
    },
    "ZC_12": {
        "name": "Corn December LONG",
        "ticker": "ZC1!",
        "month": 12,
        "direction": "long",
        "exchange": "CBOT",
    },
}

# Pine v5 strategy template: enters on the first trading day of the target
# month, exits at the month change (first bar of the following month). This
# mirrors the ATLAS backtest rule "first trading day in, month-end close"
# closely enough for a visual/signal tool; the authoritative numbers live in
# the ATLAS backtest, not here.
_PINE_TEMPLATE = """//@version=5
// {name}  — ATLAS seasonal leg (locked: {key})
// Direction: {direction} · Entry month: {month} · Ticker: {ticker}
// Visual/signal aid only. Authority: the ATLAS strict backtest.
strategy("{name}", overlay=true, initial_capital=100,
     default_qty_type=strategy.percent_of_equity, default_qty_value=10,
     commission_type=strategy.commission.percent, commission_value=0.05)

TARGET_MONTH = {month}
isMonthStart = month(time) != month(time[1])      // first bar of a month

inWindow = false
if isMonthStart and month(time) == TARGET_MONTH
    inWindow := true
if isMonthStart and month(time) != TARGET_MONTH
    inWindow := false

if inWindow and strategy.position_size == 0
    if "{direction}" == "long"
        strategy.entry("{key}-L", strategy.long)
    else
        strategy.entry("{key}-S", strategy.short)

// exit at the month change (first bar of the following month)
if strategy.position_size != 0 and not inWindow
    strategy.close_all(comment="month-end")
"""


def _signals_path() -> Path:
    """workspace/tv_signals.jsonl — created on demand."""
    raw = os.environ.get("DOURMOUSE_WORKSPACE")
    root = Path(raw).expanduser() if raw else Path(__file__).resolve().parent.parent / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    return root / "tv_signals.jsonl"


def webhook_secret() -> str:
    """TV_WEBHOOK_SECRET from env; '' = open webhook (loopback posture)."""
    return os.environ.get("TV_WEBHOOK_SECRET", "").strip()


def parse_tv_payload(body: bytes, content_type: str) -> dict[str, Any] | None:
    """Parse a TradingView webhook body into a dict.

    TradingView sends form-encoded ``payload=<message>`` by default, and raw
    JSON when the alert message is JSON and the URL is configured for it.
    Accepts both. Returns None on any parse failure (never raises).
    """
    if not body:
        return None
    ctype = (content_type or "").lower()
    try:
        if "application/json" in ctype:
            parsed = json.loads(body.decode("utf-8"))
            return parsed if isinstance(parsed, dict) else None
        # form-encoded: payload=<urlencoded json>
        import urllib.parse

        fields = urllib.parse.parse_qs(body.decode("utf-8"))
        raw = fields.get("payload", [""])[0]
        parsed = json.loads(raw) if raw else None
        return parsed if isinstance(parsed, dict) else None
    except (ValueError, UnicodeDecodeError):
        return None


def validate_signal(signal: dict[str, Any]) -> tuple[bool, str]:
    """Validate a parsed signal against the webhook secret + shape.

    Returns (ok, reason). The secret must match TV_WEBHOOK_SECRET when one
    is configured; an open webhook (no secret set) accepts anything but is
    only safe on the loopback default.
    """
    secret = webhook_secret()
    if secret:
        got = str(signal.get("secret") or "")
        if got != secret:
            return False, "secret mismatch"
    ticker = str(signal.get("ticker") or signal.get("symbol") or "").strip()
    if not ticker:
        return False, "missing ticker/symbol"
    return True, "ok"


def record_signal(signal: dict[str, Any]) -> dict[str, Any]:
    """Append one validated signal to workspace/tv_signals.jsonl.

    Enriches with a UTC timestamp and the source marker. Returns the stored
    record (with the secret stripped — never persisted).
    """
    record = {k: v for k, v in signal.items() if k != "secret"}
    record.setdefault("source", "tradingview")
    record.setdefault("received_utc", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    line = json.dumps(record, default=str)
    with open(_signals_path(), "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return record


def handle_tv_webhook(
    body: bytes, content_type: str = "", bus=None
) -> dict[str, Any]:
    """Full webhook pipeline for POST /api/tv-webhook.

    Parse -> validate -> persist -> broadcast to the AGENT COMMS bus.
    Always returns a dict (never raises): the HTTP layer maps it to JSON.
    """
    signal = parse_tv_payload(body, content_type)
    if signal is None:
        return {"ok": False, "error": "unparseable payload (expected JSON or payload=JSON)"}
    ok, reason = validate_signal(signal)
    if not ok:
        return {"ok": False, "error": reason}
    record = record_signal(signal)
    applied = route_to_paper(signal)
    record["_paper"] = applied
    if bus is not None:
        try:
            act = applied.get("action", "?")
            bus.post(
                "tradingview",
                "BROADCAST",
                f"TV {act.upper()}",
                f"{signal.get('strategy', '?')} {signal.get('side', '?')} "
                f"{signal.get('ticker', '?')} @ {signal.get('price', '?')}"
                + (f" pnl% {applied['pnl_pct']:.4f}" if applied.get("pnl_pct") is not None else ""),
            )
        except Exception:  # noqa: BLE001 -- a failed broadcast never drops the signal
            pass
    return {"ok": True, "record": record, "paper": applied}


def recent_signals(limit: int = 20) -> list[dict[str, Any]]:
    """Newest N signal records from the log (for the HUD panel)."""
    path = _signals_path()
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return rows


def tv_pine_script(key: str) -> str | None:
    """Generated Pine v5 strategy for one locked leg (for copy-paste)."""
    leg = LEGS.get(key)
    if leg is None:
        return None
    return _PINE_TEMPLATE.format(key=key, **leg)


def tv_alert_template(key: str, secret: str | None = None) -> str | None:
    """JSON to paste into a TradingView strategy alert's Message field.

    The secret rides in the payload (TradingView cannot send headers).
    """
    leg = LEGS.get(key)
    if leg is None:
        return None
    sec = secret if secret is not None else webhook_secret()
    return json.dumps(
        {
            "secret": sec,
            "source": "tradingview",
            "strategy": key,
            "ticker": "{{ticker}}",
            "side": "{{strategy.order.action}}",
            "price": "{{close}}",
            "time": "{{timenow}}",
        }
    )


# ---------------------------------------------------------------------------
# Paper-engine routing: a validated signal for a locked leg is applied to the
# shared seasonal paper log (FOREX_DATA_PATH/reports/paper_log.csv) so the
# TradingView alerts actually drive the paper book.
# Schema (matches seasonal_mt5.py):
#   key,side,venue,entry_date,entry_price,contract,exit_date,exit_price,
#   pnl_pct,pnl_usd,notes
# Side is the LEG direction (long/short). A strategy alert fires on entry
# AND exit ({{strategy.order.action}} buy/sell): for a short leg sell=open,
# buy=close; for a long leg buy=open, sell=close.
# ---------------------------------------------------------------------------

_PAPER_REFERENCE_NOTIONAL = 100.0  # nominal $100 reference for pnl_usd


def _paper_log_path() -> Path | None:
    """FOREX_DATA_PATH/reports/paper_log.csv, or None when unset."""
    raw = os.environ.get("FOREX_DATA_PATH", "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser() / "reports" / "paper_log.csv"
    return p


def _write_paper_row(row: list[str]) -> bool:
    """Append one CSV row to the paper log (thread-safe, creates header)."""
    import csv

    path = _paper_log_path()
    if path is None:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if fh.tell() == 0:
                writer.writerow(
                    ["key", "side", "venue", "entry_date", "entry_price",
                     "contract", "exit_date", "exit_price", "pnl_pct",
                     "pnl_usd", "notes"]
                )
            writer.writerow(row)
        return True
    except OSError:
        return False


def _read_paper_rows() -> list[list[str]]:
    """All paper-log rows (header stripped); empty on any failure."""
    import csv

    path = _paper_log_path()
    if path is None or not path.exists():
        return []
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            return [r for r in csv.reader(fh) if r and r[0] != "key"]
    except OSError:
        return []


def _rewrite_paper_rows(rows: list[list[str]]) -> bool:
    """Rewrite the whole log (header + rows). Locked via a module mutex."""
    import csv

    path = _paper_log_path()
    if path is None:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(
                ["key", "side", "venue", "entry_date", "entry_price",
                 "contract", "exit_date", "exit_price", "pnl_pct",
                 "pnl_usd", "notes"]
            )
            writer.writerows(rows)
        return True
    except OSError:
        return False


_PAPER_LOCK = __import__("threading").Lock()


def route_to_paper(signal: dict[str, Any]) -> dict[str, Any]:
    """Apply a validated TradingView signal to the seasonal paper log.

    Returns {"applied": bool, "action": "open"|"close"|"unknown", ...}.
    Only locked legs with a resolvable action are written; everything else
    is reported honestly as not applied (never fabricated).
    """
    key = str(signal.get("strategy") or "").strip()
    leg = LEGS.get(key)
    if leg is None:
        return {"applied": False, "action": "unknown",
                "reason": f"{key!r} is not a locked leg"}
    side = str(signal.get("side") or "").strip().lower()
    price_raw = str(signal.get("price") or "").strip()
    try:
        price = float(price_raw)
    except (TypeError, ValueError):
        return {"applied": False, "action": "unknown",
                "reason": f"price not numeric: {price_raw!r}"}
    if price <= 0:
        return {"applied": False, "action": "unknown",
                "reason": f"price not positive: {price}"}

    # Map alert action to open/close for this leg's direction.
    leg_dir = leg["direction"]  # "long" | "short"
    action: str | None
    if leg_dir == "short":
        action = "open" if side == "sell" else ("close" if side == "buy" else None)
    else:
        action = "open" if side == "buy" else ("close" if side == "sell" else None)
    if action is None:
        return {"applied": False, "action": "unknown",
                "reason": f"side {side!r} not open/close for {key}"}

    if _paper_log_path() is None:
        return {"applied": False, "action": "unknown",
                "reason": "FOREX_DATA_PATH not set — paper log unavailable"}

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    contract = leg["ticker"]
    with _PAPER_LOCK:
        rows = _read_paper_rows()
        if action == "open":
            row = [key, leg_dir, "tradingview", now, f"{price:.5f}", contract,
                   "", "", "", "", "tv alert open"]
            rows.append(row)
            if not _rewrite_paper_rows(rows):
                return {"applied": False, "action": "open",
                        "reason": "paper log write failed"}
            return {"applied": True, "action": "open", "key": key,
                    "price": price, "row": row}
        # close: fill the newest OPEN row for this key (FIFO per key).
        for i in range(len(rows) - 1, -1, -1):
            r = rows[i]
            if r[0] == key and not r[6]:  # same key, no exit yet
                entry = float(r[4])
                pnl_pct = ((price - entry) / entry if leg_dir == "long"
                           else (entry - price) / entry)
                pnl_usd = pnl_pct * _PAPER_REFERENCE_NOTIONAL
                r[6] = now
                r[7] = f"{price:.5f}"
                r[8] = f"{pnl_pct:.4f}"
                r[9] = f"{pnl_usd:.2f}"
                r[10] = "tv alert close"
                if not _rewrite_paper_rows(rows):
                    return {"applied": False, "action": "close",
                            "reason": "paper log write failed"}
                return {"applied": True, "action": "close", "key": key,
                        "price": price, "pnl_pct": pnl_pct,
                        "pnl_usd": pnl_usd, "row": r}
        return {"applied": False, "action": "close",
                "reason": f"no open {key} row to close"}


def tv_panel_snapshot() -> dict[str, Any]:
    """HUD panel data: configured?, legs, alert URL(s), recent signals.

    The public URL (TV_PUBLIC_URL, set by the cloudflared tunnel) is shown
    when configured; the loopback URL is the local default. Never claims a
    tunnel that isn't recorded (Rule 2.2)."""
    public = os.environ.get("TV_PUBLIC_URL", "").strip()
    return {
        "configured": bool(webhook_secret()),
        "legs": [
            {"key": k, **{kk: vv for kk, vv in v.items()}}
            for k, v in sorted(LEGS.items())
        ],
        "webhook_url": "http://127.0.0.1:8765/api/tv-webhook",
        "public_url": public or None,
        "note": "TradingView webhooks require a PUBLIC https URL — set "
                "TV_PUBLIC_URL to the active tunnel URL",
        "recent_signals": recent_signals(limit=10),
    }
