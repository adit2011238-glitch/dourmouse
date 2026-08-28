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

import http.client
import json
import threading
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


class TestWriteManifestEntry3DModel:
    """v(world-monitor-expansion): write_manifest_entry's second shape —
    a non-empty 'primitives' array persists a 3d_model-kind entry into the
    SAME manifest file, for the interactive 3D workspace's SAVE action to
    round-trip through."""

    def _lamp_args(self, **overrides):
        args = {
            "name": "toy_lamp",
            "description": "A simple desk lamp",
            "primitives": [
                {"type": "cylinder", "position": [0, 0, 0], "scale": 1,
                 "material": {"color": "#333333"}},
                {"type": "sphere", "position": [0, 1, 0], "scale": [0.5, 0.5, 0.5],
                 "material": {"color": "#ffdd88", "roughness": 0.2}},
            ],
        }
        args.update(overrides)
        return args

    def test_write_3d_model_then_list_then_read(self, workspace):
        write_spec = _tool("write_manifest_entry")
        approved = lambda prompt: True
        out = _execute_tool(write_spec, self._lamp_args(), approved)
        assert out.startswith("ADDED")

        manifest_file = workspace / "design_3d" / "ui_manifest.json"
        data = json.loads(manifest_file.read_text())
        assert data["toy_lamp"]["kind"] == "3d_model"
        assert len(data["toy_lamp"]["primitives"]) == 2
        assert data["toy_lamp"]["primitives"][0]["scale"] == [1.0, 1.0, 1.0]
        assert "category" not in data["toy_lamp"]
        assert "dimensions" not in data["toy_lamp"]

        list_out = _tool("list_manifest").handler({})
        assert "toy_lamp" in list_out
        assert "3D MODEL" in list_out
        assert "2 primitive" in list_out

        read_out = _tool("read_manifest_entry").handler({"name": "toy_lamp"})
        payload = json.loads(read_out)
        assert payload["toy_lamp"]["primitives"][1]["material"]["roughness"] == 0.2

    def test_write_3d_model_missing_primitives_item_field_errors(self, workspace):
        write_spec = _tool("write_manifest_entry")
        approved = lambda prompt: True
        out = _execute_tool(write_spec, self._lamp_args(primitives=[{"type": "teapot"}]), approved)
        assert out.startswith("ERROR:")
        assert "teapot" in out

    def test_write_3d_model_missing_name_errors(self, workspace):
        write_spec = _tool("write_manifest_entry")
        approved = lambda prompt: True
        args = self._lamp_args()
        del args["name"]
        out = _execute_tool(write_spec, args, approved)
        assert out.startswith("ERROR:")
        assert "name" in out

    def test_confirm_prompt_names_3d_model_shape(self, workspace):
        write_spec = _tool("write_manifest_entry")
        prompt = write_spec.confirm_prompt(self._lamp_args())
        assert "3D model" in prompt

    def test_confirm_prompt_names_component_shape_when_no_primitives(self, workspace):
        write_spec = _tool("write_manifest_entry")
        prompt = write_spec.confirm_prompt({
            "name": "x", "category": "panel", "description": "d", "width": 1, "height": 1,
        })
        assert "component spec" in prompt

    def test_plain_ui_component_write_still_works_without_primitives_key(self, workspace):
        # Regression guard: omitting 'primitives' entirely must still take
        # the original UI-component path, unaffected by the new branch.
        write_spec = _tool("write_manifest_entry")
        approved = lambda prompt: True
        out = _execute_tool(write_spec, {
            "name": "glass_hud_panel", "category": "panel", "description": "HUD panel",
            "width": 320, "height": 180,
        }, approved)
        assert out.startswith("ADDED")
        manifest_file = workspace / "design_3d" / "ui_manifest.json"
        data = json.loads(manifest_file.read_text())
        assert data["glass_hud_panel"]["category"] == "panel"
        assert "kind" not in data["glass_hud_panel"]


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


# --------------------------------------------------------------------------- #
# Over-HTTP round trip: this pins the EXACT SSE contract the DESIGN screen's
# interactive workspaces (ui/console.html, dispatchDesignDirective /
# saveDesign3DModel / saveDesign2DComponent) depend on — a fake chat client
# stands in for the real model (no network, no local Ollama dependency;
# manual browser verification during development found the real local
# backend can be busy/unresponsive, which is an environment fact, not
# something a hermetic test should ever depend on). Same FakeClient/server
# pattern dourmouse/tests/test_webui.py already established for
# test_confirmation_requires_approval_over_http.
# --------------------------------------------------------------------------- #

class _FakeFunction:
    def __init__(self, name: str, arguments: str):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, call_id: str, name: str, arguments: str):
        self.id = call_id
        self.function = _FakeFunction(name, arguments)


class _FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, message: _FakeMessage):
        self.message = message


class _FakeResponse:
    def __init__(self, message: _FakeMessage):
        self.choices = [_FakeChoice(message)]


class _FakeCompletions:
    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls: list = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self._responses) == 1:
            return self._responses[0]
        return self._responses.pop(0)


class _FakeChat:
    def __init__(self, completions: _FakeCompletions):
        self.completions = completions


class _FakeClient:
    def __init__(self, responses: list):
        self.chat = _FakeChat(_FakeCompletions(responses))


@pytest.fixture
def http_server(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    from dourmouse.webui import run_server

    srv = run_server(build_general_registry(), port=0, client=None, config=None)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    port = srv.server_address[1]
    yield srv, port
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2)


def _stream_events(port, prompt, focus_agent="design_3d"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(
        "POST",
        "/api/chat",
        body=json.dumps({"prompt": prompt, "focus_agent": focus_agent}),
        headers={"Content-Type": "application/json"},
    )
    resp = conn.getresponse()
    assert resp.status == 200
    events = []
    confirm_id = None
    while True:
        line = resp.readline()
        if not line:
            break
        if not line.startswith(b"data: "):
            continue
        event = json.loads(line[6:])
        events.append(event)
        if event["type"] == "confirmation_requested":
            confirm_id = event["id"]
            break
    if confirm_id is not None:
        conn2 = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn2.request(
            "POST",
            "/api/confirm",
            body=json.dumps({"id": confirm_id, "approved": True}),
            headers={"Content-Type": "application/json"},
        )
        resp2 = conn2.getresponse()
        assert resp2.status == 200
        conn2.close()
        while True:
            line = resp.readline()
            if not line:
                break
            if line.startswith(b"data: "):
                events.append(json.loads(line[6:]))
    conn.close()
    return events


class TestDesignScreenOverHttp:
    def test_save_3d_model_tool_result_matches_frontend_parsing_contract(self, http_server):
        # Mirrors saveDesign3DModel()'s directive in ui/console.html: a
        # write_manifest_entry call carrying a 'primitives' array.
        srv, port = http_server
        payload = {
            "name": "toy_lamp",
            "description": "A simple desk lamp",
            "primitives": [
                {"type": "cylinder", "position": [0, 0, 0], "scale": [1, 1, 1],
                 "material": {"color": "#333333", "roughness": 0.5}},
            ],
        }
        srv.session.client = _FakeClient([
            _FakeResponse(_FakeMessage(
                content=None,
                tool_calls=[_FakeToolCall("c1", "write_manifest_entry", json.dumps(payload))],
            )),
            _FakeResponse(_FakeMessage(content="Saved.")),
        ])
        events = _stream_events(port, "save it", focus_agent="design_3d")
        types = [e["type"] for e in events]
        assert "confirmation_requested" in types
        assert "tool_result" in types
        assert "done" in types

        write_result = next(e for e in events if e["type"] == "tool_result" and e["name"] == "write_manifest_entry")
        # EXACTLY what saveDesign3DModel() does client-side: find the
        # first '{', JSON.parse the remainder, read parsed[name].primitives.
        raw = write_result["text"]
        assert raw.startswith("ADDED")
        parsed = json.loads(raw[raw.index("{"):])
        assert parsed["toy_lamp"]["kind"] == "3d_model"
        assert parsed["toy_lamp"]["primitives"] == payload["primitives"]

    def test_load_3d_model_read_result_matches_frontend_parsing_contract(self, http_server):
        # Pre-seed a 3d_model entry the way SAVE would have left it (same
        # DOURMOUSE_WORKSPACE the http_server fixture already set for this
        # test — the running server and this direct handler call resolve
        # the identical manifest path), then verify loadDesign3DModel()'s
        # read_manifest_entry parsing contract.
        srv, port = http_server
        write_spec = next(t for t in build_design_3d_tool_specs() if t.name == "write_manifest_entry")
        path = _manifest_path({})
        _execute_tool(write_spec, {
            "name": "toy_lamp", "description": "desk lamp",
            "primitives": [{"type": "sphere", "position": [0, 1, 0], "scale": 1,
                             "material": {"color": "#ffdd88"}}],
        }, lambda prompt: True)
        assert path.is_file()

        srv.session.client = _FakeClient([
            _FakeResponse(_FakeMessage(
                content=None,
                tool_calls=[_FakeToolCall("c1", "read_manifest_entry", json.dumps({"name": "toy_lamp"}))],
            )),
            _FakeResponse(_FakeMessage(content="Here it is.")),
        ])
        events = _stream_events(port, "load toy_lamp", focus_agent="design_3d")
        read_result = next(e for e in events if e["type"] == "tool_result" and e["name"] == "read_manifest_entry")
        # EXACTLY what loadDesign3DModel() does: JSON.parse the whole text,
        # then parsed[name].primitives.
        parsed = json.loads(read_result["text"])
        assert isinstance(parsed["toy_lamp"].get("primitives"), list)
        assert parsed["toy_lamp"]["primitives"][0]["type"] == "sphere"

    def test_save_ui_component_tool_result_matches_frontend_parsing_contract(self, http_server):
        # Mirrors saveDesign2DComponent()'s directive: write_manifest_entry
        # WITHOUT 'primitives' — the original component shape.
        srv, port = http_server
        payload = {
            "name": "glass_hud_panel", "category": "panel", "description": "HUD panel",
            "width": 320, "height": 180, "color": "#7ea9c9", "opacity": 0.85,
        }
        srv.session.client = _FakeClient([
            _FakeResponse(_FakeMessage(
                content=None,
                tool_calls=[_FakeToolCall("c1", "write_manifest_entry", json.dumps(payload))],
            )),
            _FakeResponse(_FakeMessage(content="Saved.")),
        ])
        events = _stream_events(port, "save it", focus_agent="design_3d")
        write_result = next(e for e in events if e["type"] == "tool_result" and e["name"] == "write_manifest_entry")
        raw = write_result["text"]
        parsed = json.loads(raw[raw.index("{"):])
        saved = parsed["glass_hud_panel"]
        # EXACTLY what saveDesign2DComponent()'s verification checks.
        assert saved["category"] == payload["category"]
        assert saved["dimensions"]["width"] == payload["width"]
        assert saved["dimensions"]["height"] == payload["height"]
        assert saved["color"] == payload["color"]
        assert "kind" not in saved
