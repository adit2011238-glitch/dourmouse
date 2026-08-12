"""ATLAS Terminal status tools (v8.0).

One tool for the roster: ``atlas_terminal_status`` — a deterministic read of
what the ATLAS Terminal would show right now (pipeline configured?, the
validation verdict, the next trade window, upcoming events, paper log,
IBKR gateway). Reuses the terminal's own real-data layer
(``atlas_terminal.data.live``) so the roster and the UI can never disagree.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _atlas_terminal_status_tool(arguments: dict[str, Any]) -> str:
    try:
        from atlas_terminal import data as terminal_data
        L = terminal_data.live()
    except Exception as exc:  # defensive: never crash the roster
        return f"ATLAS TERMINAL STATUS (reported honestly): unavailable — {exc}"

    pipe = L["pipeline"]
    if not pipe.get("configured", False):
        return (f"ATLAS TERMINAL STATUS: NOT CONFIGURED — set FOREX_DATA_PATH "
                f"({pipe.get('error', '')}). The terminal shows an honest "
                f"offline state; no numbers are fabricated.")
    val = L["validation"]
    cal = L["calendar"]
    evs = L["events"]
    paper = L["paper"]
    ibkr = L["ibkr"]
    next_win = next((w for w in cal if w["status"] == "NOW OPEN"), cal[0] if cal else None)
    core = val.get("core", {})
    lines = [
        "ATLAS TERMINAL STATUS:",
        f"  pipeline: CONFIGURED ({pipe['root']})",
        f"  data: {pipe['fx_pairs']} FX pairs · {pipe['commodities']} commodities · "
        f"{pipe['total_bars']:,} bars",
        f"  validation: perm p = {val.get('perm_p', '—')} · "
        f"core Sharpe = {core.get('sharpe', '—')} · "
        f"core max DD = {core.get('maxdd', '—')}%",
        f"  next trade: {next_win['leg']} {next_win['entry']}→{next_win['exit']} "
        f"[{next_win['status']}]" if next_win else "  next trade: none",
        f"  upcoming events (72h): {len(evs)}",
        f"  paper log: {paper.get('trades', 0)} trades · "
        f"${paper.get('realised_pnl_usd', 0.0):.2f} realised",
        f"  IBKR gateway: {'REACHABLE' if ibkr.get('reachable') else 'UNREACHABLE'}",
    ]
    return "\n".join(lines)


def build_atlas_ui_tool_specs() -> list[Any]:
    """ToolSpecs for the ``atlas_ui`` subagent."""
    from dourmouse.dispatch import ToolSpec

    return [
        ToolSpec(
            name="atlas_terminal_status",
            description=(
                "Deterministic read of what the ATLAS Terminal shows right now: "
                "pipeline configured?, validation verdict, next trade window, "
                "upcoming events, paper log, IBKR gateway."
            ),
            parameters={"type": "object", "properties": {}, "required": []},
            handler=_atlas_terminal_status_tool,
        ),
    ]
