"""ATLAS Command Center (v8.1) — RUN the research pipeline from Dourmouse.

Reads and executes the REAL pipeline scripts in ``FOREX_DATA_PATH`` so the
roster can not only report on the data but drive it:

- ``atlas_standard`` — the LOCKED standard (protocol + exact numbers) from
  ``reports/validation_standard.json``, the machine-readable output of the
  five-stage validation suite. This is the standard every answer cites.
- ``atlas_run_validation`` — runs ``scripts/seasonal_validation.py`` (the
  full five-stage suite) and refreshes the standard file.
- ``atlas_run_walkforward`` / ``atlas_run_backtest`` — the strict walk-
  forward and the seasonal backtest.
- ``atlas_paper_*`` — the paper-trading log (open / close / status).
- ``atlas_calendar`` / ``atlas_refresh_events`` — live calendar + events.
- ``atlas_full_status`` — one consolidated "everything" report.

All executions are real subprocesses with timeouts; failures are reported
honestly (Rule 2.2) and never fabricated. Nothing here is a stub.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from dourmouse.forex_ops import (
    ForexNotConfiguredError,
    get_forex_data_path,
    forex_events,
    forex_ibkr,
    forex_inventory,
    forex_paper,
)

_LONG = 600          # validation / backtests / event refresh
_SHORT = 60          # calendar / paper log


def _run_script(args: list[str], timeout: int) -> tuple[int, str, str]:
    """Run one pipeline script in the forex-data root (real subprocess)."""
    root = get_forex_data_path()
    try:
        proc = subprocess.run(
            ["python", *args],
            cwd=str(root), capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, "", f"could not run {' '.join(args)}: {exc}"
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _tail(text: str, n: int = 40) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


# --------------------------------------------------------------------------- #
# The locked standard
# --------------------------------------------------------------------------- #

def atlas_standard() -> dict[str, Any]:
    """The locked standard (protocol + exact numbers)."""
    try:
        root = get_forex_data_path()
    except ForexNotConfiguredError as exc:
        return {"configured": False, "error": str(exc)}
    path = root / "reports" / "validation_standard.json"
    if not path.is_file():
        return {"configured": True, "error": "validation_standard.json missing — "
                "run atlas_run_validation to generate the standard."}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"configured": True, "error": f"standard unreadable: {exc}"}
    return {"configured": True, "error": "", **data}


def _atlas_standard_tool(arguments: dict[str, Any]) -> str:
    s = atlas_standard()
    if not s.get("configured"):
        return f"ATLAS STANDARD (reported honestly): NOT CONFIGURED — {s.get('error', '')}"
    if s.get("error"):
        return f"ATLAS STANDARD: {s['error']}"
    nums = s["numbers"]
    core = nums.get("portfolio_core") or {}
    boot = nums.get("bootstrap") or {}
    legs = {k: v for k, v in nums.get("legs", {}).items() if v.get("t") is not None}
    lines = [
        f"ATLAS STANDARD (generated {s.get('generated_utc', '?')}):",
        f"  protocol: in-sample {s['protocol']['in_sample'][0]} -> "
        f"{s['protocol']['in_sample'][1]} | OOS from {s['protocol']['oos_start']} | "
        f"5 stages: {', '.join(s['protocol']['stages'])}",
        f"  locked config: T={s['config']['T']}, min_n={s['config']['min_n']}, "
        f"in-sample legs {s['config']['in_sample_legs']}",
        f"  permutation p: {nums.get('permutation_p'):.4f} "
        f"(bar < 0.01: {'PASS' if (nums.get('permutation_p') or 1) < 0.01 else 'FAIL'})",
        f"  core legs (HE_8, HE_4, ZC_12): terminal ${core.get('terminal', '—'):.2f}, "
        f"Sharpe {core.get('sharpe', '—')}, max DD {core.get('maxdd_pct', '—')}%",
        f"  all-gated portfolio: terminal ${nums.get('portfolio_all', {}).get('terminal', '—')}",
        f"  bootstrap: median ${boot.get('terminal_median', '—')}, "
        f"P(loss) = {boot.get('p_loss_pct', '—')}%",
        "  leg t-stats (OOS): " + ", ".join(f"{k} {v['t']:+.2f}" for k, v in sorted(legs.items())),
        f"  verdict: {s.get('verdict', '')}",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Run the pipeline (real scripts)
# --------------------------------------------------------------------------- #

def _atlas_run_validation_tool(arguments: dict[str, Any]) -> str:
    code, out, err = _run_script(["scripts/seasonal_validation.py"], _LONG)
    head = [f"ATLAS RUN VALIDATION (exit {code}):"]
    if code == 0:
        head.append("suite complete — standard refreshed.")
    else:
        head.append(f"suite FAILED: {_tail(err, 6)}")
    head.append(_tail(out, 30))
    return "\n".join(head)


def _atlas_run_walkforward_tool(arguments: dict[str, Any]) -> str:
    code, out, err = _run_script(["scripts/seasonal_walkforward.py"], _LONG)
    return (f"ATLAS RUN WALK-FORWARD (exit {code}):\n" +
            (_tail(out, 25) if code == 0 else _tail(err, 8)))


def _atlas_run_backtest_tool(arguments: dict[str, Any]) -> str:
    code, out, err = _run_script(["scripts/seasonal_backtest.py"], _LONG)
    return (f"ATLAS RUN BACKTEST (exit {code}):\n" +
            (_tail(out, 25) if code == 0 else _tail(err, 8)))


def _atlas_calendar_tool(arguments: dict[str, Any]) -> str:
    code, out, err = _run_script(["scripts/seasonal_calendar.py"], _SHORT)
    return (f"ATLAS CALENDAR (exit {code}):\n" +
            (_tail(out, 25) if code == 0 else _tail(err, 8)))


def _atlas_refresh_events_tool(arguments: dict[str, Any]) -> str:
    code, out, err = _run_script(["scripts/calendar_fetch.py"], _LONG)
    return (f"ATLAS REFRESH EVENTS (exit {code}):\n" +
            (_tail(out, 20) if code == 0 else _tail(err, 8)))


# --------------------------------------------------------------------------- #
# Paper trading (real log commands)
# --------------------------------------------------------------------------- #

def _atlas_paper_status_tool(arguments: dict[str, Any]) -> str:
    code, out, err = _run_script(["scripts/paper_log.py", "status"], _SHORT)
    return (f"ATLAS PAPER STATUS (exit {code}):\n" +
            (_tail(out, 25) if code == 0 else _tail(err, 8)))


def _atlas_paper_open_tool(arguments: dict[str, Any]) -> str:
    leg = str(arguments.get("leg", "")).strip()
    date = str(arguments.get("date", "")).strip()
    try:
        price = float(arguments.get("price"))
        size = float(arguments.get("size"))
    except (TypeError, ValueError):
        return "ERROR: price and size must be numbers."
    if not leg or not date:
        return "ERROR: leg (e.g. HE_8) and date (YYYY-MM-DD) are required."
    code, out, err = _run_script(
        ["scripts/paper_log.py", "open", leg, "--date", date,
         "--price", str(price), "--size", str(size)], _SHORT)
    return (f"ATLAS PAPER OPEN {leg} (exit {code}):\n" +
            (_tail(out, 12) if code == 0 else _tail(err, 8)))


def _atlas_paper_close_tool(arguments: dict[str, Any]) -> str:
    leg = str(arguments.get("leg", "")).strip()
    date = str(arguments.get("date", "")).strip()
    try:
        price = float(arguments.get("price"))
    except (TypeError, ValueError):
        return "ERROR: price must be a number."
    if not leg or not date:
        return "ERROR: leg (e.g. HE_8) and date (YYYY-MM-DD) are required."
    code, out, err = _run_script(
        ["scripts/paper_log.py", "close", leg, "--date", date,
         "--price", str(price)], _SHORT)
    return (f"ATLAS PAPER CLOSE {leg} (exit {code}):\n" +
            (_tail(out, 12) if code == 0 else _tail(err, 8)))


# --------------------------------------------------------------------------- #
# Everything
# --------------------------------------------------------------------------- #

def _atlas_full_status_tool(arguments: dict[str, Any]) -> str:
    parts = [_atlas_standard_tool({})]
    try:
        inv = forex_inventory()
        parts.append(
            f"DATA INVENTORY: {inv['fx_pairs']} FX pairs · "
            f"{inv['commodities']} commodities · {inv['total_bars']:,} bars · "
            f"{inv['fundamentals_files']} fundamentals files"
        )
    except ForexNotConfiguredError as exc:
        parts.append(f"DATA INVENTORY: NOT CONFIGURED — {exc}")
    parts.append(_atlas_calendar_tool({}))
    try:
        evs = forex_events(hours_ahead=72, limit=8)
        rows = evs.get("rows", [])
        parts.append(f"UPCOMING EVENTS (72h): {len(rows)}" +
                     ("" if not rows else " — " + ", ".join(
                         f"{r['title']} [{r['impact']}]" for r in rows[:4])))
    except ForexNotConfiguredError:
        pass
    parts.append(_atlas_paper_status_tool({}))
    ib = forex_ibkr()
    parts.append(f"IBKR GATEWAY: {'REACHABLE' if ib.get('reachable') else 'UNREACHABLE'}")
    return "\n\n".join(parts)


def build_atlas_cmd_tool_specs() -> list[Any]:
    """ToolSpecs for the ``atlas_cmd`` subagent (run the pipeline from here)."""
    from dourmouse.dispatch import ToolSpec

    def _spec(name: str, description: str, handler, props: dict[str, Any]) -> Any:
        return ToolSpec(
            name=name,
            description=description,
            parameters={"type": "object", "properties": props, "required": []},
            handler=handler,
        )

    return [
        _spec("atlas_standard",
              "The LOCKED validation standard: protocol, config and the exact "
              "numbers (permutation p, core legs, portfolio, bootstrap) that "
              "every answer must cite.",
              _atlas_standard_tool, {}),
        _spec("atlas_run_validation",
              "RUN the full five-stage validation suite "
              "(scripts/seasonal_validation.py) and refresh the standard.",
              _atlas_run_validation_tool, {}),
        _spec("atlas_run_walkforward",
              "RUN the strict walk-forward (scripts/seasonal_walkforward.py).",
              _atlas_run_walkforward_tool, {}),
        _spec("atlas_run_backtest",
              "RUN the seasonal backtest (scripts/seasonal_backtest.py).",
              _atlas_run_backtest_tool, {}),
        _spec("atlas_calendar",
              "LIVE paper-trading calendar (next trade windows).",
              _atlas_calendar_tool, {}),
        _spec("atlas_refresh_events",
              "Refresh the economic-calendar archive from the live feed "
              "(scripts/calendar_fetch.py; can take minutes).",
              _atlas_refresh_events_tool, {}),
        _spec("atlas_paper_status",
              "Paper-trading log status: open positions, closed trades, P&L.",
              _atlas_paper_status_tool, {}),
        _spec("atlas_paper_open",
              "Log a paper entry. leg (HE_8/HE_4/ZC_12/...), date YYYY-MM-DD, "
              "price, size.",
              _atlas_paper_open_tool,
              {"leg": {"type": "string"}, "date": {"type": "string"},
               "price": {"type": "number"}, "size": {"type": "number"}}),
        _spec("atlas_paper_close",
              "Log a paper exit. leg, date YYYY-MM-DD, price.",
              _atlas_paper_close_tool,
              {"leg": {"type": "string"}, "date": {"type": "string"},
               "price": {"type": "number"}}),
        _spec("atlas_full_status",
              "EVERYTHING in one block: standard + inventory + calendar + "
              "events + paper log + IBKR gateway.",
              _atlas_full_status_tool, {}),
    ]
