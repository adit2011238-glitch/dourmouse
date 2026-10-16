"""SPLIT: the BROWSER split view's layout math (ui/assets/os/screens/browser/split-model.js), tested in node.

The model is pure: ratio clamp (minimum 280 px, maximum 45 percent), the 900 px overlay threshold,
drag and key maths for the divider, what the native pane may cover, and the column's fixed copy.
The DOM that uses it is checked by test_fix_SPLIT_browser_split_dom.py in a real browser.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_DIR = _ROOT / "ui" / "assets" / "os" / "screens" / "browser"
_NODE = shutil.which("node")


def node(tmp_path, body):
    if _NODE is None:
        pytest.skip("node not on PATH in this environment")
    script = tmp_path / "m.mjs"
    script.write_text(
        f"import * as m from {(_DIR / 'split-model.js').as_uri()!r};\n"
        f"import * as h from {(_DIR / 'helpers.js').as_uri()!r};\nconst R = {{}};\n{body}\nconsole.log(JSON.stringify(R));\n",
        encoding="utf-8",
    )
    proc = subprocess.run([_NODE, str(script)], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestStoredPreferences:
    def test_the_stored_ratio_is_read_safely_and_never_leaves_the_allowed_range(self, tmp_path):
        out = node(tmp_path, """
R.none = m.parseStoredRatio(null);
R.empty = m.parseStoredRatio('');
R.junk = m.parseStoredRatio('wide');
R.zero = m.parseStoredRatio('0');
R.neg = m.parseStoredRatio('-0.3');
R.inf = m.parseStoredRatio('Infinity');
R.ok = m.parseStoredRatio('0.3');
R.big = m.parseStoredRatio('0.9');
R.tiny = m.parseStoredRatio('0.001');
""")
        assert out["none"] == out["empty"] == out["junk"] == out["zero"] == out["neg"] == out["inf"] == 0.2
        assert out["ok"] == 0.3
        assert out["big"] == 0.45
        assert 0 < out["tiny"] <= 0.1

    def test_side_and_collapsed_default_to_right_and_open(self, tmp_path):
        out = node(tmp_path, """
R.sideNone = m.parseStoredSide(null);
R.sideLeft = m.parseStoredSide('left');
R.sideJunk = m.parseStoredSide('top');
R.colNone = m.parseStoredFlag(null);
R.colOne = m.parseStoredFlag('1');
R.colJunk = m.parseStoredFlag('true');
""")
        assert out["sideNone"] == "right" and out["sideLeft"] == "left" and out["sideJunk"] == "right"
        assert out["colNone"] is False and out["colOne"] is True and out["colJunk"] is False


class TestTheLimits:
    def test_the_defaults_are_twenty_percent_with_280_pixels_minimum_and_45_percent_maximum(self, tmp_path):
        out = node(tmp_path, """
R.c = { ratio: m.DEFAULT_RATIO, min: m.MIN_CHAT_PX, max: m.MAX_RATIO, below: m.OVERLAY_BELOW };
""")
        assert out["c"] == {"ratio": 0.2, "min": 280, "max": 0.45, "below": 900}

    def test_the_column_is_clamped_to_280_pixels_and_45_percent_of_the_screen(self, tmp_path):
        out = node(tmp_path, """
R.w1400 = m.chatPxFor(0.2, 1400);       // 280 exactly
R.w1600 = m.chatPxFor(0.2, 1600);       // 320
R.w1000 = m.chatPxFor(0.2, 1000);       // 200 is under the minimum
R.tooWide = m.chatPxFor(0.9, 1400);     // capped at 45 percent
R.tooNarrow = m.chatPxFor(0.01, 1600);
R.clampLo = m.clampChatPx(10, 1200);
R.clampHi = m.clampChatPx(9999, 1200);
R.clampNaN = m.clampChatPx(NaN, 1200);
R.maxAt900 = m.clampChatPx(9999, 900);
R.tiny = m.clampChatPx(9999, 300);       // never wider than 45 percent, even below the minimum
""")
        assert out["w1400"] == 280 and out["w1600"] == 320 and out["w1000"] == 280
        assert out["tooWide"] == 630  # 45 percent of 1400
        assert out["tooNarrow"] == 280
        assert out["clampLo"] == 280 and out["clampHi"] == 540 and out["clampNaN"] == 280
        assert out["maxAt900"] == 405
        assert out["tiny"] == 135

    def test_the_ratio_read_back_from_a_width_is_stable(self, tmp_path):
        out = node(tmp_path, """
R.a = m.ratioFromPx(320, 1600);
R.b = m.ratioFromPx(100, 1600);   // clamped to 280 first
R.c = m.ratioFromPx(5000, 1600);  // clamped to 45 percent first
R.zero = m.ratioFromPx(300, 0);
R.round = m.chatPxFor(m.ratioFromPx(333, 1777), 1777);
""")
        assert out["a"] == 0.2
        assert out["b"] == 0.175
        assert out["c"] == 0.45
        assert out["zero"] == 0.2
        assert out["round"] == 333


class TestLayout:
    def test_at_a_wide_screen_the_page_gets_about_80_percent_and_the_column_20(self, tmp_path):
        out = node(tmp_path, """
R.l = m.layoutFor({ total: 1600, ratio: 0.2, collapsed: false, overlayOpen: false });
""")
        lay = out["l"]
        assert lay["mode"] == "split" and lay["chatPx"] == 320 and lay["dividerPx"] == 8
        assert lay["leftPx"] == 1600 - 320 - 8
        assert 0.78 <= lay["leftPx"] / 1600 <= 0.8

    def test_collapsing_leaves_only_a_narrow_rail_and_the_page_takes_the_rest(self, tmp_path):
        out = node(tmp_path, "R.l = m.layoutFor({ total: 1600, ratio: 0.2, collapsed: true, overlayOpen: false });")
        lay = out["l"]
        assert lay["mode"] == "rail" and lay["chatPx"] == 0 and lay["railPx"] == 34
        assert lay["leftPx"] == 1600 - 34

    def test_below_900_pixels_the_column_is_an_overlay_and_never_squeezes_the_page(self, tmp_path):
        out = node(tmp_path, """
R.closed = m.layoutFor({ total: 899, ratio: 0.2, collapsed: false, overlayOpen: false });
R.open = m.layoutFor({ total: 899, ratio: 0.2, collapsed: false, overlayOpen: true });
R.edge = m.layoutFor({ total: 900, ratio: 0.2, collapsed: false, overlayOpen: false });
R.collapsedIgnored = m.layoutFor({ total: 700, ratio: 0.2, collapsed: true, overlayOpen: false });
R.phone = m.layoutFor({ total: 360, ratio: 0.2, collapsed: false, overlayOpen: true });
R.unknown = m.layoutFor({ total: 0, ratio: 0.2, collapsed: false, overlayOpen: false });
""")
        assert out["closed"]["mode"] == "overlay-closed" and out["closed"]["leftPx"] == 899 - 34
        assert out["open"]["mode"] == "overlay-open" and out["open"]["leftPx"] == 899 - 34
        assert 240 <= out["open"]["chatPx"] <= 380 and out["open"]["chatPx"] <= 899 - 34
        assert out["edge"]["mode"] == "split"
        assert out["collapsedIgnored"]["mode"] == "overlay-closed"
        assert out["phone"]["chatPx"] <= 360 - 34
        assert out["unknown"]["mode"] == "overlay-closed" and out["unknown"]["leftPx"] == 0

    def test_a_toggle_means_collapse_in_split_mode_and_open_in_overlay_mode(self, tmp_path):
        out = node(tmp_path, """
R.wideOpen = m.toggled({ total: 1400, collapsed: false, overlayOpen: false });
R.wideShut = m.toggled({ total: 1400, collapsed: true, overlayOpen: false });
R.narrow = m.toggled({ total: 700, collapsed: false, overlayOpen: false });
R.narrowBack = m.toggled({ total: 700, collapsed: false, overlayOpen: true });
""")
        assert out["wideOpen"] == {"collapsed": True, "overlayOpen": False}
        assert out["wideShut"] == {"collapsed": False, "overlayOpen": False}
        assert out["narrow"] == {"collapsed": False, "overlayOpen": True}
        assert out["narrowBack"] == {"collapsed": False, "overlayOpen": False}

    def test_a_pending_approval_forces_the_column_open_without_forgetting_the_saved_choice(self, tmp_path):
        out = node(tmp_path, """
R.wide = m.forcedOpen({ total: 1400, collapsed: true, overlayOpen: false });
R.narrow = m.forcedOpen({ total: 600, collapsed: false, overlayOpen: false });
R.already = m.forcedOpen({ total: 1400, collapsed: false, overlayOpen: false });
""")
        assert out["wide"] == {"collapsed": False, "overlayOpen": False}
        assert out["narrow"] == {"collapsed": False, "overlayOpen": True}
        assert out["already"] is None


class TestTheDivider:
    def test_dragging_on_the_right_hand_side_grows_the_column_as_the_pointer_moves_left(self, tmp_path):
        out = node(tmp_path, """
const base = { rectLeft: 100, rectRight: 1700, total: 1600, side: 'right' };
R.a = m.dragPx({ ...base, pointer: 1380 });   // 1700 - 1380 - 4 = 316
R.b = m.dragPx({ ...base, pointer: 800 });    // far left: capped at 45 percent
R.c = m.dragPx({ ...base, pointer: 1690 });   // far right: minimum
""")
        assert out["a"] == 316 and out["b"] == 720 and out["c"] == 280

    def test_dragging_on_the_left_hand_side_is_the_mirror_image(self, tmp_path):
        out = node(tmp_path, """
const base = { rectLeft: 100, rectRight: 1700, total: 1600, side: 'left' };
R.a = m.dragPx({ ...base, pointer: 424 });    // 424 - 100 - 4 = 320
R.b = m.dragPx({ ...base, pointer: 1500 });
R.c = m.dragPx({ ...base, pointer: 110 });
""")
        assert out["a"] == 320 and out["b"] == 720 and out["c"] == 280

    def test_arrow_keys_move_the_divider_the_way_the_arrow_points(self, tmp_path):
        out = node(tmp_path, """
R.rLeft = m.keyPx({ key: 'ArrowLeft', px: 320, total: 1600, side: 'right', shift: false });   // divider moves left: column grows
R.rRight = m.keyPx({ key: 'ArrowRight', px: 320, total: 1600, side: 'right', shift: true });  // 100 px step, shrinks
R.lLeft = m.keyPx({ key: 'ArrowLeft', px: 320, total: 1600, side: 'left', shift: false });    // column on the left shrinks
R.lRight = m.keyPx({ key: 'ArrowRight', px: 320, total: 1600, side: 'left', shift: false });
R.floor = m.keyPx({ key: 'ArrowRight', px: 285, total: 1600, side: 'right', shift: true });
R.other = m.keyPx({ key: 'a', px: 320, total: 1600, side: 'right', shift: false });
""")
        assert out["rLeft"] == 340 and out["rRight"] == 280 and out["lLeft"] == 300 and out["lRight"] == 340
        assert out["floor"] == 280
        assert out["other"] is None


class TestWhatTheNativeViewMayCover:
    """The native pane is composited above the page's own DOM, so its rectangle is the whole safety net."""

    def test_the_clip_is_the_left_region_so_the_view_never_reaches_the_column(self, tmp_path):
        out = node(tmp_path, """
const base = { left: 0, top: 0, right: 1600, bottom: 900 };
const left = { left: 0, top: 80, right: 1272, bottom: 900 };
const page = { left: 10, top: 160, right: 1262, bottom: 800 };
R.clip = m.paneClip({ base, left, overlay: null, side: 'right' });
R.bounds = h.viewBounds(page, R.clip);
R.leftSide = m.paneClip({ base, left: { ...left, left: 328, right: 1600 }, overlay: null, side: 'left' });
""")
        assert out["clip"]["right"] == 1272 and out["clip"]["left"] == 0
        assert out["bounds"] == {"x": 10, "y": 160, "width": 1252, "height": 640}
        assert out["leftSide"]["left"] == 328

    def test_an_open_overlay_is_cut_out_of_the_view_on_its_own_side(self, tmp_path):
        out = node(tmp_path, """
const base = { left: 0, top: 0, right: 800, bottom: 900 };
const left = { left: 0, top: 80, right: 766, bottom: 900 };
const overlayR = { left: 466, top: 80, right: 800, bottom: 900 };
const overlayL = { left: 34, top: 80, right: 368, bottom: 900 };
R.right = m.paneClip({ base, left, overlay: overlayR, side: 'right' });
R.leftSideLeft = m.paneClip({ base, left: { ...left, left: 34, right: 800 }, overlay: overlayL, side: 'left' });
R.page = h.viewBounds({ left: 10, top: 160, right: 756, bottom: 800 }, R.right);
""")
        assert out["right"]["right"] == 466
        assert out["leftSideLeft"]["left"] == 368
        assert out["page"] == {"x": 10, "y": 160, "width": 456, "height": 640}

    def test_a_missing_left_region_gives_no_clip_at_all_so_the_view_stays_hidden(self, tmp_path):
        out = node(tmp_path, """
R.none = m.paneClip({ base: { left: 0, top: 0, right: 10, bottom: 10 }, left: null, overlay: null, side: 'right' });
""")
        assert out["none"] is None


class TestTheColumnsOwnWords:
    def test_the_three_starters_say_what_they_do_and_two_of_them_say_who_confirms(self, tmp_path):
        out = node(tmp_path, "R.s = m.STARTERS;")
        labels = [s["label"] for s in out["s"]]
        assert labels == ["Summarise this page", "Find the price", "Fill this form (I'll confirm)"]
        for s in out["s"]:
            assert len(s["send"]) > 20 and s["spec"]
        fill = out["s"][2]["send"].lower()
        assert "before" in fill and ("submit" in fill or "send" in fill)

    def test_the_context_line_names_the_tab_the_ai_will_work_in(self, tmp_path):
        out = node(tmp_path, """
R.page = m.contextLine({ url: 'https://shop.example.com/cart?x=1', title: 'Your cart', loading: false }, true);
R.noTitle = m.contextLine({ url: 'https://shop.example.com/cart', title: '', loading: false }, true);
R.loading = m.contextLine({ url: 'https://a.example.org/', title: '', loading: true }, true);
R.empty = m.contextLine({ url: '', title: '', loading: false }, true);
R.app = m.contextLine({ url: '/hub.html', title: '', loading: false }, false);
R.noPane = m.contextLine({ url: 'https://a.example.org/', title: 'A', loading: false }, false);
""")
        assert out["page"] == {"title": "Your cart", "host": "shop.example.com", "kind": "page", "shared": True}
        assert out["noTitle"]["title"] == "shop.example.com" and out["noTitle"]["kind"] == "page"
        assert out["loading"]["kind"] == "loading" and out["loading"]["host"] == "a.example.org"
        assert out["empty"]["kind"] == "none" and out["empty"]["title"] == "No page open"
        assert out["noPane"]["shared"] is False


def test_the_model_has_no_dom_and_no_em_dash():
    text = (_DIR / "split-model.js").read_text(encoding="utf-8")
    code = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    assert "\u2014" not in text
    assert not re.search(r"\b(document|window|navigator|localStorage)\b", code), "split-model.js must stay pure"
