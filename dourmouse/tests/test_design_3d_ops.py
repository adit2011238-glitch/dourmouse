"""3D & UI Design agent tests (design_3d_ops.py + roster wiring).

Hermetic (Rule 2.1): every manifest read/write happens against a tmp dir
via DOURMOUSE_WORKSPACE or an explicit manifest_path — nothing here ever
touches a real workspace file, and D:\\spatial_ai_library\\ is never
referenced (this suite runs on the Mac; the desktop is out of reach).

Covered:
- the roster carries the design_3d subagent with all five tools
- generate_ui_component_spec: valid input, validation errors (missing
  fields, non-positive dimensions, out-of-range opacity)
- generate_3d_model_spec: valid primitive composition, validation errors,
  and the explicit "not a real mesh" disclaimer text
- list_manifest / read_manifest_entry against an empty workspace (honest
  "no file yet"), then against a manifest with real entries
- write_manifest_entry is REQUIRES_CONFIRMATION and actually persists JSON
  matching the exact ui_manifest.json shape; a denied confirmation writes
  nothing
- manifest_path is configurable per call, and DOURMOUSE_UI_MANIFEST_PATH
  overrides the default when no manifest_path is given
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dourmouse.design_3d_ops import (
    _build_component_entry,
    _manifest_path,
    build_design_3d_tool_specs,
)
from dourmouse.dispatch import Permission, _execute_tool
from dourmouse.general_roster import build_general_registry


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(ws))
    monkeypatch.delenv("DOURMOUSE_UI_MANIFEST_PATH", raising=False)
    return ws


def _tool(name: str):
    specs = {t.name: t for t in build_design_3d_tool_specs()}
    return specs[name]


class TestRosterWiring:
    def test_design_3d_registered_with_all_tools(self):
        registry = build_general_registry()
        sub = registry.get_subagent("design_3d")
        assert sub is not None
        names = {t.name for t in sub.tools}
        assert {
            "generate_ui_component_spec",
            "generate_3d_model_spec",
            "list_manifest",
            "read_manifest_entry",
            "write_manifest_entry",
        } <= names

    def test_write_manifest_entry_requires_confirmation(self):
        registry = build_general_registry()
        sub = registry.get_subagent("design_3d")
        spec = next(t for t in sub.tools if t.name == "write_manifest_entry")
        assert spec.permission is Permission.REQUIRES_CONFIRMATION
        assert spec.confirm_prompt is not None

    def test_other_four_tools_are_regular(self):
        registry = build_general_registry()
        sub = registry.get_subagent("design_3d")
        for t in sub.tools:
            if t.name == "write_manifest_entry" or t.name == "query_shared_memory":
                continue
            assert t.permission is Permission.REGULAR, t.name


class TestGenerateUiComponentSpec:
    def test_valid_component_matches_ui_manifest_shape(self, workspace):
        spec = _tool("generate_ui_component_spec")
        out = spec.handler({
            "name": "glass_hud_panel",
            "category": "panel",
            "description": "Translucent heads-up display panel",
            "width": 320,
            "height": 180,
            "color": "#7ea9c9",
            "opacity": 0.85,
        })
        assert "UI COMPONENT SPEC" in out
        payload = json.loads(out.split("\n", 1)[1])
        assert payload == {
            "glass_hud_panel": {
                "category": "panel",
                "description": "Translucent heads-up display panel",
                "dimensions": {"width": 320.0, "height": 180.0},
                "color": "#7ea9c9",
                "opacity": 0.85,
            }
        }

    def test_defaults_color_and_opacity(self, workspace):
        spec = _tool("generate_ui_component_spec")
        out = spec.handler({
            "name": "x", "category": "toolbar", "description": "d",
            "width": 10, "height": 10,
        })
        payload = json.loads(out.split("\n", 1)[1])
        assert payload["x"]["color"] == "#888888"
        assert payload["x"]["opacity"] == 1.0

    def test_missing_name_errors(self, workspace):
        spec = _tool("generate_ui_component_spec")
        out = spec.handler({"category": "panel", "description": "d", "width": 1, "height": 1})
        assert out.startswith("ERROR:")
        assert "name" in out

    def test_non_positive_dimensions_error(self, workspace):
        spec = _tool("generate_ui_component_spec")
        out = spec.handler({
            "name": "x", "category": "panel", "description": "d", "width": 0, "height": 10,
        })
        assert out.startswith("ERROR:")
        assert "positive" in out

    def test_opacity_out_of_range_errors(self, workspace):
        spec = _tool("generate_ui_component_spec")
        out = spec.handler({
            "name": "x", "category": "panel", "description": "d",
            "width": 1, "height": 1, "opacity": 1.5,
        })
        assert out.startswith("ERROR:")
        assert "opacity" in out.lower() or "0 and 1" in out


class TestGenerate3dModelSpec:
    def test_valid_primitive_composition(self, workspace):
        spec = _tool("generate_3d_model_spec")
        out = spec.handler({
            "name": "toy_lamp",
            "description": "A simple desk lamp",
            "primitives": [
                {"type": "cylinder", "position": [0, 0, 0], "scale": 1,
                 "material": {"color": "#333333"}},
                {"type": "sphere", "position": [0, 1, 0], "scale": [0.5, 0.5, 0.5],
                 "material": {"color": "#ffdd88", "roughness": 0.2}},
            ],
        })
        assert "NOT real mesh/CAD geometry" in out
        assert "out of scope" in out.lower() or "OUT OF SCOPE" in out
        payload = json.loads(out.split("\n", 1)[1])
        assert payload["name"] == "toy_lamp"
        assert len(payload["primitives"]) == 2
        assert payload["primitives"][0]["scale"] == [1.0, 1.0, 1.0]  # uniform-scale expansion
        assert payload["primitives"][1]["material"]["roughness"] == 0.2

    def test_missing_primitives_errors(self, workspace):
        spec = _tool("generate_3d_model_spec")
        out = spec.handler({"name": "x"})
        assert out.startswith("ERROR:")
        assert "primitives" in out

    def test_bad_primitive_type_errors(self, workspace):
        spec = _tool("generate_3d_model_spec")
        out = spec.handler({"name": "x", "primitives": [{"type": "teapot"}]})
        assert out.startswith("ERROR:")
        assert "teapot" in out

    def test_bad_position_errors(self, workspace):
        spec = _tool("generate_3d_model_spec")
        out = spec.handler({"name": "x", "primitives": [{"type": "box", "position": [0, 0]}]})
        assert out.startswith("ERROR:")
        assert "position" in out


class TestManifestListReadWrite:
    def test_list_manifest_honest_when_missing(self, workspace):
        spec = _tool("list_manifest")
        out = spec.handler({})
        assert "no file at" in out

    def test_read_manifest_entry_missing_file_errors(self, workspace):
        spec = _tool("read_manifest_entry")
        out = spec.handler({"name": "glass_hud_panel"})
        assert out.startswith("ERROR:")

    def test_write_then_list_then_read(self, workspace):
        write_spec = _tool("write_manifest_entry")
        approved = lambda prompt: True
        out = _execute_tool(write_spec, {
            "name": "glass_hud_panel",
            "category": "panel",
            "description": "HUD panel",
            "width": 320,
            "height": 180,
            "color": "#7ea9c9",
            "opacity": 0.85,
        }, approved)
        assert out.startswith("ADDED")

        manifest_file = workspace / "design_3d" / "ui_manifest.json"
        assert manifest_file.is_file()
        data = json.loads(manifest_file.read_text())
        assert data["glass_hud_panel"]["category"] == "panel"

        list_out = _tool("list_manifest").handler({})
        assert "glass_hud_panel" in list_out
        assert "1 entries" in list_out

        read_out = _tool("read_manifest_entry").handler({"name": "glass_hud_panel"})
        payload = json.loads(read_out)
        assert payload["glass_hud_panel"]["dimensions"] == {"width": 320.0, "height": 180.0}

    def test_write_denied_confirmation_writes_nothing(self, workspace):
        write_spec = _tool("write_manifest_entry")
        denied = lambda prompt: False
        out = _execute_tool(write_spec, {
            "name": "x", "category": "panel", "description": "d",
            "width": 1, "height": 1,
        }, denied)
        assert out.startswith("DECLINED BY USER:")
        manifest_file = workspace / "design_3d" / "ui_manifest.json"
        assert not manifest_file.exists()

    def test_write_overwrite_reports_updated(self, workspace):
        write_spec = _tool("write_manifest_entry")
        approved = lambda prompt: True
        args = {
            "name": "x", "category": "panel", "description": "d",
            "width": 1, "height": 1,
        }
        _execute_tool(write_spec, args, approved)
        out2 = _execute_tool(write_spec, args, approved)
        assert out2.startswith("UPDATED")

    def test_explicit_manifest_path_overrides_workspace(self, workspace, tmp_path):
        custom = tmp_path / "elsewhere" / "ui_manifest.json"
        write_spec = _tool("write_manifest_entry")
        approved = lambda prompt: True
        _execute_tool(write_spec, {
            "name": "x", "category": "panel", "description": "d",
            "width": 1, "height": 1, "manifest_path": str(custom),
        }, approved)
        assert custom.is_file()
        default_file = workspace / "design_3d" / "ui_manifest.json"
        assert not default_file.exists()

    def test_env_var_overrides_default_when_no_explicit_path(self, workspace, tmp_path, monkeypatch):
        custom = tmp_path / "spatial_ai_library" / "ui_components" / "ui_manifest.json"
        monkeypatch.setenv("DOURMOUSE_UI_MANIFEST_PATH", str(custom))
        write_spec = _tool("write_manifest_entry")
        approved = lambda prompt: True
        _execute_tool(write_spec, {
            "name": "x", "category": "panel", "description": "d",
            "width": 1, "height": 1,
        }, approved)
        assert custom.is_file()

    def test_manifest_path_resolution_priority(self, workspace, tmp_path, monkeypatch):
        env_path = tmp_path / "env" / "ui_manifest.json"
        monkeypatch.setenv("DOURMOUSE_UI_MANIFEST_PATH", str(env_path))
        explicit_path = tmp_path / "explicit" / "ui_manifest.json"
        resolved = _manifest_path({"manifest_path": str(explicit_path)})
        assert resolved == explicit_path
        resolved_env = _manifest_path({})
        assert resolved_env == env_path


class TestBuildComponentEntry:
    def test_raises_on_missing_category(self):
        with pytest.raises(ValueError, match="category"):
            _build_component_entry({"name": "x", "description": "d", "width": 1, "height": 1})


class TestToolDescriptions:
    """Guards the tightened tool descriptions stay honest and non-generic."""

    def test_generate_ui_component_spec_gives_category_guidance(self):
        spec = _tool("generate_ui_component_spec")
        assert "category" in spec.description.lower()
        # not just "a string" -- gives the model real examples to anchor on
        assert "panel" in spec.description.lower()

    def test_read_manifest_entry_describes_path_resolution_and_honesty(self):
        spec = _tool("read_manifest_entry")
        # was a one-line stub ("Read ONE entry from the UI manifest by
        # name."); must now match its siblings' level of detail on path
        # resolution and honest-error behavior.
        assert "manifest_path" in spec.description
        assert "DOURMOUSE_UI_MANIFEST_PATH" in spec.description
        assert "error" in spec.description.lower()

    def test_generate_3d_model_spec_still_disclaims_mesh_output(self):
        spec = _tool("generate_3d_model_spec")
        assert "mesh" in spec.description.lower()
        assert ".obj" in spec.description and ".glb" in spec.description

    def test_write_manifest_entry_still_warns_about_silent_overwrite(self):
        spec = _tool("write_manifest_entry")
        assert "overwrite" in spec.description.lower()
        assert "confirmation" in spec.description.lower()
