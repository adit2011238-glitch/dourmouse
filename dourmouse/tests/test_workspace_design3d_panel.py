"""ui/workspace.html — the 3D MODEL panel (window.Design3D), and the real
"AI can pull a 3D model with no clicking" path: a natural-language ASK box
wired to the real design_3d agent, exactly console.html's own
dispatchDesignDirective pipeline, ported here rather than reinvented.

The Design3D Three.js module itself is ported VERBATIM from
ui/console.html's DESIGN screen -- these tests confirm the port is
faithful (same public API surface) and confirm the two REAL, NEW pieces
built for this consolidation: extractDesign3DPrimitives() (turns a raw
tool_result string into loadable primitives, or refuses to when it isn't
one) and dispatchDesign3D()'s confirmation-gate handling for
write_manifest_entry (must never bypass the real Permission.
REQUIRES_CONFIRMATION gate design_3d_ops.py enforces).
"""

from __future__ import annotations

import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_WORKSPACE_HTML = _PROJECT_ROOT / "ui" / "workspace.html"


def _html() -> str:
    return _WORKSPACE_HTML.read_text(encoding="utf-8")


def _classic_script() -> str:
    m = re.search(r"<script>(.*?)</script>", _html(), re.S)
    assert m, "no classic <script> block found"
    return m.group(1)


def _module_script() -> str:
    m = re.search(r'<script type="module">(.*?)</script>', _html(), re.S)
    assert m, "no type=module <script> block found"
    return m.group(1)


class TestImportmapVendored:
    def test_three_resolves_to_the_locally_vendored_build_no_cdn(self):
        html = _html()
        assert '"three": "/assets/vendor/three/three.module.min.js"' in html
        assert '"three/addons/": "/assets/vendor/three/addons/"' in html
        assert "cdn" not in html.lower().split("importmap")[1][:200].lower()


class TestDesign3DDockButtonWired:
    def test_dock_button_present(self):
        assert '<button class="dockbtn" type="button" data-open="design3d">+ 3D MODEL</button>' in _html()

    def test_open_panel_routes_design3d_to_the_real_loader(self):
        m = re.search(r"function openPanel\(type\)\{(.*?)\n\}\n", _classic_script(), re.S)
        assert m
        body = m.group(1)
        assert 'design3d: "3D MODEL"' in body
        assert 'else if(type === "design3d") loadDesign3DPanel(p);' in body


class TestDesign3DModulePortedFaithfully:
    """The public API console.html's paintDesign3dWorkspace3D() actually
    calls must all still exist, unchanged in shape, in the ported copy."""

    def test_public_api_surface_matches(self):
        script = _module_script()
        m = re.search(r"window\.Design3D = \{(.*?)\n\};\n", script, re.S)
        assert m, "window.Design3D export not found"
        api = m.group(1)
        for fn in ("mount", "addPrimitive", "deleteSelected", "clear", "setMode",
                   "getSelected", "setSelectedPosition", "setSelectedScale",
                   "setSelectedColor", "getState", "loadState"):
            assert fn in api, f"Design3D.{fn} missing from ported module"

    def test_gizmo_and_orbit_are_real_not_decorative(self):
        script = _module_script()
        assert "new OrbitControls(" in script
        assert "new TransformControls(" in script
        assert "gizmo.setMode(state.mode)" in script

    def test_six_real_primitive_types_supported(self):
        script = _module_script()
        for t in ("box", "sphere", "cylinder", "cone", "plane", "torus"):
            assert f'case "{t}":' in script


class TestExtractDesign3DPrimitives:
    """Real Node execution against the actual shipped function -- not a
    source-grep -- covering the exact two real tool_result shapes it must
    handle (generate_3d_model_spec's flat {name,primitives}, and
    read_manifest_entry/write_manifest_entry's {name: {primitives}}), plus
    the refusal cases (error text, non-3D manifest entries)."""

    def _fn_source(self) -> str:
        script = _classic_script()
        m = re.search(r"function extractDesign3DPrimitives\(toolName, rawText\)\{(.*?)\n\}\n", script, re.S)
        assert m
        return "function extractDesign3DPrimitives(toolName, rawText){" + m.group(1) + "\n}"

    def _run(self, tmp_path, harness_tail):
        import json
        import shutil
        import subprocess

        import pytest as _pytest
        node = shutil.which("node")
        if not node:
            _pytest.skip("node not on PATH in this environment")
        js_file = tmp_path / "extract.js"
        js_file.write_text(self._fn_source() + "\n" + harness_tail, encoding="utf-8")
        result = subprocess.run([node, str(js_file)], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_generate_3d_model_spec_shape(self, tmp_path):
        raw = ('3D MODEL SPEC (primitive-composition level).\n' +
               '{"name": "toy_lamp", "description": "", "primitives": '
               '[{"type":"box","position":[0,0,0],"scale":[1,1,1],"material":{"color":"#fff","roughness":0.5}}]}')
        out = self._run(tmp_path,
            f'console.log(JSON.stringify(extractDesign3DPrimitives("generate_3d_model_spec", {raw!r})));')
        assert out["name"] == "toy_lamp"
        assert len(out["primitives"]) == 1
        assert out["primitives"][0]["type"] == "box"

    def test_read_manifest_entry_shape(self, tmp_path):
        raw = '{"toy_lamp": {"kind": "3d_model", "description": "", "primitives": [{"type":"sphere","position":[1,0,0],"scale":[1,1,1],"material":{"color":"#f00","roughness":0.5}}]}}'
        out = self._run(tmp_path,
            f'console.log(JSON.stringify(extractDesign3DPrimitives("read_manifest_entry", {raw!r})));')
        assert out["name"] == "toy_lamp"
        assert out["primitives"][0]["type"] == "sphere"

    def test_ui_component_entry_refused_not_a_3d_model(self, tmp_path):
        # read_manifest_entry can return a UI-COMPONENT entry (category/
        # width/height, no primitives) -- must return null, not crash or
        # silently wipe the live scene with garbage.
        raw = '{"glass_panel": {"category": "panel", "dimensions": {"width": 10, "height": 4}, "color": "#fff", "opacity": 0.8}}'
        out = self._run(tmp_path,
            f'console.log(JSON.stringify(extractDesign3DPrimitives("read_manifest_entry", {raw!r})));')
        assert out is None

    def test_error_text_refused(self, tmp_path):
        out = self._run(tmp_path,
            'console.log(JSON.stringify(extractDesign3DPrimitives("read_manifest_entry", "ERROR: no entry named x")));')
        assert out is None

    def test_irrelevant_tool_result_refused(self, tmp_path):
        # e.g. a tool_result from an unrelated tool call in the same turn
        # (list_manifest, web_search, ...) must never be mistaken for a
        # loadable 3D scene.
        out = self._run(tmp_path,
            'console.log(JSON.stringify(extractDesign3DPrimitives("list_manifest", "{\\"entries\\": [\\"a\\", \\"b\\"]}")));')
        assert out is None


class TestDispatchDesign3DNeverBypassesTheRealConfirmationGate:
    def test_confirmation_requested_renders_approve_decline_not_auto_approved(self):
        script = _classic_script()
        m = re.search(r"async function dispatchDesign3D\(directive, statusEl, opts\)\{(.*?)\n\}\n", script, re.S)
        assert m, "dispatchDesign3D not found"
        body = m.group(1)
        assert 'ev.type === "confirmation_requested"' in body
        assert "APPROVE" in body and "DECLINE" in body
        # both buttons must go through the real /api/confirm endpoint with
        # an explicit approved true/false -- never auto-resolved client-side.
        assert 'approved: true' in body
        assert 'approved: false' in body
        assert "/api/confirm" in body

    def test_uses_the_real_design_3d_focus_agent(self):
        script = _classic_script()
        m = re.search(r"async function dispatchDesign3D\(directive, statusEl, opts\)\{(.*?)\n\}\n", script, re.S)
        body = m.group(1)
        assert 'focus_agent: "design_3d"' in body


class TestAskBoxIsTheConversationalNoClickPath:
    def test_ask_input_and_button_exist(self):
        script = _classic_script()
        m = re.search(r"function loadDesign3DPanel\(p\)\{(.*?)\n\}\n", script, re.S)
        assert m
        body = m.group(1)
        assert 'id="d3AskInput"' in body
        assert 'id="d3AskGo"' in body
        assert "addEventListener(\"keydown\"" in body  # Enter submits, not click-only

    def test_tool_result_auto_loads_the_live_scene(self):
        # The actual "no clicking" behavior: a successful tool_result
        # calls Design3D.loadState() directly from the onToolResult
        # callback, with no intermediate manual "apply" button.
        script = _classic_script()
        m = re.search(r"const runAsk = \(\) => \{(.*?)\n  \};\n", script, re.S)
        assert m, "runAsk() not found"
        body = m.group(1)
        assert "window.Design3D.loadState(got.primitives)" in body

    def test_live_scene_state_is_sent_so_save_requests_have_something_real(self):
        script = _classic_script()
        m = re.search(r"const runAsk = \(\) => \{(.*?)\n  \};\n", script, re.S)
        body = m.group(1)
        assert "window.Design3D.getState()" in body
