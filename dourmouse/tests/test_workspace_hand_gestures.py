"""ui/workspace.html — hand-gesture window control: EMA smoothing +
pinch hysteresis, verified against a synthetic-landmark Node harness
(vision-hand-tracking-v2).

DIAGNOSIS this fixes: the hand-gesture window control (`handPinchState`,
`onHandFrame`, the two-hand pinch/resize/rotate and single-hand drag
logic) applied MediaPipe's raw, per-frame hand-landmark output directly to
window drag/resize/rotate with ZERO smoothing anywhere, and pinch
detection used a single distance threshold. Raw MediaPipe landmark output
is genuinely noisy frame-to-frame (especially at the 480x270 capture
resolution requested there), so this produced jittery drag/resize/rotate
and a `pinch` boolean that could flicker true/false near the threshold on
noise alone, starting and stopping drags erratically.

THE FIX (see ui/workspace.html, section 4): an EMA is applied to every raw
landmark point BEFORE any distance/midpoint math reads it
(`LANDMARK_SMOOTH_ALPHA`), and pinch detection is a Schmitt trigger with
two thresholds (`PINCH_ENGAGE_RATIO` / `PINCH_RELEASE_RATIO`) instead of
one.

HOW THIS IS VERIFIED (read this before assuming more than is built here,
same discipline as dourmouse/wakeword.py's own caveat): there is no live
camera in this sandbox (`navigator.mediaDevices.getUserMedia` raises
`NotAllowedError` here), so this cannot be confirmed against a real human
hand. What CAN be honestly verified, and what this file does, is
execution-level: the actual pure gesture-math functions
(`emaPoint`/`smoothLandmarks`/`handPinchState`/`computeTwoHandTransform`/
`computeDragTarget`) are extracted VERBATIM out of ui/workspace.html's
real inline script (regex, same technique test_console_session_restore.py
already uses for `node --check`) and actually EXECUTED in a real Node
subprocess against fabricated MediaPipe-shaped landmark sequences — not
just syntax-checked. The numbers in this file's assertions are real
output from that execution, printed by the test failures if anything
regresses. What this does NOT and CANNOT prove: that the fix FEELS right
in a real hand's hands — real, sustained camera jitter has a different
statistical character than any hand-picked synthetic sequence, and only a
live desktop/browser session with real camera permission can confirm the
felt experience. Treat this file as proof the diagnosed class of bug is
mathematically fixed, not as proof the UI now feels perfect.
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

# The pure gesture-math functions/constants pulled verbatim out of the
# real file for execution. If any of these disappear or get renamed,
# extraction fails loudly (assert) rather than silently testing stale code.
_GESTURE_FUNCS = [
    "emaPoint",
    "smoothLandmarks",
    "computeJitter",
    "handPinchState",
    "angleDelta",
    "computeTwoHandTransform",
    "computeDragTarget",
]
_GESTURE_CONSTS = ["LANDMARK_SMOOTH_ALPHA", "PINCH_ENGAGE_RATIO", "PINCH_RELEASE_RATIO"]


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


# ---------------------------------------------------------------------
# The synthetic-landmark driver. Everything above the "REAL SOURCE ENDS
# HERE" marker in the assembled harness is the actual extracted
# ui/workspace.html code; everything below is test scaffolding that
# fabricates MediaPipe-shaped landmark frames and drives the real
# functions with them, then prints one JSON object of real measurements.
# ---------------------------------------------------------------------
_SCENARIO_JS = r"""
// ==================== synthetic-landmark test scaffolding ====================
// Not extracted from ui/workspace.html — this is the test's own fixture code.

function makeHand(wrist, mcp9, thumb, index){
  // A 21-point landmark array shaped like a real MediaPipe HandLandmarker
  // frame. Only indices 0 (wrist), 9 (middle-finger MCP), 4 (thumb tip)
  // and 8 (index tip) matter to the gesture math under test; the rest are
  // filler so smoothLandmarks() still gets a full-length array to walk.
  const lm = new Array(21);
  for (let i = 0; i < 21; i++) lm[i] = { x: 0.5, y: 0.5 };
  lm[0] = wrist; lm[9] = mcp9; lm[4] = thumb; lm[8] = index;
  return lm;
}

const VIEW_W = 1000, VIEW_H = 1000;
const WRIST = { x: 0.5, y: 0.5 };
const MCP9 = { x: 0.5, y: 0.62 };
const HAND_SCALE = Math.hypot(WRIST.x - MCP9.x, WRIST.y - MCP9.y); // 0.12

function rotatePoint(p, center, thetaRad){
  const dx = p.x - center.x, dy = p.y - center.y;
  const c = Math.cos(thetaRad), s = Math.sin(thetaRad);
  return { x: center.x + dx * c - dy * s, y: center.y + dx * s + dy * c };
}

const out = {};

// ---- scenario 1: clean pinch-and-drag ----
// A single hand pinches (thumb+index co-located, distance 0 => unambiguous
// engage) and the pinch point moves in a straight line from normalized
// x=0.5 to x=0.3 over 30 frames, then holds for 40 more frames.
{
  const FRAMES_MOVE = 30, FRAMES_HOLD = 40;
  let smooth = null, wasPinching = false;
  let dragOffX = null, dragOffY = null;
  const PANEL_X0 = 500; // panel starts exactly under the pinch point
  const track = [];
  for (let f = 0; f <= FRAMES_MOVE + FRAMES_HOLD; f++) {
    const t = Math.min(f, FRAMES_MOVE) / FRAMES_MOVE;
    const px = 0.5 - 0.2 * t, py = 0.5; // normalized midpoint moves 0.5 -> 0.3
    const raw = makeHand(WRIST, MCP9, { x: px, y: py }, { x: px, y: py });
    smooth = smoothLandmarks(smooth, raw, LANDMARK_SMOOTH_ALPHA);
    const s = handPinchState(smooth, wasPinching, VIEW_W, VIEW_H);
    wasPinching = s.pinch;
    if (dragOffX === null) { dragOffX = s.x - PANEL_X0; dragOffY = s.y - 0; }
    const target = computeDragTarget(s, dragOffX, dragOffY);
    track.push({ frame: f, rawX: (1 - px) * VIEW_W, panelX: target.x, pinch: s.pinch });
  }
  out.drag = {
    startPanelX: track[0].panelX,
    rawXAtMoveEnd: track[FRAMES_MOVE].rawX,
    panelXAtMoveEnd: track[FRAMES_MOVE].panelX,
    panelXFinal: track[track.length - 1].panelX,
    expectedDelta: 200, // (1-0.3)*1000 - (1-0.5)*1000
    allPinching: track.every(r => r.pinch === true),
  };
}

// ---- scenario 2: two-hand pinch-spread resize ----
// Two co-located-thumb-index hands (unambiguous pinch each) start at a
// known separation, then spread wider and pinch narrower; verify the
// resulting scale direction and the documented 0.4x-2.5x clamp.
{
  function twoHandDistAngle(paNorm, pbNorm){
    let smoothA = null, smoothB = null;
    for (let i = 0; i < 12; i++) { // enough frames for EMA to settle near the held value
      const rawA = makeHand(WRIST, MCP9, paNorm, paNorm);
      const rawB = makeHand(WRIST, MCP9, pbNorm, pbNorm);
      smoothA = smoothLandmarks(smoothA, rawA, LANDMARK_SMOOTH_ALPHA);
      smoothB = smoothLandmarks(smoothB, rawB, LANDMARK_SMOOTH_ALPHA);
    }
    const sa = handPinchState(smoothA, true, VIEW_W, VIEW_H);
    const sb = handPinchState(smoothB, true, VIEW_W, VIEW_H);
    const dist = Math.hypot(sa.x - sb.x, sa.y - sb.y);
    const angle = Math.atan2(sb.y - sa.y, sb.x - sa.x) * 180 / Math.PI;
    return { dist, angle };
  }
  const start = twoHandDistAngle({ x: 0.30, y: 0.5 }, { x: 0.70, y: 0.5 });
  const wider = twoHandDistAngle({ x: 0.20, y: 0.5 }, { x: 0.80, y: 0.5 });
  const narrower = twoHandDistAngle({ x: 0.35, y: 0.5 }, { x: 0.65, y: 0.5 });
  const extremeWide = twoHandDistAngle({ x: 0.01, y: 0.5 }, { x: 0.99, y: 0.5 });
  const extremeClose = twoHandDistAngle({ x: 0.499, y: 0.5 }, { x: 0.501, y: 0.5 });
  const panelStart = { dist: start.dist, angle: start.angle, w: 380, h: 320, rotate: 0 };
  const tWider = computeTwoHandTransform(wider.dist, wider.angle, panelStart);
  const tNarrower = computeTwoHandTransform(narrower.dist, narrower.angle, panelStart);
  const tExtremeWide = computeTwoHandTransform(extremeWide.dist, extremeWide.angle, panelStart);
  const tExtremeClose = computeTwoHandTransform(extremeClose.dist, extremeClose.angle, panelStart);
  out.resize = {
    startW: panelStart.w,
    widerW: tWider.w, narrowerW: tNarrower.w,
    extremeWideW: tExtremeWide.w, extremeCloseW: tExtremeClose.w,
  };
}

// ---- scenario 3: two-hand rotation ----
// Same two-hand pinch, but instead of changing separation, both points
// are rotated together (in normalized space) around their midpoint by a
// known angle before being read through the real handPinchState pipeline.
{
  function twoHandDistAngle(paNorm, pbNorm){
    const smoothA = smoothLandmarks(null, makeHand(WRIST, MCP9, paNorm, paNorm), LANDMARK_SMOOTH_ALPHA);
    const smoothB = smoothLandmarks(null, makeHand(WRIST, MCP9, pbNorm, pbNorm), LANDMARK_SMOOTH_ALPHA);
    const sa = handPinchState(smoothA, true, VIEW_W, VIEW_H);
    const sb = handPinchState(smoothB, true, VIEW_W, VIEW_H);
    const dist = Math.hypot(sa.x - sb.x, sa.y - sb.y);
    const angle = Math.atan2(sb.y - sa.y, sb.x - sa.x) * 180 / Math.PI;
    return { dist, angle };
  }
  // Vertical arrangement (not horizontal): a horizontal A0/B0 pair maps,
  // through the mirrored-x screen transform, to a base angle sitting
  // exactly on the +-180 degree atan2 wraparound boundary, where a +-40
  // degree rotation can appear to jump ~320 degrees the "long way around"
  // — a real discontinuity in the plain `angleNow - angleStart` formula
  // computeTwoHandTransform uses (unchanged from the original code), not
  // a bug this test introduces. A vertical pair keeps the base angle near
  // 90 degrees, comfortably clear of that boundary for a +-40 degree test.
  const mid = { x: 0.5, y: 0.5 };
  const A0 = { x: 0.5, y: 0.35 }, B0 = { x: 0.5, y: 0.65 };
  const base = twoHandDistAngle(A0, B0);
  const panelStart = { dist: base.dist, angle: base.angle, w: 380, h: 320, rotate: 0 };

  function rotatedDelta(thetaDeg){
    const theta = thetaDeg * Math.PI / 180;
    const Ar = rotatePoint(A0, mid, theta), Br = rotatePoint(B0, mid, theta);
    const now = twoHandDistAngle(Ar, Br);
    const t = computeTwoHandTransform(now.dist, now.angle, panelStart);
    return t.rotate - panelStart.rotate;
  }
  out.rotate = {
    plus40: rotatedDelta(40),
    minus40: rotatedDelta(-40),
  };

  // The wraparound case the comment above deliberately steered clear of —
  // now a REAL regression test now that angleDelta() fixes it. A horizontal
  // A0h/B0h pair sits the base angle right on the +-180 boundary; a small
  // +-4 degree rotation must still report a small, correctly-signed delta,
  // not the ~320-360 degree jump the old plain-subtraction formula gave.
  const midH = { x: 0.5, y: 0.5 };
  const A0h = { x: 0.35, y: 0.5 }, B0h = { x: 0.65, y: 0.5 };
  const baseH = twoHandDistAngle(A0h, B0h);
  const panelStartH = { dist: baseH.dist, angle: baseH.angle, w: 380, h: 320, rotate: 0 };
  function rotatedDeltaH(thetaDeg){
    const theta = thetaDeg * Math.PI / 180;
    const Ar = rotatePoint(A0h, midH, theta), Br = rotatePoint(B0h, midH, theta);
    const now = twoHandDistAngle(Ar, Br);
    const t = computeTwoHandTransform(now.dist, now.angle, panelStartH);
    return t.rotate - panelStartH.rotate;
  }
  out.wraparound = {
    baseAngle: baseH.angle,
    plus4: rotatedDeltaH(4),
    minus4: rotatedDeltaH(-4),
  };
}

// ---- scenario 4: noisy held pinch geometry — the actual regression this
// fix targets. The thumb-index distance ratio oscillates between 0.30 and
// 0.50 (straddling the OLD single 0.42 threshold, but never crossing the
// NEW 0.55 release threshold) while the pinch midpoint stays fixed — a
// held-pinch hand with camera noise on the fingers, not a real gesture
// change. `oldPinch` below recreates the DIAGNOSED ORIGINAL formula
// (`dist(lm[4],lm[8]) < scale*0.42`, evaluated on raw per-frame landmarks
// with no memory) purely as a measurement baseline for how much the fix
// helps — it is not current source, since that formula no longer exists
// in ui/workspace.html after this fix.
{
  const RATIOS = [0.30, 0.50, 0.35, 0.48, 0.32, 0.46, 0.38, 0.44, 0.33, 0.49, 0.36, 0.45, 0.31, 0.47];
  const oldPinch = (ratio) => ratio < 0.42;
  let smooth = null, wasPinching = false;
  let oldFlips = 0, newFlips = 0;
  let prevOld = null, prevNew = null;
  const newSeq = [], oldSeq = [];
  for (const ratio of RATIOS) {
    const half = (ratio * HAND_SCALE) / 2;
    const raw = makeHand(WRIST, MCP9, { x: 0.4 - half, y: 0.5 }, { x: 0.4 + half, y: 0.5 });
    // old: raw landmarks, single threshold, no cross-frame memory
    const o = oldPinch(ratio);
    if (prevOld !== null && o !== prevOld) oldFlips++;
    prevOld = o; oldSeq.push(o);
    // new: EMA-smoothed landmarks feeding the hysteresis gate
    smooth = smoothLandmarks(smooth, raw, LANDMARK_SMOOTH_ALPHA);
    const s = handPinchState(smooth, wasPinching, VIEW_W, VIEW_H);
    wasPinching = s.pinch;
    if (prevNew !== null && s.pinch !== prevNew) newFlips++;
    prevNew = s.pinch; newSeq.push(s.pinch);
  }
  out.flicker = { ratios: RATIOS, oldSeq, newSeq, oldFlips, newFlips };
}

// ---- scenario 5: noisy held position — smoothing should damp per-frame
// position jitter without the panel drifting off from where the hand
// actually is. Pinch geometry stays comfortably engaged (ratio 0.1) the
// whole time; only the pinch MIDPOINT position gets +/-0.02 (normalized)
// jitter around a fixed held value.
{
  const JITTER = [0, 0.02, -0.015, 0.018, -0.02, 0.01, -0.01, 0.02, -0.018, 0.015, 0, -0.02];
  const HELD_PX = 0.45;
  let smooth = null, wasPinching = false;
  const rawXs = [], panelXs = [];
  for (const j of JITTER) {
    const px = HELD_PX + j;
    const raw = makeHand(WRIST, MCP9, { x: px, y: 0.5 }, { x: px, y: 0.5 });
    smooth = smoothLandmarks(smooth, raw, LANDMARK_SMOOTH_ALPHA);
    const s = handPinchState(smooth, wasPinching, VIEW_W, VIEW_H);
    wasPinching = s.pinch;
    rawXs.push((1 - px) * VIEW_W);
    panelXs.push(s.x);
  }
  const maxFrameDelta = (arr) => {
    let m = 0;
    for (let i = 1; i < arr.length; i++) m = Math.max(m, Math.abs(arr[i] - arr[i - 1]));
    return m;
  };
  out.jitter = {
    rawMaxFrameDelta: maxFrameDelta(rawXs),
    smoothedMaxFrameDelta: maxFrameDelta(panelXs),
    allPinching: (() => { // pinch never should have dropped — ratio 0.1 is well inside engage
      let s2 = null, w2 = false, ok = true;
      for (const j of JITTER) {
        const px = HELD_PX + j;
        const raw = makeHand(WRIST, MCP9, { x: px, y: 0.5 }, { x: px, y: 0.5 });
        s2 = smoothLandmarks(s2, raw, LANDMARK_SMOOTH_ALPHA);
        const st = handPinchState(s2, w2, VIEW_W, VIEW_H);
        w2 = st.pinch;
        ok = ok && st.pinch;
      }
      return ok;
    })(),
  };
}

process.stdout.write(JSON.stringify(out));
"""


def _build_harness(script: str) -> str:
    parts = [_extract_const(script, c) for c in _GESTURE_CONSTS]
    parts += [_extract_function(script, f) for f in _GESTURE_FUNCS]
    parts.append("// ==================== REAL SOURCE ENDS HERE ====================")
    parts.append(_SCENARIO_JS)
    return "\n".join(parts)


@pytest.fixture(scope="module")
def gesture_math_results(tmp_path_factory) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH in this environment")
    script = _extract_inline_script()
    harness = _build_harness(script)
    tmp_dir = tmp_path_factory.mktemp("hand_gesture_harness")
    js_file = tmp_dir / "harness.js"
    js_file.write_text(harness, encoding="utf-8")
    result = subprocess.run(
        [node, str(js_file)], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"synthetic-landmark Node harness failed:\n{result.stdout}\n{result.stderr}"
    )
    return json.loads(result.stdout)


class TestWorkspaceScriptSyntax:
    def test_node_check_passes(self, tmp_path):
        node = shutil.which("node")
        if not node:
            pytest.skip("node not on PATH in this environment")
        script = _extract_inline_script()
        js_file = tmp_path / "workspace_extracted.js"
        js_file.write_text(script, encoding="utf-8")
        result = subprocess.run(
            [node, "--check", str(js_file)],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, (
            f"node --check failed on the extracted workspace.html script:\n"
            f"{result.stdout}\n{result.stderr}"
        )


class TestHandLandmarkerDelegateFallback:
    """v13: a real bug fixed — startHandControl() used to hard-code
    delegate:"GPU" with no fallback. A packaged desktop app's embedded
    webview (WebView2/WKWebView) frequently has degraded or absent GPU
    compute support versus a normal browser tab: MediaPipe's GPU delegate
    then either rejects createFromOptions outright, or silently returns
    near-zero real detections every frame — either way the user sees no
    error, just hand tracking that "doesn't really work". Fix: try GPU,
    catch ANY failure, retry CPU (WASM SIMD — no such silent-failure mode),
    and surface which one actually won."""

    def test_gpu_tried_first_with_a_cpu_fallback_on_failure(self):
        script = _extract_inline_script()
        fn = _extract_function(script, "startHandControl")
        assert 'await _createHandLandmarker(vision, fileset, "GPU")' in fn
        assert 'await _createHandLandmarker(vision, fileset, "CPU")' in fn
        # The CPU retry must be reachable ONLY from a catch around the GPU
        # attempt — i.e. genuinely a fallback, not just two calls in a row.
        gpu_idx = fn.index('await _createHandLandmarker(vision, fileset, "GPU")')
        catch_idx = fn.index("catch(gpuErr)")
        cpu_idx = fn.index('await _createHandLandmarker(vision, fileset, "CPU")')
        assert gpu_idx < catch_idx < cpu_idx

    def test_active_delegate_is_surfaced_to_the_user(self):
        script = _extract_inline_script()
        fn = _extract_function(script, "startHandControl")
        assert "HAND.delegateEl.textContent = delegateUsed" in fn

    def test_stop_resets_the_delegate_readout_and_closes_the_old_instance(self):
        script = _extract_inline_script()
        fn = _extract_function(script, "stopHandControl")
        assert 'HAND.delegateEl.textContent = "—"' in fn
        assert "HAND.handLM.close()" in fn


class TestLandmarkSmoothingConstants:
    def test_alpha_is_a_named_documented_constant_in_range(self):
        script = _extract_inline_script()
        const_line = _extract_const(script, "LANDMARK_SMOOTH_ALPHA")
        alpha = float(re.search(r"[\d.]+", const_line).group(0))
        assert 0.0 < alpha < 1.0
        # documented "responsiveness-weighted" 0.5-0.7 range from the task
        assert 0.5 <= alpha <= 0.7

    def test_release_threshold_is_looser_than_engage_threshold(self):
        # the whole point of the Schmitt trigger: release must be a wider
        # (larger) ratio than engage, or it isn't hysteresis at all.
        script = _extract_inline_script()
        engage = float(re.search(r"[\d.]+", _extract_const(script, "PINCH_ENGAGE_RATIO")).group(0))
        release = float(re.search(r"[\d.]+", _extract_const(script, "PINCH_RELEASE_RATIO")).group(0))
        assert release > engage


class TestSyntheticPinchAndDrag:
    """Scenario 1: does a synthetic pinch-and-drag move the panel by the
    right amount in the right direction?"""

    def test_drag_tracks_toward_the_real_destination(self, gesture_math_results):
        d = gesture_math_results["drag"]
        # direction: raw destination moved +200px (rightward); the panel
        # must have moved the same direction, not backward.
        assert d["panelXAtMoveEnd"] > d["startPanelX"]
        # magnitude at the moment raw motion stops: EMA smoothing means the
        # panel is still catching up (real, expected lag) but should
        # already have covered the large majority of the real 200px move.
        moved_by_move_end = d["panelXAtMoveEnd"] - d["startPanelX"]
        assert moved_by_move_end > 0.85 * d["expectedDelta"], (
            f"panel only moved {moved_by_move_end:.1f}px of the real "
            f"{d['expectedDelta']}px move by the time raw motion stopped"
        )
        # magnitude after holding the final position: EMA must fully
        # converge given enough frames, landing within 1px of the true
        # 200px destination — not stuck short due to smoothing bias.
        moved_final = d["panelXFinal"] - d["startPanelX"]
        assert abs(moved_final - d["expectedDelta"]) < 1.0, (
            f"panel converged to {moved_final:.2f}px of {d['expectedDelta']}px "
            f"after holding position — smoothing should fully settle, not bias"
        )
        # pinch must have stayed engaged for the entire drag (co-located
        # thumb/index = unambiguous pinch every single frame).
        assert d["allPinching"] is True


class TestSyntheticTwoHandResize:
    """Scenario 2: does a synthetic two-hand pinch-spread resize correctly,
    in the right direction, with the documented clamp respected?"""

    def test_wider_spread_grows_the_panel(self, gesture_math_results):
        r = gesture_math_results["resize"]
        assert r["widerW"] > r["startW"] > r["narrowerW"]

    def test_scale_clamp_respected_both_directions(self, gesture_math_results):
        r = gesture_math_results["resize"]
        # documented clamp: 0.4x - 2.5x of the starting size
        assert r["extremeWideW"] <= r["startW"] * 2.5 + 1e-6
        assert r["extremeCloseW"] >= r["startW"] * 0.4 - 1e-6


class TestSyntheticTwoHandRotation:
    """Scenario 3: does a synthetic two-hand rotation rotate the panel by
    approximately the right angle, in the right (consistent) direction?"""

    def test_rotation_magnitude_is_approximately_right(self, gesture_math_results):
        r = gesture_math_results["rotate"]
        assert abs(abs(r["plus40"]) - 40) < 1.5, r
        assert abs(abs(r["minus40"]) - 40) < 1.5, r

    def test_rotation_direction_reverses_with_input(self, gesture_math_results):
        r = gesture_math_results["rotate"]
        # rotating the two hands the opposite way must rotate the panel
        # the opposite way too — same sign convention, flipped input.
        assert (r["plus40"] > 0) != (r["minus40"] > 0)


class TestAngleWraparound:
    """Real bug found and fixed after the smoothing/hysteresis pass shipped:
    computeTwoHandTransform's plain `angleNow - angleStart` subtraction jumps
    by ~360deg the instant the two-hand angle crosses the atan2 +-180deg
    boundary. The original test suite explicitly documented this case and
    steered its base angle away from the boundary to avoid it (see the
    driver's own comment) — this class is the regression test for the real
    fix (angleDelta), placed directly on that boundary."""

    def test_base_angle_is_actually_near_the_180_boundary(self, gesture_math_results):
        # Sanity check that this scenario actually exercises the boundary —
        # a wrong test fixture asserting nothing meaningful would be worse
        # than no test at all.
        r = gesture_math_results["wraparound"]
        assert abs(abs(r["baseAngle"]) - 180) < 1.0, r

    def test_small_rotation_near_the_boundary_gives_a_small_delta(self, gesture_math_results):
        r = gesture_math_results["wraparound"]
        # A +-4 degree hand rotation must report a small delta close to
        # +-4 degrees — NOT the ~320-360 degree jump the old plain
        # subtraction would have produced right at this boundary.
        assert abs(abs(r["plus4"]) - 4) < 1.5, r
        assert abs(abs(r["minus4"]) - 4) < 1.5, r
        assert abs(r["plus4"]) < 10, r
        assert abs(r["minus4"]) < 10, r


class TestSyntheticNoisyPinchNoFlicker:
    """Scenario 4 — the actual regression this fix targets: a synthetic
    NOISY held-pinch sequence must not cause click-through pinch-state
    flicker, unlike the old single-threshold formula."""

    def test_old_single_threshold_really_would_have_flickered(self, gesture_math_results):
        # sanity check on the injected noise itself: prove it's genuinely
        # adversarial for a naive single threshold before crediting the fix.
        f = gesture_math_results["flicker"]
        assert f["oldFlips"] >= 5, f"expected the old formula to flicker a lot; got {f}"

    def test_new_hysteresis_does_not_flicker_on_the_same_noise(self, gesture_math_results):
        f = gesture_math_results["flicker"]
        assert f["newFlips"] == 0, (
            f"pinch hysteresis flipped {f['newFlips']} times on noise that "
            f"never crosses the release threshold: {f}"
        )


class TestSyntheticNoisyHeldPositionNoSpuriousDrag:
    """Extra coverage in the same spirit as scenario 4: noisy per-frame
    POSITION (not just pinch geometry) should not translate into
    proportionally large spurious drag movement."""

    def test_smoothed_position_jitter_is_meaningfully_damped(self, gesture_math_results):
        j = gesture_math_results["jitter"]
        assert j["smoothedMaxFrameDelta"] < 0.6 * j["rawMaxFrameDelta"], (
            f"smoothing barely damped position jitter: {j}"
        )

    def test_pinch_stays_engaged_throughout(self, gesture_math_results):
        j = gesture_math_results["jitter"]
        assert j["allPinching"] is True
