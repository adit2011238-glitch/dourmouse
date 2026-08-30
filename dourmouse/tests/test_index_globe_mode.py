"""ui/index.html — VISION_ROADMAP.md Phase 1: gesture -> globe bridge.

Real, already-shipped pieces this wires together, not new subsystems:
- ui/index.html's own MediaPipe hand-tracking gesture engine
  (gestureAct()/onHands(), v5.29 "advanced gesture engine").
- gods-eye-view/src/voice/gevActions.js's createGevActionRunner -- the
  globe's real, already-tested voice-command action vocabulary
  (zoom_to_globe, adjust_camera_zoom, select_nearest_aircraft,
  track_entity, stop_tracking, frame_overhead, ...).
- gods-eye-view/vite.config.js's dourmouseActionBridgeProxy -- a real
  POST /api/dourmouse/action endpoint the dev server already exposes for
  an external caller to run one of those actions and get the real result
  back, proven by dourmouseBridge.js's own voice-side long-poll consumer.

GLOBE MODE is an explicit user-clicked toggle (not a gesture, not a
"which window has focus" guess across two separate browser origins/ports)
that repoints a SUBSET of the existing gesture vocabulary at
sendGevAction() instead of index.html's own normal action, while the
toggle is off nothing changes at all. Halt/emergency-stop/wake (OPEN
PALM/BOTH PALMS/WAVE) are deliberately never remapped -- those must mean
the same thing everywhere per the roadmap's own stated rule.

No headless browser here (none available in this suite, matching this
session's own established convention for ui/console.html) -- source-level
coverage that the real wiring is present, syntactically correct, and the
safety-critical gestures are provably untouched by the remap.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_INDEX_HTML = _PROJECT_ROOT / "ui" / "index.html"


def _extract_inline_scripts() -> list[str]:
    html = _INDEX_HTML.read_text(encoding="utf-8")
    return re.findall(r"<script>(.*?)</script>", html, re.S)


def _gesture_engine_script() -> str:
    scripts = _extract_inline_scripts()
    for s in scripts:
        if "function gestureAct(name)" in s:
            return s
    raise AssertionError("no inline <script> block contains gestureAct()")


class TestIndexHtmlScriptSyntax:
    def test_every_inline_script_is_valid_js(self, tmp_path):
        node = shutil.which("node")
        if not node:
            pytest.skip("node not on PATH in this environment")
        scripts = _extract_inline_scripts()
        assert scripts, "ui/index.html has no inline <script>...</script> blocks"
        for i, script in enumerate(scripts):
            js_file = tmp_path / f"index_script_{i}.js"
            js_file.write_text(script, encoding="utf-8")
            result = subprocess.run(
                [node, "--check", str(js_file)],
                capture_output=True, text=True, timeout=30,
            )
            assert result.returncode == 0, (
                f"node --check failed on inline script block {i}:\n"
                f"{result.stdout}\n{result.stderr}"
            )


class TestGevActionBridge:
    def test_real_action_endpoint_and_url_match_the_vite_proxy(self):
        script = _gesture_engine_script()
        assert "GODS_EYE_ACTION_URL = 'http://localhost:4173/api/dourmouse/action'" in script
        # The proxy this must match, verbatim in the vendored app.
        proxy_src = (_PROJECT_ROOT / "gods-eye-view" / "vite.config.js").read_text(encoding="utf-8")
        assert "'/api/dourmouse/action'" in proxy_src

    def test_send_gev_action_posts_name_and_args_never_throws_into_the_caller(self):
        script = _gesture_engine_script()
        m = re.search(r"function sendGevAction\(name, args\) \{(.*?)\n  \}\n", script, re.S)
        assert m, "sendGevAction not found"
        body = m.group(1)
        assert "method: 'POST'" in body
        assert "JSON.stringify({ name, args: args || {} })" in body
        assert ".catch(" in body  # a dead God's Eye View tab must never bubble up

    def test_toggle_is_a_plain_click_not_a_gesture(self):
        script = _gesture_engine_script()
        assert "visGlobeModeBtn" in script
        assert "GLOBE_MODE = !GLOBE_MODE" in script


class TestSelectNearestAircraftUsesRealCameraContext:
    """select_nearest_aircraft's real contract (gevActions.js) requires a
    preset/place-name/lat+lon -- "nearest" needs a reference point, and a
    gesture has no typed location. sendGevSelectNearestAircraft must ask
    the globe where its own camera is centered (get_current_view_state)
    and use THAT, never call select_nearest_aircraft with empty args."""

    def test_fetches_view_state_before_selecting(self):
        script = _gesture_engine_script()
        m = re.search(
            r"async function sendGevSelectNearestAircraft\(\) \{(.*?)\n  \}\n",
            script, re.S,
        )
        assert m, "sendGevSelectNearestAircraft not found"
        body = m.group(1)
        assert "sendGevAction('get_current_view_state'" in body
        assert "view.camera.latitude" in body
        assert "view.camera.longitude" in body
        assert "sendGevAction('select_nearest_aircraft'" in body

    def test_bails_out_honestly_if_view_state_is_unavailable(self):
        script = _gesture_engine_script()
        m = re.search(
            r"async function sendGevSelectNearestAircraft\(\) \{(.*?)\n  \}\n",
            script, re.S,
        )
        assert m
        body = m.group(1)
        assert "view.ok === false" in body or "!view.camera" in body
        assert "return null" in body


class TestGestureRemapOnlyWhenToggledOn:
    """Every remapped case must check GLOBE_MODE first and fall through to
    the ORIGINAL action unchanged when it's off -- toggling off must be a
    real no-op, not just "mostly the same"."""

    REMAPPED = {
        "PINCH": ("adjust_camera_zoom", "send"),
        "THUMBS_UP": ("sendGevSelectNearestAircraft", "approve"),
        "THUMBS_DOWN": ("stop_tracking", "deny"),
        "ROCK": ("zoom_to_globe", "cycleView"),
        "OK": ("sendGevSelectNearestAircraft", "openPalette"),
        "FIST": ("frame_overhead", "deckShow"),
    }

    def test_each_remapped_case_checks_globe_mode_and_keeps_the_original_action(self):
        script = _gesture_engine_script()
        m = re.search(r"function gestureAct\(name\) \{(.*?)\n  \}\n", script, re.S)
        assert m, "gestureAct not found"
        body = m.group(1)
        cases = re.split(r"\n      case '", body)
        by_name = {c.split("':")[0]: c for c in cases[1:]}
        for gesture, (gev_action, original_marker) in self.REMAPPED.items():
            assert gesture in by_name, f"case {gesture!r} not found in gestureAct"
            case_body = by_name[gesture]
            assert "if (GLOBE_MODE)" in case_body, gesture
            assert gev_action in case_body, gesture
            assert original_marker in case_body, (
                f"{gesture}'s original (non-globe) action {original_marker!r} "
                "seems to have been removed, not just guarded"
            )

    def test_halt_emergency_stop_and_wake_are_never_remapped(self):
        """OPEN PALM / BOTH PALMS / WAVE must mean the same thing whether
        GLOBE MODE is on or off -- explicit rule from VISION_ROADMAP.md."""
        script = _gesture_engine_script()
        m = re.search(r"function gestureAct\(name\) \{(.*?)\n  \}\n", script, re.S)
        assert m
        body = m.group(1)
        for gesture in ("OPEN_PALM", "BOTH_PALMS", "WAVE"):
            case_m = re.search(
                rf"case '{gesture}':(.*?)break;", body, re.S
            )
            assert case_m, f"case {gesture!r} not found"
            assert "GLOBE_MODE" not in case_m.group(1), (
                f"{gesture} must never be gated on GLOBE_MODE"
            )


class TestPhase2CoarsePointPan:
    """VISION_ROADMAP.md Phase 2, option 2: no cross-origin raycast into
    the globe's own 3D scene exists (documented architectural gap in the
    roadmap itself) -- POINT in Globe Mode instead reads which way the
    finger points RELATIVE TO THE WRIST and pans the camera that way via
    the real move_camera(motion:'pan', direction, mode:'continuous')
    contract (gods-eye-view/src/cameraVerbs.js's moveCamera)."""

    def _on_hands_point_branch(self) -> str:
        scripts = _extract_inline_scripts()
        target = next(s for s in scripts if "function onHands(result)" in s)
        m = re.search(
            r"if \(s\.g === 'POINT'\) \{(.*?)\n      setMode\('POINTER'\);",
            target, re.S,
        )
        assert m, "GLOBE_MODE branch of the POINT case not found in onHands"
        return m.group(1)

    def test_direction_computed_from_fingertip_relative_to_wrist(self):
        body = self._on_hands_point_branch()
        assert "GLOBE_MODE" in body
        assert "wrist.x - s.tip.x" in body  # x negated for the mirrored display
        assert "s.tip.y - wrist.y" in body
        assert "'up'" in body and "'down'" in body and "'left'" in body and "'right'" in body

    def test_pan_call_uses_the_real_move_camera_contract(self):
        body = self._on_hands_point_branch()
        assert "sendGevAction('move_camera'" in body
        assert "motion: 'pan'" in body
        assert "mode: 'continuous'" in body

    def test_pan_is_throttled_like_the_zoom_branch(self):
        body = self._on_hands_point_branch()
        assert "_lastGlobePan" in body
        assert "> 400" in body

    def test_stops_panning_when_the_finger_leaves_point_shape(self):
        scripts = _extract_inline_scripts()
        target = next(s for s in scripts if "function onHands(result)" in s)
        assert "GS._globePanning && s.g !== 'POINT'" in target
        assert "motion: 'stop' }" in target

    def test_stops_panning_when_hands_leave_the_frame(self):
        scripts = _extract_inline_scripts()
        target = next(s for s in scripts if "function onHands(result)" in s)
        m = re.search(
            r"if \(!result \|\| !result\.landmarks \|\| !result\.landmarks\.length\) \{(.*?)\n    \}\n",
            target, re.S,
        )
        assert m
        assert "GS._globePanning" in m.group(1)

    def test_stops_panning_when_globe_mode_is_toggled_off(self):
        script = _gesture_engine_script()
        m = re.search(r"_visGlobeModeBtn\.addEventListener\('click', \(\) => \{(.*?)\n  \}\);", script, re.S)
        assert m, "toggle click handler not found"
        assert "GS._globePanning" in m.group(1)


class TestHoldProgressMeter:
    """VISION_ROADMAP.md Phase 4 (corrected scope): the 21-point hand
    skeleton already existed (drawLandmarks, pre-existing) -- what was
    genuinely missing was making the hysteresis engine's OWN already-
    computed hold-progress (GS.pending/GS.count/GS.NEED) visible, so a
    gesture that's recognized but not held long enough shows a filling
    bar instead of silently doing nothing."""

    def _full_scripts(self):
        return _extract_inline_scripts()

    def _script_with(self, marker: str) -> str:
        for s in self._full_scripts():
            if marker in s:
                return s
        raise AssertionError(f"no inline script contains {marker!r}")

    def test_paint_hold_meter_defined_and_scales_by_need(self):
        script = self._script_with("function paintHoldMeter(")
        m = re.search(r"function paintHoldMeter\(name, count\) \{(.*?)\n  \}\n", script, re.S)
        assert m, "paintHoldMeter not found"
        body = m.group(1)
        assert "count / GS.NEED" in body
        assert "meter.style.display = 'none'" in body  # hides when nothing pending

    def test_feed_gesture_paints_progress_on_every_held_frame(self):
        script = self._script_with("function feedGesture(")
        m = re.search(r"function feedGesture\(name\) \{(.*?)\n  \}\n", script, re.S)
        assert m, "feedGesture not found"
        body = m.group(1)
        assert "paintHoldMeter(name, GS.count)" in body
        assert "paintHoldMeter(name, 1)" in body

    def test_commit_gesture_clears_the_meter_before_acting(self):
        script = self._script_with("function commitGesture(")
        m = re.search(r"function commitGesture\(name\) \{(.*?)\n  \}\n", script, re.S)
        assert m, "commitGesture not found"
        body = m.group(1)
        assert "paintHoldMeter(null, 0)" in body

    def test_no_hands_detected_clears_the_meter(self):
        script = self._script_with("function onHands(result)")
        m = re.search(
            r"if \(!result \|\| !result\.landmarks \|\| !result\.landmarks\.length\) \{(.*?)\n    \}\n",
            script, re.S,
        )
        assert m, "no-hands early return not found in onHands"
        assert "paintHoldMeter(null, 0)" in m.group(1)


class TestContinuousZoomThrottled:
    """PEACE/THREE's continuous per-frame path (onHands) must throttle real
    HTTP calls, not fire one every animation frame — the dev-server proxy's
    own 15s per-call result-wait would let an unthrottled 60fps loop pile
    up dozens of in-flight requests."""

    def test_globe_zoom_branch_is_time_gated(self):
        scripts = _extract_inline_scripts()
        target = next(s for s in scripts if "function onHands(result)" in s)
        m = re.search(r"if \(s\.g === 'PEACE' \|\| s\.g === 'THREE'\) \{(.*?)\n    \}\n", target, re.S)
        assert m, "PEACE/THREE continuous branch not found in onHands"
        body = m.group(1)
        assert "GLOBE_MODE" in body
        assert "_lastGlobeZoom" in body
        assert "> 400" in body  # a real minimum interval, not per-frame
        assert "sendGevAction('adjust_camera_zoom'" in body
