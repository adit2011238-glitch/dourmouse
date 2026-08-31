"""ui/workspace.html panel control (v13.4) — the real, non-gimmick answer
to "real-time window sizes controlled by an LLM, no clicking required".

WHAT THIS ACTUALLY IS (say it plainly): a small set of real, deterministic
tools (Rule 2.8 — no model in the loop for the actual mutation; Rule 2.2 —
no silent stubs) that validate a requested floating-panel action and hand
back a structured spec for it. Panel geometry is 100% client-side DOM
state — it is never sent to, or owned by, the server — so these tools
cannot reach out and move a browser window themselves, the same honest
boundary design_3d_ops.py's generate_3d_model_spec already draws for 3D
scenes ("a spec-generation tool ... NOT a renderer"). What actually
applies the action is ui/workspace.html's own chat-panel SSE consumer: it
watches for a tool_result event from one of these five tool names, parses
the JSON this module returns, and calls the exact same setPanelRect/
openPanel/closePanel/bringToFront functions a mouse-drag or a
hand-gesture pinch already calls — one real mechanism, three ways to
drive it (mouse, hand, now natural language), not three competing ones.

Registered on a dedicated "panel_control" subagent (dourmouse/
general_roster.py) that the companion agent (ui/workspace.html's default
chat panel) reaches through its existing delegate_task/delegate_parallel
self-dispatch reach — see agent_prompts.py's companion system prompt,
which now names panel_control alongside mail/research_info/worldmonitor
in its own "route real work to the right subagent" examples. No new
client-facing agent, no new confirmation gate: panel layout is not data,
money, or a persistent write, so Permission.REGULAR applies to all five
tools here, same as design_3d_ops.py's own read-only/preview tools.

NAMING NOTE (real bug found live, 2026-08-30): this module and its
subagent were originally named "workspace_*"/"workspace_ui". That
collided with dourmouse/planner.py's routing scorer's real WORD-level
name match (not the old substring bug — a genuine whole-word hit): any
totally unrelated query containing the ordinary phrase "in your
workspace" (e.g. "save it to a file ... in your workspace" — a completely
normal way to ask dev_coding's write_file to save something) matched
"workspace_ui" by name and outscored the actual write-capable agent,
live-reproduced via dourmouse/tests/test_planner.py's own regression
suite. Renamed to "panel_control"/plain "*_panel" tool names specifically
to stop using a word ("workspace") that is both this app's own generic
file-sandbox term (DOURMOUSE_WORKSPACE, _workspace_root()) AND common
English — not a defensive patch to the scorer, a fix at the source.
"""

from __future__ import annotations

import json
from typing import Any

from dourmouse.dispatch import Permission, ToolSpec

#: The real panel ids ui/workspace.html's openPanel() actually renders.
#: Kept here, not guessed per-call, for the same reason
#: dourmouse/voice_commands.py's own _PANEL_ALIASES is centralized: one
#: deterministic source of truth so an unknown panel name is reported
#: honestly instead of silently producing an action for a panel that
#: doesn't exist.
PANEL_IDS: tuple[str, ...] = ("mail", "chat", "research", "map", "globe", "design3d")

_PANEL_DESCRIPTIONS: dict[str, str] = {
    "mail": "Gmail inbox (real, via /api/gmail/search + /api/gmail/read)",
    "chat": "the companion — general conversational chat",
    "research": "research_info — web search / synthesis chat",
    "map": "the real-time world pulse map (/api/worldmap)",
    "globe": "God's Eye View — the real embedded 3D globe (aircraft/ships/satellites/earthquakes)",
    "design3d": "the real Three.js 3D model scene editor",
}

# ui/workspace.html's own setPanelRect() clamps to these same floors —
# mirrored here so a request this module can already tell is unsatisfiable
# ("make it 10 pixels wide") is reported honestly up front, not silently
# clamped somewhere the caller never sees.
_MIN_WIDTH = 260
_MIN_HEIGHT = 180


def _validate_panel(arguments: dict[str, Any]) -> tuple[str, str] | str:
    """Returns (panel, "") on success, or an "ERROR: ..." string."""
    panel = str(arguments.get("panel") or "").strip().lower()
    if not panel:
        return "ERROR: requires a non-empty 'panel'."
    if panel not in PANEL_IDS:
        return f"ERROR: unknown panel {panel!r}. Known panels: {', '.join(PANEL_IDS)}."
    return panel, ""


def _list_panel_types_tool(_arguments: dict[str, Any]) -> str:
    lines = [f"{p}: {_PANEL_DESCRIPTIONS[p]}" for p in PANEL_IDS]
    return (
        "Real floating-panel types ui/workspace.html can open (this tool "
        "does not know which ones are CURRENTLY open — the chat request "
        "already carries that as live client-side context when "
        "relevant):\n" + "\n".join(lines)
    )


def _open_panel_tool(arguments: dict[str, Any]) -> str:
    result = _validate_panel(arguments)
    if isinstance(result, str):
        return result
    panel, _ = result
    return f"Open the '{panel}' panel.\n" + json.dumps({"action": "open", "panel": panel})


def _close_panel_tool(arguments: dict[str, Any]) -> str:
    result = _validate_panel(arguments)
    if isinstance(result, str):
        return result
    panel, _ = result
    return f"Close the '{panel}' panel.\n" + json.dumps({"action": "close", "panel": panel})


def _move_panel_tool(arguments: dict[str, Any]) -> str:
    result = _validate_panel(arguments)
    if isinstance(result, str):
        return result
    panel, _ = result
    x, y = arguments.get("x"), arguments.get("y")
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return "ERROR: move_panel requires numeric 'x' and 'y' (pixels from the top-left)."
    return (
        f"Move the '{panel}' panel to ({x}, {y}).\n"
        + json.dumps({"action": "move", "panel": panel, "x": float(x), "y": float(y)})
    )


def _resize_panel_tool(arguments: dict[str, Any]) -> str:
    result = _validate_panel(arguments)
    if isinstance(result, str):
        return result
    panel, _ = result
    width, height = arguments.get("width"), arguments.get("height")
    if not isinstance(width, (int, float)) or not isinstance(height, (int, float)):
        return "ERROR: resize_panel requires numeric 'width' and 'height' (pixels)."
    if width < _MIN_WIDTH or height < _MIN_HEIGHT:
        return (
            f"ERROR: requested size ({width}x{height}) is below the panel's real minimum "
            f"({_MIN_WIDTH}x{_MIN_HEIGHT}) — ui/workspace.html would clamp to the minimum "
            "anyway, so this is reported honestly instead of silently returning a size "
            "different from what was asked."
        )
    return (
        f"Resize the '{panel}' panel to {width}x{height}.\n"
        + json.dumps({"action": "resize", "panel": panel, "width": float(width), "height": float(height)})
    )


def build_workspace_panel_tool_specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="list_panel_types",
            description=(
                "List the real floating-panel types DourMouse's Vision "
                "screen can open — mail, chat, research, map, globe, "
                "design3d. Call this first if unsure what panel a request "
                "refers to."
            ),
            parameters={"type": "object", "properties": {}, "required": []},
            handler=_list_panel_types_tool,
            permission=Permission.REGULAR,
        ),
        ToolSpec(
            name="open_panel",
            description=(
                "Open (or focus, if already open) a floating panel by real "
                "panel id (see list_panel_types)."
            ),
            parameters={
                "type": "object",
                "properties": {"panel": {"type": "string", "description": "One of: mail, chat, research, map, globe, design3d."}},
                "required": ["panel"],
            },
            handler=_open_panel_tool,
            permission=Permission.REGULAR,
        ),
        ToolSpec(
            name="close_panel",
            description="Close a floating panel by real panel id.",
            parameters={
                "type": "object",
                "properties": {"panel": {"type": "string", "description": "One of: mail, chat, research, map, globe, design3d."}},
                "required": ["panel"],
            },
            handler=_close_panel_tool,
            permission=Permission.REGULAR,
        ),
        ToolSpec(
            name="move_panel",
            description=(
                "Move a floating panel to an exact pixel position "
                "(top-left origin). Reposition only — does not change size."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "panel": {"type": "string", "description": "One of: mail, chat, research, map, globe, design3d."},
                    "x": {"type": "number", "description": "Pixels from the left edge."},
                    "y": {"type": "number", "description": "Pixels from the top edge."},
                },
                "required": ["panel", "x", "y"],
            },
            handler=_move_panel_tool,
            permission=Permission.REGULAR,
        ),
        ToolSpec(
            name="resize_panel",
            description=(
                "Resize a floating panel in real time (pixels). Minimum "
                f"{_MIN_WIDTH}x{_MIN_HEIGHT} — a smaller request is refused "
                "honestly rather than silently clamped."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "panel": {"type": "string", "description": "One of: mail, chat, research, map, globe, design3d."},
                    "width": {"type": "number"},
                    "height": {"type": "number"},
                },
                "required": ["panel", "width", "height"],
            },
            handler=_resize_panel_tool,
            permission=Permission.REGULAR,
        ),
    ]
