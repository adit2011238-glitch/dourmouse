"""dourmouse/workspace_panel_ops.py — the real, deterministic tools behind
"real-time window sizes controlled by an LLM, no clicking required".

These tests cover the actual mutation-free contract this module draws for
itself (validate + spec, never apply): every handler returns human text
followed by a JSON blob describing the requested action; the client
(ui/workspace.html, covered separately in
dourmouse/tests/test_workspace_panel_control.py) is what actually applies
it.
"""

from __future__ import annotations

import json

from dourmouse import general_roster
from dourmouse.dispatch import Permission
from dourmouse.workspace_panel_ops import (
    PANEL_IDS,
    _close_panel_tool,
    _list_panel_types_tool,
    _move_panel_tool,
    _open_panel_tool,
    _organize_panels_tool,
    _resize_panel_tool,
    build_workspace_panel_tool_specs,
)


def _json_tail(text: str) -> dict:
    i = text.index("{")
    return json.loads(text[i:])


class TestPanelIdsMatchTheRealClientCatalog:
    def test_six_real_panel_types(self):
        # Must match ui/workspace.html's openPanel() titles map exactly --
        # a drift here would let the model "successfully" spec an action
        # for a panel the client has never heard of.
        assert set(PANEL_IDS) == {"mail", "chat", "research", "map", "globe", "design3d"}


class TestOpenClosePanel:
    def test_open_valid_panel(self):
        out = _open_panel_tool({"panel": "map"})
        assert not out.startswith("ERROR")
        spec = _json_tail(out)
        assert spec == {"action": "open", "panel": "map"}

    def test_open_unknown_panel_refused(self):
        out = _open_panel_tool({"panel": "kitchen"})
        assert out.startswith("ERROR")
        assert "kitchen" in out

    def test_open_empty_panel_refused(self):
        assert _open_panel_tool({}).startswith("ERROR")

    def test_panel_name_case_and_whitespace_normalized(self):
        out = _open_panel_tool({"panel": "  MAP  "})
        assert _json_tail(out)["panel"] == "map"

    def test_close_valid_panel(self):
        out = _close_panel_tool({"panel": "globe"})
        assert _json_tail(out) == {"action": "close", "panel": "globe"}

    def test_close_unknown_panel_refused(self):
        assert _close_panel_tool({"panel": "nope"}).startswith("ERROR")


class TestMovePanel:
    def test_move_valid(self):
        out = _move_panel_tool({"panel": "chat", "x": 120, "y": 60})
        spec = _json_tail(out)
        assert spec == {"action": "move", "panel": "chat", "x": 120.0, "y": 60.0}

    def test_move_missing_coordinates_refused(self):
        assert _move_panel_tool({"panel": "chat", "x": 1}).startswith("ERROR")
        assert _move_panel_tool({"panel": "chat"}).startswith("ERROR")

    def test_move_non_numeric_coordinates_refused(self):
        out = _move_panel_tool({"panel": "chat", "x": "left", "y": 0})
        assert out.startswith("ERROR")

    def test_move_unknown_panel_refused_before_checking_coordinates(self):
        out = _move_panel_tool({"panel": "kitchen", "x": 1, "y": 1})
        assert out.startswith("ERROR")
        assert "kitchen" in out


class TestResizePanel:
    def test_resize_valid(self):
        out = _resize_panel_tool({"panel": "map", "width": 500, "height": 400})
        assert _json_tail(out) == {"action": "resize", "panel": "map", "width": 500.0, "height": 400.0}

    def test_resize_below_real_client_minimum_refused_honestly(self):
        # ui/workspace.html's setPanelRect() clamps to 260x180 -- a smaller
        # request must be refused here, not silently returned as if it had
        # been honored at the requested (too-small) size.
        out = _resize_panel_tool({"panel": "map", "width": 50, "height": 50})
        assert out.startswith("ERROR")
        assert "260" in out and "180" in out

    def test_resize_missing_dimensions_refused(self):
        assert _resize_panel_tool({"panel": "map", "width": 300}).startswith("ERROR")

    def test_resize_non_numeric_dimensions_refused(self):
        out = _resize_panel_tool({"panel": "map", "width": "big", "height": 300})
        assert out.startswith("ERROR")


class TestListPanelTypes:
    def test_lists_all_six_with_descriptions(self):
        out = _list_panel_types_tool({})
        for panel in PANEL_IDS:
            assert panel + ":" in out


class TestToolSpecsRegistration:
    def test_six_tools_all_regular_permission(self):
        specs = build_workspace_panel_tool_specs()
        names = {s.name for s in specs}
        assert names == {
            "list_panel_types", "open_panel", "close_panel",
            "move_panel", "resize_panel", "organize_panels",
        }
        # Panel layout is not data, money, or a persistent write -- none of
        # these should carry a confirmation gate.
        for s in specs:
            assert s.permission == Permission.REGULAR, f"{s.name} should not require confirmation"


class TestOrganizePanels:
    def test_returns_the_real_organize_spec(self):
        out = _organize_panels_tool({})
        spec = _json_tail(out)
        assert spec == {"action": "organize"}

    def test_panel_control_subagent_registered_in_general_roster(self):
        registry = general_roster.build_general_registry()
        assert "panel_control" in registry.subagent_names
        sub = registry.get_subagent("panel_control")
        tool_names = {t.name for t in sub.tools}
        assert "move_panel" in tool_names
        assert "resize_panel" in tool_names

    def test_companion_prompt_names_panel_control(self):
        from dourmouse.agent_prompts import AGENT_SYSTEM_PROMPTS

        assert "panel_control" in AGENT_SYSTEM_PROMPTS["companion"]
