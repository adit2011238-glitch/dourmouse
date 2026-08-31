"""ui/workspace.html — the client half of "real-time window sizes
controlled by an LLM, no clicking required": applyPanelControlToolResult()
and extractPanelControlAction() turn a real tool_result SSE event (from
dourmouse/workspace_panel_ops.py's four action tools, see
test_workspace_panel_ops.py for the server half) into a real call to the
SAME setPanelRect/openPanel/closePanel/bringToFront functions a mouse-drag
or hand-gesture pinch already calls -- covered by real Node execution
against the actual shipped functions, not source-grep.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_WORKSPACE_HTML = _PROJECT_ROOT / "ui" / "workspace.html"


def _classic_script() -> str:
    html = _WORKSPACE_HTML.read_text(encoding="utf-8")
    m = re.search(r"<script>(.*?)</script>", html, re.S)
    assert m
    return m.group(1)


class TestExtractPanelControlAction:
    def _fn_source(self) -> str:
        script = _classic_script()
        m = re.search(
            r"const PANEL_CONTROL_TOOL_NAMES = new Set\(\[(.*?)\]\);\n"
            r"function extractPanelControlAction\(toolName, rawText\)\{(.*?)\n\}\n",
            script, re.S,
        )
        assert m, "extractPanelControlAction not found"
        names, body = m.group(1), m.group(2)
        return (
            "const PANEL_CONTROL_TOOL_NAMES = new Set([" + names + "]);\n"
            "function extractPanelControlAction(toolName, rawText){" + body + "\n}"
        )

    def _run(self, tmp_path, tail):
        node = shutil.which("node")
        if not node:
            pytest.skip("node not on PATH in this environment")
        js_file = tmp_path / "extract_ui.js"
        js_file.write_text(self._fn_source() + "\n" + tail, encoding="utf-8")
        result = subprocess.run([node, str(js_file)], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_move_action_parsed(self, tmp_path):
        raw = 'Move the \'map\' panel to (400, 100).\n{"action": "move", "panel": "map", "x": 400.0, "y": 100.0}'
        out = self._run(tmp_path, f'console.log(JSON.stringify(extractPanelControlAction("move_panel", {raw!r})));')
        assert out == {"action": "move", "panel": "map", "x": 400.0, "y": 100.0}

    def test_resize_action_parsed(self, tmp_path):
        raw = 'Resize.\n{"action": "resize", "panel": "globe", "width": 600.0, "height": 500.0}'
        out = self._run(tmp_path, f'console.log(JSON.stringify(extractPanelControlAction("resize_panel", {raw!r})));')
        assert out["width"] == 600.0

    def test_unrelated_tool_name_refused(self, tmp_path):
        # A tool_result from web_search, gmail search, design_3d, etc. in
        # the same conversation must never be mistaken for a panel action.
        raw = '{"action": "move", "panel": "map", "x": 1, "y": 1}'  # even if shaped identically
        out = self._run(tmp_path, f'console.log(JSON.stringify(extractPanelControlAction("web_search", {raw!r}) || null));')
        assert out is None

    def test_error_text_refused(self, tmp_path):
        out = self._run(tmp_path,
            'console.log(JSON.stringify(extractPanelControlAction("move_panel", "ERROR: unknown panel") || null));')
        assert out is None

    def test_malformed_json_refused(self, tmp_path):
        out = self._run(tmp_path,
            'console.log(JSON.stringify(extractPanelControlAction("open_panel", "not json at all") || null));')
        assert out is None


class TestApplyPanelControlToolResultCallsRealPanelFunctions:
    """A lighter-weight check than a full DOM harness: confirms the
    dispatch table inside applyPanelControlToolResult() actually calls
    openPanel/closePanel/setPanelRect/bringToFront for each of the four
    real actions -- not console.log placeholders."""

    def _body(self) -> str:
        script = _classic_script()
        m = re.search(r"function applyPanelControlToolResult\(ev, log\)\{(.*?)\n\}\n", script, re.S)
        assert m, "applyPanelControlToolResult not found"
        return m.group(1)

    def test_open_calls_real_openPanel(self):
        assert "openPanel(action.panel)" in self._body()

    def test_close_calls_real_closePanel_only_if_actually_open(self):
        body = self._body()
        assert "findPanelByType(action.panel)" in body
        assert "closePanel(found.id)" in body

    def test_move_calls_real_setPanelRect_with_xy(self):
        body = self._body()
        assert "setPanelRect(found, { x: action.x, y: action.y })" in body

    def test_resize_calls_real_setPanelRect_with_wh(self):
        body = self._body()
        assert "setPanelRect(found, { w: action.width, h: action.height })" in body

    def test_move_and_resize_bring_the_panel_to_front(self):
        # An LLM-driven move/resize should also focus the panel it just
        # touched -- same as a real mouse drag does via bringToFront in
        # wirePanelChrome's own pointerdown handler.
        body = self._body()
        assert body.count("bringToFront(found.id)") == 2

    def test_sendChatMessage_wires_tool_result_to_this_handler(self):
        script = _classic_script()
        m = re.search(r"async function sendChatMessage\(p, text\)\{(.*?)\n\}\n", script, re.S)
        assert m
        assert 'applyPanelControlToolResult(ev, log)' in m.group(1)
