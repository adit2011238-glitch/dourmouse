"""forex-data pipeline telemetry (v6.0) — real status for Dourmouse.

Gives the roster honest, deterministic answers about the REAL forex
research pipeline (the ``FOREX_DATA_PATH`` tree — data inventory, the
validated commodity-seasonal strategy, the economic-calendar feed, the
paper-trading log and the IBKR paper gateway) without fabricating
anything (Rules 2.1 / 2.2 / 2.8):

- ``forex_inventory()`` — what data actually exists: normalized FX/commodity
  series (pairs, timeframes, bar counts, quality), raw commodity daily
  series, the events calendar archive, fundamentals, newest reports.
- ``forex_strategy()`` — the validated seasonal strategy: the verdict from
  ``reports/VALIDATION_REPORT.md``, the trade cards, and the LIVE paper
  calendar (``scripts/seasonal_calendar.py``) — real subprocess output.
- ``forex_events()`` — upcoming high-impact calendar entries from
  ``market-data/events/events.parquet`` (next N hours, deterministic).
- ``forex_paper()`` — the paper-trading log (``reports/paper_log.csv``):
  open positions, realised P&L.
- ``forex_ibkr()`` — IBKR paper-gateway reachability (TCP connect, 2s
  timeout) — real probe, honest result.

Configuration: ``FOREX_DATA_PATH`` env. Until it is set (or the dir is
missing), every tool reports NOT CONFIGURED honestly — never a stub.
"""

from __future__ import annotations

import csv
import os
import socket
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_DATA_ENV = "FOREX_DATA_PATH"
_IBKR_HOST_ENV = "IBKR_HOST"
_IBKR_PORT_ENV = "IBKR_PORT"
_DEFAULT_IBKR_HOST = "192.168.1.95"
_DEFAULT_IBKR_PORT = 7497


class ForexNotConfiguredError(NotImplementedError):
    pass


def get_forex_data_path() -> Path:
    """The real forex-data pipeline root, or raise honestly (Rule 2.2)."""
    raw = os.environ.get(_DATA_ENV)
    if not raw:
        raise ForexNotConfiguredError(
            f"{_DATA_ENV} is not set. Set it in .env to the real forex-data "
            "pipeline root to enable forex telemetry."
        )
    path = Path(raw).expanduser()
    if not path.is_dir():
        raise ForexNotConfiguredError(f"{_DATA_ENV} does not exist: {path}")
    return path


# --------------------------------------------------------------------------- #
# Data inventory (pure filesystem + CSV reads — deterministic)
# --------------------------------------------------------------------------- #

def _csv_rows(path: Path) -> list[dict[str, str]]:
    """Read a small CSV into dict rows (stdlib only)."""
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", errors="replace", newline="") as fh:
        return list(csv.DictReader(fh))


def forex_inventory() -> dict[str, Any]:
    """What data actually exists in the pipeline."""
    root = get_forex_data_path()

    # normalized manifest (FX pairs x timeframes)
    manifest = _csv_rows(root / "market-data" / "normalized" / "manifest.csv")
    pairs: dict[str, dict[str, Any]] = {}
    timeframe_counts: dict[str, int] = {}
    total_bars = 0
    d1_rows = []
    for row in manifest:
        tf = row.get("timeframe", "")
        timeframe_counts[tf] = timeframe_counts.get(tf, 0) + 1
        try:
            total_bars += int(row.get("bars", 0) or 0)
        except ValueError:
            pass
        if tf == "D1":
            d1_rows.append(
                f"{row.get('pair')}: {row.get('start_utc', '')[:10]} -> "
                f"{row.get('end_utc', '')[:10]} ({row.get('bars')} bars, "
                f"{row.get('quality')})"
            )

    # raw commodity daily series (the seasonal universe)
    comms = sorted(p.name[len("COMM_"):-len("_d.csv")]
                   for p in (root / "market-data" / "raw" / "yahoo").glob("COMM_*_d.csv"))
    comm_first_last = ""
    if comms:
        sample = root / "market-data" / "raw" / "yahoo" / f"COMM_{comms[0]}_d.csv"
        rows = _csv_rows(sample)
        if rows:
            dates = [r.get("date", "") for r in rows if r.get("date")]
            if dates:
                comm_first_last = f"{dates[0]} -> {dates[-1]}"

    # events archive + fundamentals
    events_path = root / "market-data" / "events" / "events.parquet"
    events_count = events_path.stat().st_size if events_path.is_file() else 0
    fundamentals = sorted((root / "market-data" / "fundamentals").glob("*.csv")) if (
        root / "market-data" / "fundamentals").is_dir() else []

    # newest reports (research outputs)
    reports_dir = root / "reports"
    newest = []
    if reports_dir.is_dir():
        files = sorted(reports_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files[:5]:
            try:
                mtime = datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")
            except OSError:
                mtime = ""
            newest.append({"path": p.name, "modified": mtime})

    return {
        "configured": True,
        "root": str(root),
        "fx_pairs": len([r for r in manifest if r.get("timeframe") == "D1"]),
        "timeframe_counts": timeframe_counts,
        "total_bars": total_bars,
        "d1_coverage": d1_rows,
        "commodities": len(comms),
        "commodity_range": comm_first_last,
        "events_parquet_bytes": events_count,
        "fundamentals_files": len(fundamentals),
        "newest_reports": newest,
    }


def _forex_inventory_tool(arguments: dict[str, Any]) -> str:
    try:
        inv = forex_inventory()
    except ForexNotConfiguredError as exc:
        return f"FOREX INVENTORY (reported honestly): NOT CONFIGURED — {exc}"
    lines = ["FOREX DATA INVENTORY:"]
    lines.append(f"  root: {inv['root']}")
    lines.append(f"  fx pairs (D1): {inv['fx_pairs']} | timeframes: "
                 f"{ {k: v for k, v in sorted(inv['timeframe_counts'].items())} }")
    lines.append(f"  total bars (normalized): {inv['total_bars']:,}")
    for row in inv["d1_coverage"][:4]:
        lines.append(f"    {row}")
    if len(inv["d1_coverage"]) > 4:
        lines.append(f"    ... and {len(inv['d1_coverage']) - 4} more pairs")
    lines.append(f"  commodity daily series: {inv['commodities']} "
                 f"({inv['commodity_range'] or 'unknown range'})")
    lines.append(f"  events archive: {inv['events_parquet_bytes']:,} bytes")
    lines.append(f"  fundamentals files: {inv['fundamentals_files']}")
    if inv["newest_reports"]:
        lines.append("  newest reports:")
        for r in inv["newest_reports"]:
            lines.append(f"    {r['path']}  ({r['modified']})")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Strategy status (validation verdict + live paper calendar)
# --------------------------------------------------------------------------- #

def _report_section(path: Path, marker: str, max_chars: int = 700) -> str:
    """Return the text after ``marker`` in a report file (tail-fallback)."""
    if not path.is_file():
        return "(missing)"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"(unreadable: {exc})"
    idx = text.find(marker)
    if idx >= 0:
        text = text[idx:]
    return text[:max_chars].strip()


def forex_strategy() -> dict[str, Any]:
    """Validated seasonal strategy: verdict, trade cards, live calendar."""
    root = get_forex_data_path()
    verdict = _report_section(root / "reports" / "VALIDATION_REPORT.md",
                              "## 8. Verdict", 900)
    cards = _report_section(root / "reports" / "TRADE_CARDS.md", "#", 600)

    calendar = ""
    try:
        proc = subprocess.run(
            ["python", "scripts/seasonal_calendar.py"],
            cwd=str(root), capture_output=True, text=True, timeout=30,
        )
        calendar = (proc.stdout or "").strip()
        if proc.returncode != 0:
            calendar = f"(calendar exited {proc.returncode}: {(proc.stderr or '').strip()[:200]})"
    except (OSError, subprocess.TimeoutExpired) as exc:
        calendar = f"(calendar unavailable: {exc})"

    return {
        "configured": True,
        "verdict": verdict,
        "trade_cards": cards,
        "calendar": calendar,
    }


def _forex_strategy_tool(arguments: dict[str, Any]) -> str:
    try:
        s = forex_strategy()
    except ForexNotConfiguredError as exc:
        return f"FOREX STRATEGY (reported honestly): NOT CONFIGURED — {exc}"
    parts = ["FOREX STRATEGY — VALIDATED SEASONALS:", s["verdict"]]
    parts.append("\nPAPER-TRADING CALENDAR (live):")
    parts.append(s["calendar"][:1400])
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Economic calendar (upcoming high-impact events)
# --------------------------------------------------------------------------- #

def _read_events_parquet(path: Path) -> list[dict[str, Any]] | None:
    """Read events.parquet via pandas or pyarrow; None if unavailable."""
    try:
        import pandas as pd  # type: ignore[import-not-found]
        return pd.read_parquet(path).to_dict("records")
    except ImportError:
        pass
    try:
        import pyarrow.parquet as pq  # type: ignore[import-not-found]
        return pq.read_table(path).to_pylist()
    except ImportError:
        return None


def _read_events(root: Path) -> list[dict[str, Any]] | None:
    """Read the events archive: parquet (pandas/pyarrow) then CSV (stdlib).
    Returns None only when no source is readable."""
    pq_path = root / "market-data" / "events" / "events.parquet"
    if pq_path.is_file():
        rows = _read_events_parquet(pq_path)
        if rows is not None:
            return rows
    csv_path = root / "market-data" / "events" / "events.csv"
    if csv_path.is_file():
        return _csv_rows(csv_path)
    return None


def forex_events(hours_ahead: int = 48, limit: int = 15) -> dict[str, Any]:
    """Upcoming calendar entries with actuals still unpublished."""
    root = get_forex_data_path()
    rows = _read_events(root)
    if rows is None:
        return {"configured": True, "rows": [],
                "note": "events archive missing (events.parquet / events.csv)"}
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=max(1, int(hours_ahead)))
    out = []
    for r in rows:
        ts_raw = r.get("date_utc")
        if not ts_raw:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts < now or ts > cutoff:
            continue
        actual = r.get("actual")
        if actual is not None and str(actual).strip() not in ("", "nan", "NaN"):
            continue  # already released
        impact = str(r.get("impact", ""))
        tier = r.get("tier")
        if impact.lower() not in ("high", "medium") and str(tier) not in ("1", "2"):
            continue
        out.append({
            "when": ts.isoformat(timespec="minutes"),
            "title": str(r.get("title", "")),
            "country": str(r.get("country", "")),
            "impact": impact or f"tier{tier}",
            "forecast": r.get("forecast"),
            "previous": r.get("previous"),
        })
    out.sort(key=lambda x: x["when"])
    return {"configured": True, "rows": out[:max(1, int(limit))], "note": ""}


def _forex_events_tool(arguments: dict[str, Any]) -> str:
    try:
        hours = int(arguments.get("hours_ahead", 48))
        lim = int(arguments.get("limit", 15))
    except (TypeError, ValueError):
        return "ERROR: hours_ahead and limit must be integers."
    try:
        ev = forex_events(hours, lim)
    except ForexNotConfiguredError as exc:
        return f"FOREX EVENTS (reported honestly): NOT CONFIGURED — {exc}"
    if ev.get("note"):
        return f"FOREX EVENTS (reported honestly): {ev['note']}"
    if not ev["rows"]:
        return "FOREX EVENTS: none upcoming in the window (or archive empty)."
    lines = [f"FOREX EVENTS — next {hours}h (upcoming, high/medium impact):"]
    for r in ev["rows"]:
        fc = r["forecast"]
        pv = r["previous"]
        lines.append(
            f"  {r['when']} [{r['impact']}] {r['title']} ({r['country']})"
            + (f"  fcst={fc} prev={pv}" if fc is not None or pv is not None else "")
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Paper-trading log
# --------------------------------------------------------------------------- #

def forex_paper() -> dict[str, Any]:
    """Paper log state: open positions, closed trades, realised P&L."""
    root = get_forex_data_path()
    path = root / "reports" / "paper_log.csv"
    rows = _csv_rows(path)
    open_pos = [r for r in rows if not r.get("exit_date")]
    closed = [r for r in rows if r.get("exit_date")]
    try:
        total_pnl = sum(float(r.get("pnl_usd") or 0) for r in closed)
    except ValueError:
        total_pnl = 0.0
    return {
        "configured": True,
        "log_file": path.name if path.is_file() else None,
        "trades": len(rows),
        "open_positions": open_pos,
        "closed_trades": len(closed),
        "realised_pnl_usd": total_pnl,
    }


def _forex_paper_tool(arguments: dict[str, Any]) -> str:
    try:
        p = forex_paper()
    except ForexNotConfiguredError as exc:
        return f"FOREX PAPER LOG (reported honestly): NOT CONFIGURED — {exc}"
    if p["log_file"] is None:
        return "FOREX PAPER LOG: no log yet (reports/paper_log.csv missing)."
    lines = [f"FOREX PAPER LOG ({p['log_file']}):"]
    if p["open_positions"]:
        lines.append("  OPEN positions:")
        for r in p["open_positions"]:
            lines.append(f"    {r.get('key','?')}: entry {r.get('entry_date','?')} "
                         f"@ {r.get('entry_price','?')} size {r.get('size','?')}")
    else:
        lines.append("  open positions: none")
    lines.append(f"  closed trades: {p['closed_trades']} | "
                 f"realised P&L: ${p['realised_pnl_usd']:.2f}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# IBKR paper gateway reachability (real TCP probe)
# --------------------------------------------------------------------------- #

def forex_ibkr() -> dict[str, Any]:
    """Probe the IBKR paper gateway (host:port) with a 2s timeout."""
    host = os.environ.get(_IBKR_HOST_ENV, _DEFAULT_IBKR_HOST)
    try:
        port = int(os.environ.get(_IBKR_PORT_ENV, _DEFAULT_IBKR_PORT))
    except ValueError:
        port = _DEFAULT_IBKR_PORT
    try:
        with socket.create_connection((host, port), timeout=2.0):
            return {"configured": True, "host": host, "port": port, "reachable": True}
    except OSError as exc:
        return {"configured": True, "host": host, "port": port,
                "reachable": False, "error": str(exc)}


def _forex_ibkr_tool(arguments: dict[str, Any]) -> str:
    try:
        s = forex_ibkr()
    except Exception as exc:  # defensive: probe must never crash the roster
        return f"FOREX IBKR (reported honestly): probe failed — {exc}"
    if s["reachable"]:
        return (f"FOREX IBKR GATEWAY: REACHABLE ({s['host']}:{s['port']}) — "
                f"futures execution ready (ibkr_connector.py --futures-list "
                f"resolves the 26-symbol seasonal universe; --paper-order is "
                f"paper-first and refuses the live port)")
    return (f"FOREX IBKR GATEWAY: UNREACHABLE ({s['host']}:{s['port']}) — "
            f"{s.get('error', 'no route')}. Start IB Gateway with socket "
            f"clients enabled and retry.")


# --------------------------------------------------------------------------- #
# Consolidated report
# --------------------------------------------------------------------------- #

def _forex_report_tool(arguments: dict[str, Any]) -> str:
    parts = []
    for fn in (_forex_inventory_tool, _forex_strategy_tool,
               _forex_events_tool, _forex_paper_tool, _forex_ibkr_tool):
        try:
            parts.append(fn({"hours_ahead": 48, "limit": 8}))
        except Exception as exc:  # defensive: one section never kills the rest
            parts.append(f"(section failed: {exc})")
    return "\n\n".join(parts)


def build_forex_tool_specs() -> list[Any]:
    """ToolSpecs for the ``forex`` subagent (lazy ToolSpec import)."""
    from dourmouse.dispatch import ToolSpec

    def _spec(name: str, description: str, handler, props: dict[str, Any]) -> Any:
        return ToolSpec(
            name=name,
            description=description,
            parameters={"type": "object", "properties": props, "required": []},
            handler=handler,
        )

    return [
        _spec(
            "forex_inventory",
            "Real forex-data pipeline inventory: FX/commodity series, bar "
            "counts, D1 coverage, events archive, fundamentals, newest reports.",
            _forex_inventory_tool,
            {},
        ),
        _spec(
            "forex_strategy",
            "Validated commodity-seasonal strategy: verdict from the "
            "validation report, trade cards, and the LIVE paper calendar.",
            _forex_strategy_tool,
            {},
        ),
        _spec(
            "forex_events",
            "Upcoming high/medium-impact economic calendar entries from the "
            "real events archive (default: next 48 hours).",
            _forex_events_tool,
            {"hours_ahead": {"type": "integer", "default": 48},
             "limit": {"type": "integer", "default": 15}},
        ),
        _spec(
            "forex_paper",
            "Paper-trading log: open positions, closed trades, realised P&L.",
            _forex_paper_tool,
            {},
        ),
        _spec(
            "forex_ibkr",
            "IBKR paper-gateway reachability (real 2s TCP probe to host:port).",
            _forex_ibkr_tool,
            {},
        ),
        _spec(
            "forex_report",
            "One consolidated forex-pipeline telemetry block: inventory + "
            "strategy + events + paper log + IBKR gateway.",
            _forex_report_tool,
            {},
        ),
    ]
