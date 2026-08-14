"""atlas_scheduler.py — calendar-driven paper trader for the locked ATLAS legs.

TradingView strategy alerts only fire while your TradingView session is open,
which makes them useless for the three locked seasonal legs — their entry and
exit are *calendar events* (first trading day of month M in, last trading day
of month M out), not price events. This scheduler fires those signals itself:

    python -m dourmouse.atlas_scheduler [--once] [--date YYYY-MM-DD]
                                         [--dry-run] [--daemon] [--interval 1800]

Every run it walks the locked legs in tradingview_ops.LEGS and decides, for
the current UTC date:

    today == first trading day of leg.month   -> OPEN  the leg
    today == last  trading day of leg.month   -> CLOSE the leg
    otherwise                                 -> nothing

Signals go through the SAME pipeline as a TradingView alert: record_signal()
(appends to workspace/tv_signals.jsonl + HUD feed) then route_to_paper()
(appends to FOREX_DATA_PATH/reports/paper_log.csv with the identical
open/close/P&L semantics). One code path, one paper book — TradingView and
the scheduler are interchangeable signal sources.

Prices are fetched live from Yahoo Finance continuous front-month futures
(HE=F, ZC=F) at signal time and recorded honestly in the log. If the fetch
fails the signal is NOT fabricated: the run logs the failure and skips.

Trading-day approximation (documented): first/last trading day = the
first/last weekday of the month that is not one of the ten major US market
holidays (New Year, MLK, Presidents', Good Friday, Memorial Day, Juneteenth,
July 4, Labor Day, Thanksgiving, Christmas). CME early closes are ignored —
they do not change the day a daily signal belongs to. This is a paper
approximation, not a production settlement calendar.

Idempotency: an OPEN is only applied when no row for that leg already exists
with an entry date inside the target window; a CLOSE only when an open row
exists. Running every 30 minutes (or a daily scheduled task) is therefore
safe — the second run of a day is a no-op, reported honestly.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dourmouse.tradingview_ops import LEGS, record_signal, route_to_paper

log = logging.getLogger("atlas_scheduler")

# Ten major US market holidays (fixed or first-observance approximations).
# Good Friday is computed via the anonymous Gregorian Easter algorithm;
# the rest are rule-based. Enough for a paper signal day; not a settlement
# calendar.
def _good_friday(year: int) -> date:
    """Good Friday for `year` (Easter Sunday - 2 days)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day) - timedelta(days=2)


def _holidays(year: int) -> set[date]:
    """Major US market holidays for one year (CME-ish approximation)."""
    def nth_weekday(y: int, m: int, wd: int, n: int) -> date:
        first = date(y, m, 1)
        offset = (wd - first.weekday()) % 7
        return first + timedelta(days=offset + 7 * (n - 1))

    s: set[date] = {
        date(year, 1, 1),                      # New Year's Day
        nth_weekday(year, 1, 0, 3),            # MLK (3rd Mon Jan)
        nth_weekday(year, 2, 0, 3),            # Presidents' Day (3rd Mon Feb)
        _good_friday(year),                    # Good Friday
        nth_weekday(year, 5, 0, 5),            # Memorial Day (last Mon May)
        date(year, 6, 19),                     # Juneteenth
        date(year, 7, 4),                      # Independence Day
        nth_weekday(year, 9, 0, 1),            # Labor Day (1st Mon Sep)
        nth_weekday(year, 11, 3, 4),           # Thanksgiving (4th Thu Nov)
        date(year, 12, 25),                    # Christmas
    }
    return s


def first_trading_day(year: int, month: int) -> date:
    """First weekday of the month not on a major US holiday."""
    d = date(year, month, 1)
    holidays = _holidays(year)
    while d.weekday() >= 5 or d in holidays:
        d += timedelta(days=1)
    return d


def last_trading_day(year: int, month: int) -> date:
    """Last weekday of the month not on a major US holiday."""
    if month == 12:
        d = date(year, 12, 31)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    holidays = _holidays(year)
    while d.weekday() >= 5 or d in holidays:
        d -= timedelta(days=1)
    return d


# ---------------------------------------------------------------------------
# Live price fetch (Yahoo continuous front-month futures). Bounded, honest:
# returns None on any failure so the caller can skip rather than fabricate.
# ---------------------------------------------------------------------------

_YAHOO_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
    "?interval=1d&range=5d"
)
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0"
}
_YAHOO_SYMBOLS = {"HE_8": "HE=F", "HE_4": "HE=F", "ZC_12": "ZC=F"}


def fetch_price(key: str, timeout: float = 30.0) -> float | None:
    """Last close of the continuous front-month future for a locked leg.

    Returns None (never raises) when the fetch fails, so a dead network can
    never produce a fabricated paper fill.
    """
    sym = _YAHOO_SYMBOLS.get(key)
    if sym is None:
        return None
    try:
        url = _YAHOO_URL.format(sym=urllib.parse.quote(sym))
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        closes = [
            c for c in payload["chart"]["result"][0]["indicators"]["quote"][0]["close"]
            if c is not None
        ]
        if not closes:
            return None
        return float(closes[-1])
    except Exception as exc:  # noqa: BLE001 -- honest None, never raise
        log.warning("price fetch failed for %s (%s): %r", key, sym, exc)
        return None


# ---------------------------------------------------------------------------
# Window bookkeeping: has this leg already been opened/closed in its window?
# ---------------------------------------------------------------------------

def _paper_log_rows() -> list[list[str]]:
    """Rows from FOREX_DATA_PATH/reports/paper_log.csv, or [] when unset."""
    raw = os.environ.get("FOREX_DATA_PATH", "").strip()
    if not raw:
        return []
    path = Path(raw).expanduser() / "reports" / "paper_log.csv"
    if not path.exists():
        return []
    import csv

    try:
        with open(path, newline="", encoding="utf-8") as fh:
            return [r for r in csv.reader(fh) if r and r[0] != "key"]
    except OSError:
        return []


def _leg_open_in_window(key: str, year: int, month: int) -> bool:
    """True if the paper log already has an OPEN row for `key` whose entry
    date falls inside (year, month) — prevents double-opening a window."""
    prefix = f"{year:04d}-{month:02d}"
    for r in _paper_log_rows():
        # schema: key,side,venue,entry_date,entry_price,contract,exit_date,...
        if r[0] == key and r[6] == "" and r[3].startswith(prefix):
            return True
    return False


def _leg_has_open(key: str) -> bool:
    """True if any OPEN row exists for the leg (exit price empty)."""
    return any(r[0] == key and r[6] == "" for r in _paper_log_rows())


# ---------------------------------------------------------------------------
# Core run
# ---------------------------------------------------------------------------

def _load_env() -> None:
    """Load .env (setdefault — real env wins) like tv_webhook_server."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())
    except OSError:
        pass


def _side_for(key: str, action: str) -> str:
    """route_to_paper expects alert-style sides: short leg opens on sell,
    closes on buy; long leg opens on buy, closes on sell."""
    direction = LEGS[key]["direction"]
    if action == "open":
        return "sell" if direction == "short" else "buy"
    return "buy" if direction == "short" else "sell"


def run_once(today: date | None = None, dry_run: bool = False) -> list[dict[str, Any]]:
    """Evaluate every locked leg against `today` (default: UTC date).

    Returns one report dict per leg: {key, window, action ("open"|"close"|
    "none"), applied, reason, price}. Never raises for a single leg — the
    whole run degrades gracefully so one bad price can't block the others.
    """
    today = today or datetime.now(timezone.utc).date()
    reports: list[dict[str, Any]] = []
    for key, leg in sorted(LEGS.items()):
        month, direction = leg["month"], leg["direction"]
        report: dict[str, Any] = {
            "key": key, "direction": direction, "month": month,
            "today": today.isoformat(), "action": "none", "applied": False,
            "reason": "not a window day", "price": None,
        }
        try:
            open_day = first_trading_day(today.year, month)
            close_day = last_trading_day(today.year, month)
            report["window"] = {"open": open_day.isoformat(),
                                "close": close_day.isoformat()}
            if today == open_day:
                if _leg_open_in_window(key, today.year, month):
                    report.update(action="open", reason="already open this window")
                    reports.append(report)
                    continue
                price = None if dry_run else fetch_price(key)
                if price is None and not dry_run:
                    report.update(action="open",
                                  reason="price fetch failed — skipped, will retry next run")
                    reports.append(report)
                    continue
                report.update(action="open", price=price)
                if not dry_run:
                    sig = {
                        "source": "atlas-scheduler",
                        "strategy": key,
                        "ticker": leg["ticker"],
                        "side": _side_for(key, "open"),
                        "price": str(price),
                        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    }
                    record_signal(sig)
                    applied = route_to_paper(sig)
                    report.update(applied=applied.get("applied", False),
                                  reason=applied.get("reason") or "applied")
                else:
                    report["reason"] = "dry-run — would open"
            elif today == close_day:
                if not _leg_has_open(key):
                    report.update(action="close", reason="no open row to close")
                    reports.append(report)
                    continue
                price = None if dry_run else fetch_price(key)
                if price is None and not dry_run:
                    report.update(action="close",
                                  reason="price fetch failed — skipped, will retry next run")
                    reports.append(report)
                    continue
                report.update(action="close", price=price)
                if not dry_run:
                    sig = {
                        "source": "atlas-scheduler",
                        "strategy": key,
                        "ticker": leg["ticker"],
                        "side": _side_for(key, "close"),
                        "price": str(price),
                        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    }
                    record_signal(sig)
                    applied = route_to_paper(sig)
                    report.update(applied=applied.get("applied", False),
                                  reason=applied.get("reason") or "applied")
                else:
                    report["reason"] = "dry-run — would close"
        except Exception as exc:  # noqa: BLE001 -- one leg never kills a run
            report.update(reason=f"error: {exc!r}")
        reports.append(report)
    return reports


def main(argv: list[str] | None = None) -> int:
    _load_env()
    args = list(argv if argv is not None else __import__("sys").argv[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true",
                        help="evaluate now and exit (default)")
    parser.add_argument("--daemon", action="store_true",
                        help="loop forever, evaluating every --interval seconds")
    parser.add_argument("--interval", type=int, default=1800)
    parser.add_argument("--date", default=None,
                        help="override today (YYYY-MM-DD) — testing only")
    parser.add_argument("--dry-run", action="store_true",
                        help="decide + print, but never write to any log")
    parsed, _ = parser.parse_known_args(args)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler()],
    )

    today = date.fromisoformat(parsed.date) if parsed.date else None

    def _cycle() -> int:
        reports = run_once(today, dry_run=parsed.dry_run)
        for r in reports:
            if r["action"] != "none":
                window = r.get("window") or {}
                log.info(
                    "%s %s %s (window %s..%s) price=%s applied=%s reason=%s",
                    r["key"], r["direction"], r["action"],
                    window.get("open"), window.get("close"),
                    r["price"], r["applied"], r["reason"],
                )
            else:
                log.info("%s %s: %s (window %s..%s)", r["key"],
                         r["direction"], r["reason"],
                         (r.get("window") or {}).get("open"),
                         (r.get("window") or {}).get("close"))
        return 0

    if parsed.daemon:
        log.info("atlas scheduler daemon: evaluating every %ss", parsed.interval)
        while True:
            try:
                _cycle()
            except Exception as exc:  # noqa: BLE001
                log.exception("cycle failed: %r", exc)
            time.sleep(parsed.interval)
    return _cycle()


if __name__ == "__main__":
    raise SystemExit(main())
