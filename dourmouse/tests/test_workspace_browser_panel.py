"""ui/workspace.html — LIVE BROWSER panel (Vision OS "Autonomous Headless
Browser & Live DOM Navigation Engine") and the generic panel onClose
teardown hook it needed.

The panel itself (loadBrowserPanel) is DOM/fetch-heavy UI wiring, same
category as loadMailPanel/loadUploadPanel — this codebase's own
established convention (see test_workspace_panel_control.py) doesn't
unit-test that class of function at the execution level; it was instead
live-verified directly in a real browser against the real running server
this session (real /api/browser/status, /api/browser/activity,
/api/browser/screenshot calls, all 200 OK, real screenshot rendered, and
confirmed the polling interval actually stops on panel close).

What IS real, small, and directly testable here is the new generic
mechanism this panel needed: closePanel()'s onClose teardown hook (any
panel with a live interval can now stop it on close, not just this one).
Extracted verbatim and executed in a real Node harness, same discipline
as this codebase's other workspace.html tests.
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


def _extract_inline_script() -> str:
    html = _WORKSPACE_HTML.read_text(encoding="utf-8")
    m = re.search(r"<script>(.*?)</script>", html, re.S)
    assert m, "ui/workspace.html has no inline <script>...</script> block"
    return m.group(1)


def _extract_function(script: str, name: str) -> str:
    m = re.search(rf"function {name}\(.*?\n\}}\n", script, re.S)
    assert m, f"{name}() not found in ui/workspace.html's inline script"
    return m.group(0)


def _run(tmp_path, js_tail: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH in this environment")
    script = _extract_inline_script()
    closePanel_src = _extract_function(script, "closePanel")
    harness = f"""
// ---- fake panels Map + DOM element (not extracted -- test scaffolding) ----
const panels = new Map();
let focusedId = null;
function makePanel(id, onClose){{
  const el = {{ removed: false, remove(){{ this.removed = true; }} }};
  const p = {{ id, el, onClose }};
  panels.set(id, p);
  return p;
}}

{closePanel_src}

{js_tail}
"""
    js_file = tmp_path / "close_panel.js"
    js_file.write_text(harness, encoding="utf-8")
    result = subprocess.run([node, str(js_file)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


class TestClosePanelOnCloseHook:
    def test_onclose_is_called_when_present(self, tmp_path):
        tail = """
let calls = 0;
const p = makePanel("a", () => { calls++; });
closePanel("a");
console.log(JSON.stringify({ calls, removed: p.el.removed, stillTracked: panels.has("a") }));
"""
        out = _run(tmp_path, tail)
        assert out == {"calls": 1, "removed": True, "stillTracked": False}

    def test_no_onclose_is_a_safe_noop(self, tmp_path):
        tail = """
const p = makePanel("a", undefined);
closePanel("a");
console.log(JSON.stringify({ removed: p.el.removed, stillTracked: panels.has("a") }));
"""
        out = _run(tmp_path, tail)
        assert out == {"removed": True, "stillTracked": False}

    def test_a_raising_onclose_never_blocks_real_panel_removal(self, tmp_path):
        tail = """
const p = makePanel("a", () => { throw new Error("interval cleanup exploded"); });
closePanel("a");
console.log(JSON.stringify({ removed: p.el.removed, stillTracked: panels.has("a") }));
"""
        out = _run(tmp_path, tail)
        assert out == {"removed": True, "stillTracked": False}

    def test_closing_an_unknown_id_never_raises(self, tmp_path):
        tail = """
closePanel("does-not-exist");
console.log(JSON.stringify({ ok: true }));
"""
        out = _run(tmp_path, tail)
        assert out == {"ok": True}
