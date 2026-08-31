"""ui/workspace.html — gaze-assisted attention focus (Vision OS checklist
item 3: "runs entirely on-device using your webcam... dynamically
applying CSS backdrop filters... to peripheral application cards when
you focus intensely on a central workspace window").

DIAGNOSIS/DESIGN CHOICE this covers: MediaPipe FaceLandmarker's
facialTransformationMatrixes output could give a real yaw/pitch, but
extracting it reliably needs an exact row/column-major + axis-sign
convention this sandbox has no live camera to verify against a real
face — guessing that would be exactly the "never guess a wire protocol
un-verified" mistake code_backends.py's own stream_claude docstring
warns against. Instead, computeYawRatio/computeGazeState use plain
landmark-ratio geometry (nose tip vs. the midpoint between the two face-
boundary landmarks, normalized by face width) — the same KIND of
already-proven approach handPinchState() uses for pinch detection
(test_workspace_hand_gestures.py), just applied to a different landmark
set.

HOW THIS IS VERIFIED (same honest limit as test_workspace_hand_gestures.py
and test_workspace_vision_backdrop.py): the real pure functions are
extracted verbatim out of ui/workspace.html's inline script and actually
EXECUTED in a real Node subprocess against fabricated 478-point Face
Mesh-shaped landmark arrays — not just syntax-checked. What this does
NOT and cannot prove: that this feels right against a REAL live camera
and a real human face turning their head — only a live desktop/browser
session with real camera permission can confirm that.
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

_GAZE_FUNCS = ["computeYawRatio", "computeGazeState"]
_GAZE_CONSTS = ["GAZE_LM", "GAZE_ENGAGE_RATIO", "GAZE_RELEASE_RATIO"]


def _extract_inline_script() -> str:
    html = _WORKSPACE_HTML.read_text(encoding="utf-8")
    m = re.search(r"<script>(.*?)</script>", html, re.S)
    assert m, "ui/workspace.html has no inline <script>...</script> block"
    return m.group(1)


def _extract_function(script: str, name: str) -> str:
    m = re.search(rf"function {name}\(.*?\n\}}\n", script, re.S)
    assert m, f"{name}() not found in ui/workspace.html's inline script"
    return m.group(0)


def _extract_const(script: str, name: str) -> str:
    m = re.search(rf"const {name} = [^;]+;", script)
    assert m, f"const {name} not found in ui/workspace.html's inline script"
    return m.group(0)


def _harness_source() -> str:
    script = _extract_inline_script()
    parts = [_extract_const(script, c) for c in _GAZE_CONSTS]
    parts += [_extract_function(script, f) for f in _GAZE_FUNCS]
    return "\n".join(parts)


def _make_face(nose_x, left_x, right_x, y=0.5):
    """A 478-point landmark array shaped like a real MediaPipe
    FaceLandmarker frame. Only the three canonical indices computeYawRatio
    actually reads (1=nose tip, 234/454=left/right face boundary) matter
    to the math under test; the rest are filler so the array is the real
    478-point length."""
    lm = [{"x": 0.5, "y": 0.5} for _ in range(478)]
    lm[1] = {"x": nose_x, "y": y}
    lm[234] = {"x": left_x, "y": y}
    lm[454] = {"x": right_x, "y": y}
    return lm


def _run(tmp_path, js_tail: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH in this environment")
    js_file = tmp_path / "gaze.js"
    js_file.write_text(_harness_source() + "\n" + js_tail, encoding="utf-8")
    result = subprocess.run([node, str(js_file)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


class TestComputeYawRatio:
    def test_face_dead_center_is_near_zero(self, tmp_path):
        # Nose exactly at the midpoint between the two face-boundary
        # landmarks -- facing the camera head-on.
        lm = _make_face(nose_x=0.5, left_x=0.3, right_x=0.7)
        out = _run(tmp_path, f"console.log(JSON.stringify(computeYawRatio({json.dumps(lm)})));")
        assert out == pytest.approx(0.0, abs=1e-9)

    def test_turned_right_gives_a_real_signed_ratio(self, tmp_path):
        # Nose shifted toward the right boundary (real geometry: turning
        # your head moves the nose tip off-center relative to the ears).
        lm = _make_face(nose_x=0.6, left_x=0.3, right_x=0.7)
        out = _run(tmp_path, f"console.log(JSON.stringify(computeYawRatio({json.dumps(lm)})));")
        # (0.6 - 0.5) / 0.4 == 0.25
        assert out == pytest.approx(0.25, abs=1e-9)

    def test_turned_left_gives_the_opposite_sign(self, tmp_path):
        lm = _make_face(nose_x=0.4, left_x=0.3, right_x=0.7)
        out = _run(tmp_path, f"console.log(JSON.stringify(computeYawRatio({json.dumps(lm)})));")
        assert out == pytest.approx(-0.25, abs=1e-9)

    def test_normalized_by_face_width_not_absolute_distance(self, tmp_path):
        # Same RELATIVE offset, a face twice as wide (closer to camera) --
        # the ratio must come out identical, same robustness handPinchState
        # gets from normalizing by hand scale.
        near = _make_face(nose_x=0.6, left_x=0.3, right_x=0.7)
        far = _make_face(nose_x=0.55, left_x=0.4, right_x=0.6)
        out_near = _run(tmp_path, f"console.log(JSON.stringify(computeYawRatio({json.dumps(near)})));")
        out_far = _run(tmp_path, f"console.log(JSON.stringify(computeYawRatio({json.dumps(far)})));")
        assert out_near == pytest.approx(out_far, abs=1e-9)


class TestComputeGazeState:
    def test_dead_center_engages_gaze_from_not_gazing(self, tmp_path):
        lm = _make_face(nose_x=0.5, left_x=0.3, right_x=0.7)
        out = _run(
            tmp_path,
            f"console.log(JSON.stringify(computeGazeState({json.dumps(lm)}, false)));",
        )
        assert out["gazing"] is True

    def test_hard_turn_never_engages_gaze(self, tmp_path):
        lm = _make_face(nose_x=0.68, left_x=0.3, right_x=0.7)  # yawRatio ~0.45
        out = _run(
            tmp_path,
            f"console.log(JSON.stringify(computeGazeState({json.dumps(lm)}, false)));",
        )
        assert out["gazing"] is False

    def test_hysteresis_a_mid_range_yaw_stays_gazing_once_engaged(self, tmp_path):
        # Real Schmitt-trigger check, same shape as PINCH_ENGAGE/RELEASE's
        # own tests: a yaw ratio between the two thresholds must NOT drop
        # gaze if it was already engaged (wasGazing=true), but must NOT
        # engage it fresh from a not-gazing state either.
        lm = _make_face(nose_x=0.56, left_x=0.3, right_x=0.7)  # yawRatio = 0.15
        still_gazing = _run(
            tmp_path,
            f"console.log(JSON.stringify(computeGazeState({json.dumps(lm)}, true)));",
        )
        fresh_not_gazing = _run(
            tmp_path,
            f"console.log(JSON.stringify(computeGazeState({json.dumps(lm)}, false)));",
        )
        assert still_gazing["gazing"] is True
        assert fresh_not_gazing["gazing"] is False

    def test_noise_near_the_boundary_cannot_flicker(self, tmp_path):
        # The actual bug class this hysteresis exists to prevent (same
        # rationale as the pinch Schmitt trigger): a yaw ratio that
        # oscillates JUST inside/outside GAZE_ENGAGE_RATIO must not toggle
        # gazing every frame once already engaged, as long as it never
        # crosses the wider RELEASE threshold.
        wobble_in = _make_face(nose_x=0.505, left_x=0.3, right_x=0.7)   # ~0.0125
        wobble_out = _make_face(nose_x=0.552, left_x=0.3, right_x=0.7)  # ~0.13, > ENGAGE, < RELEASE
        state = True
        for lm in [wobble_in, wobble_out, wobble_in, wobble_out]:
            out = _run(
                tmp_path,
                f"console.log(JSON.stringify(computeGazeState({json.dumps(lm)}, {json.dumps(state)})));",
            )
            assert out["gazing"] is True
            state = out["gazing"]
