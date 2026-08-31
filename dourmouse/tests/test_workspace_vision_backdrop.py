"""ui/workspace.html's drawHandOverlay() — the visual half of the v13.5
"hand is not on the titlebar" fix (see the #visBackdrop CSS comment and
drawHandOverlay's own comment in ui/workspace.html for the full
diagnosis).

Before this fix, the debug overlay drew raw, un-mirrored MediaPipe
landmark coordinates onto a tiny ~230px canvas confined to the HAND
CONTROL dock, while the REAL hit-test (handPinchState(), covered by
test_workspace_hand_gestures.py) mapped the exact same landmarks across
the whole window, mirrored. The two coordinate spaces had zero
relationship — what a user saw in the little preview box told them
nothing about where the code thought their hand actually was. The fix
makes the camera fill the viewport and draws the overlay in the SAME
mirrored, full-window page-pixel space handPinchState() hit-tests
against: (1 - x) * window.innerWidth, y * window.innerHeight.

Verified the same way test_workspace_hand_gestures.py verifies the pure
gesture math: the real drawHandOverlay() function is extracted verbatim
out of ui/workspace.html's inline script and actually executed in a real
Node subprocess against a fake canvas 2D context that records every
arc()/clearRect() call, fabricated landmark frames, and a fake `window`
— not just syntax-checked. What this does NOT and cannot prove (same
honest limit as the gesture-math tests): that this looks/feels right
against a REAL live camera and a real human hand — only a live
desktop/browser session with real camera permission can confirm that.
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


def _run(tmp_path, *, view_w=1000, view_h=800, landmarks=None):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH in this environment")
    script = _extract_inline_script()
    fn_src = _extract_function(script, "drawHandOverlay")

    landmarks_js = json.dumps(landmarks) if landmarks is not None else "null"
    harness = f"""
// ---- fake DOM/canvas scaffolding (not extracted from ui/workspace.html) ----
const calls = {{ arcs: [], clearRects: [], widthSets: [], heightSets: [] }};
const ctx = {{
  fillStyle: null,
  clearRect(x, y, w, h) {{ calls.clearRects.push([x, y, w, h]); }},
  beginPath() {{}},
  arc(x, y, r) {{ calls.arcs.push([x, y, r]); }},
  fill() {{}},
}};
let _w = 480, _h = 270;
const fakeCanvas = {{
  get width() {{ return _w; }},
  set width(v) {{ _w = v; calls.widthSets.push(v); }},
  get height() {{ return _h; }},
  set height(v) {{ _h = v; calls.heightSets.push(v); }},
  getContext(_kind) {{ return ctx; }},
}};
const window = {{ innerWidth: {view_w}, innerHeight: {view_h} }};
const HAND = {{ overlay: fakeCanvas }};

{fn_src}

const res = {landmarks_js} === null ? null : {{ landmarks: [{landmarks_js}] }};
drawHandOverlay(res);
console.log(JSON.stringify({{
  width: fakeCanvas.width, height: fakeCanvas.height,
  clearRects: calls.clearRects, arcs: calls.arcs,
}}));
"""
    js_file = tmp_path / "draw_overlay.js"
    js_file.write_text(harness, encoding="utf-8")
    result = subprocess.run([node, str(js_file)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


class TestDrawHandOverlaySyncedToFullWindow:
    def test_canvas_is_sized_to_the_real_viewport_not_the_video_native_resolution(self, tmp_path):
        out = self._run_no_landmarks(tmp_path, view_w=1440, view_h=900)
        assert out["width"] == 1440
        assert out["height"] == 900

    def _run_no_landmarks(self, tmp_path, **kw):
        return _run(tmp_path, landmarks=None, **kw)

    def test_clears_the_full_canvas_every_frame(self, tmp_path):
        out = _run(tmp_path, view_w=1000, view_h=800, landmarks=None)
        assert out["clearRects"] == [[0, 0, 1000, 800]]
        assert out["arcs"] == []  # no result -> nothing drawn, but still cleared

    def test_a_landmark_is_plotted_mirrored_in_real_page_pixels(self, tmp_path):
        # The exact mirroring convention handPinchState() uses:
        # x_page = (1 - x_normalized) * window.innerWidth
        # y_page = y_normalized * window.innerHeight
        # A landmark at normalized (0.2, 0.75) in a 1000x800 viewport must
        # land at page pixel (800, 600) -- the SAME point the real pinch/
        # drag hit-test would compute for that same raw landmark.
        out = _run(tmp_path, view_w=1000, view_h=800, landmarks=[{"x": 0.2, "y": 0.75}])
        assert len(out["arcs"]) == 1
        x, y, r = out["arcs"][0]
        assert x == pytest.approx(800.0)
        assert y == pytest.approx(600.0)
        assert r > 0

    def test_center_landmark_maps_to_center_regardless_of_mirroring(self, tmp_path):
        # A sanity check independent of the mirror-direction reasoning above:
        # dead-center (0.5, 0.5) must land at dead-center in page space too,
        # since (1-0.5) == 0.5 -- mirroring a symmetric point is a no-op.
        out = _run(tmp_path, view_w=1200, view_h=600, landmarks=[{"x": 0.5, "y": 0.5}])
        x, y, _r = out["arcs"][0]
        assert x == pytest.approx(600.0)
        assert y == pytest.approx(300.0)

    def test_multiple_landmarks_in_one_hand_are_all_plotted(self, tmp_path):
        lm = [{"x": 0.0, "y": 0.0}, {"x": 1.0, "y": 1.0}]
        out = _run(tmp_path, view_w=100, view_h=50, landmarks=lm)
        assert len(out["arcs"]) == 2
        # x=0.0 mirrors to the FAR edge (page x=100), x=1.0 mirrors to 0.
        pts = sorted((a[0], a[1]) for a in out["arcs"])
        assert pts[0] == pytest.approx((0.0, 50.0))
        assert pts[1] == pytest.approx((100.0, 0.0))
