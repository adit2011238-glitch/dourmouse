"""Real-data layer for the ATLAS Terminal (v8.0 upgrade).

Every number shown by the terminal comes from one of:

- ``dourmouse.forex_ops`` (the pipeline telemetry tools: inventory,
  strategy calendar, events, paper log, IBKR probe), or
- ``reports/VALIDATION_REPORT.md`` (the five-stage validation suite),
  parsed here into machine-readable numbers, or
- the paper log CSV.

Nothing is fabricated (Rule 2.2): when ``FOREX_DATA_PATH`` is unset or a
file is missing, the layer returns ``configured=False`` / ``None`` and the
modules render an honest NOT CONFIGURED state instead of mock numbers.
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime
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

CORE_LEGS = ["HE_8", "HE_4", "ZC_12"]
# reports use the unicode minus (U+2212) in negative figures
_SIGN = r"[+\-\u2212]"


def pipeline() -> dict[str, Any]:
    """Pipeline context: configured flag + inventory (honest on failure)."""
    try:
        inv = forex_inventory()
    except ForexNotConfiguredError as exc:
        return {"configured": False, "error": str(exc)}
    except Exception as exc:  # defensive: UI must never crash on data
        return {"configured": False, "error": f"inventory failed: {exc}"}
    return {"configured": True, "error": "", **inv}


def _num(text: str | None) -> float | None:
    """Parse a number out of a possibly-markdown-wrapped string."""
    if not text:
        return None
    m = re.search(r"[-+]?\d+(?:\.\d+)?", str(text).replace(",", "").replace("\u2212", "-"))
    return float(m.group()) if m else None


def validation() -> dict[str, Any]:
    """Parse the validation report into machine-readable numbers."""
    try:
        root = get_forex_data_path()
    except ForexNotConfiguredError as exc:
        return {"configured": False, "error": str(exc)}
    path = root / "reports" / "VALIDATION_REPORT.md"
    if not path.is_file():
        return {"configured": True, "error": "VALIDATION_REPORT.md missing"}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"configured": True, "error": f"report unreadable: {exc}"}

    def find(pattern: str) -> float | None:
        m = re.search(pattern, text)
        return _num(m.group(1)) if m else None

    # stage-2 permutation p-value
    perm_p = find(r"p \(shuffled[^)]*actual\)[^|]*\|\s*\*?\*?([\d.]+)")

    # per-leg OOS stats (Stage 3 table; bold cells + unicode minus tolerated)
    legs: dict[str, dict[str, Any]] = {}
    for m in re.finditer(
        r"\*?\*?([A-Z]{2}_\d+)\*?\*?[^|]*\|\s*(\d+)\s*\|\s*\*?\*?"
        rf"({_SIGN}[\d.]+)%\*?\*?\s*\|\s*\*?\*?({_SIGN}[\d.]+)%\*?\*?\s*\|\s*"
        r"\*?\*?([\d.]+)%\*?\*?\s*\|\s*\*?\*?(" + _SIGN + r"[\d.]+)\*?\*?\s*\|\s*"
        r"\*?\*?(\d+)%\*?\*?",
        text,
    ):
        key = m.group(1)
        legs[key] = {
            "n": int(m.group(2)),
            "mean": _num(m.group(3)),
            "median": _num(m.group(4)),
            "std": _num(m.group(5)),
            "t": _num(m.group(6)),
            "win": _num(m.group(7)),
        }

    # all-trades line
    all_m = re.search(
        r"ALL 60 trades[^|]*\|\s*60\s*\|\s*\*?\*?(" + _SIGN + r"[\d.]+)%\*?\*?\s*\|\s*"
        r"\*?\*?(" + _SIGN + r"[\d.]+)%\*?\*?\s*\|\s*\*?\*?([\d.]+)%\*?\*?\s*\|\s*"
        r"\*?\*?(" + _SIGN + r"[\d.]+)\*?\*?\s*\|\s*\*?\*?(\d+)%\*?\*?",
        text,
    )

    def all_stat(i: int) -> float | None:
        return _num(all_m.group(i)) if all_m else None

    # portfolio (all gated legs)
    term_all = find(r"Terminal:\s*\*\*\$([\d.]+)\*\*")
    sharpe_all = find(r"Sharpe ([\d.]+) · max drawdown")
    mdd_all = find(r"max drawdown \*\*\u2212?([\d.]+)%\*\*")
    ann_mean = find(r"mean \*\*\+?([\d.]+)%\*\*")
    ann_std = find(r"std \*\*±?([\d.]+)%\*\*")

    # core portfolio row
    core_m = re.search(
        r"CORE \(HE_8, HE_4, ZC_12\)[^|]*\|\s*\*?\*?\$([\d.]+)\*?\*?\s*\|\s*"
        r"\*?\*?([\d.]+)\*?\*?\s*\|\s*\*?\*?\u2212?([\d.]+)%\*?\*?",
        text,
    )
    core = {
        "terminal": _num(core_m.group(1)) if core_m else None,
        "sharpe": _num(core_m.group(2)) if core_m else None,
        "maxdd": -abs(_num(core_m.group(3))) if core_m and core_m.group(3) else None,
    }

    # bootstrap (Stage 5 table: "| Terminal equity | $325 | **$410** | $490 |")
    boot_m = re.search(
        r"Terminal equity\s*\|\s*\$([\d.]+)\s*\|\s*\*?\*?\$([\d.]+)\*?\*?\s*\|\s*\$([\d.]+)",
        text,
    )
    boot_med = _num(boot_m.group(2)) if boot_m else None
    boot_p5 = _num(boot_m.group(1)) if boot_m else None
    boot_p95 = _num(boot_m.group(3)) if boot_m else None
    p_loss = find(r"P\(terminal < \$100\) = ([\d.]+)%")

    # verdict section
    verdict = ""
    idx = text.find("## 8. Verdict")
    if idx >= 0:
        verdict = text[idx: idx + 900].strip()

    return {
        "configured": True,
        "error": "",
        "perm_p": perm_p,
        "legs": legs,
        "all": {
            "n": 60,
            "mean": all_stat(1),
            "median": all_stat(2),
            "std": all_stat(3),
            "t": all_stat(4),
            "win": all_stat(5),
        },
        "portfolio": {
            "terminal": term_all,
            "sharpe": sharpe_all,
            "maxdd": -abs(mdd_all) if mdd_all is not None else None,
            "ann_mean": ann_mean,
            "ann_std": ann_std,
        },
        "core": core,
        "bootstrap": {
            "median": boot_med,
            "p5": boot_p5,
            "p95": boot_p95,
            "p_loss": p_loss,
        },
        "verdict": verdict,
    }


def strategy_calendar() -> list[dict[str, Any]]:
    """Parse the live calendar output into windows (real subprocess data)."""
    try:
        root = get_forex_data_path()
    except ForexNotConfiguredError:
        return []
    try:
        proc = subprocess.run(
            ["python", "scripts/seasonal_calendar.py"],
            cwd=str(root), capture_output=True, text=True, timeout=30,
        )
        out = proc.stdout or ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        out = f"(calendar unavailable: {exc})"

    windows: list[dict[str, Any]] = []
    leg: str | None = None
    for line in out.splitlines():
        lm = re.match(r"^([A-Z]{2}_\d+)\s+", line.strip())
        if lm:
            leg = lm.group(1)
            continue
        if leg:
            wm = re.search(
                r"(\d{4}): entry (\d{4}-\d{2}-\d{2}) \(open\) -> exit "
                r"(\d{4}-\d{2}-\d{2}) \(close\)\s+\[([A-Z ]+)\]",
                line,
            )
            if wm:
                windows.append({
                    "leg": leg,
                    "year": wm.group(1),
                    "entry": wm.group(2),
                    "exit": wm.group(3),
                    "status": wm.group(4),
                })
    return windows


def events(limit: int = 10) -> list[dict[str, Any]]:
    try:
        ev = forex_events(hours_ahead=72, limit=limit)
    except ForexNotConfiguredError:
        return []
    return ev.get("rows", []) or []


def paper() -> dict[str, Any]:
    try:
        return forex_paper()
    except ForexNotConfiguredError:
        return {"configured": False, "error": "FOREX_DATA_PATH not set"}


def ibkr() -> dict[str, Any]:
    return forex_ibkr()


def live() -> dict[str, Any]:
    """One consolidated real snapshot for the terminal."""
    p = pipeline()
    v = validation()
    return {
        "pipeline": p,
        "validation": v,
        "calendar": strategy_calendar(),
        "events": events(10),
        "paper": paper(),
        "ibkr": ibkr(),
        "generated": datetime.now().isoformat(timespec="seconds"),
    }
