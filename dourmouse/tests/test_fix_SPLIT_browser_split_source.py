"""SPLIT: the rules the split view's source has to keep (screen contract, wiring, copy).

What can be checked without a browser is checked here; the layout itself is test_fix_SPLIT_browser_split_dom.py.
"""

from __future__ import annotations

import re
from pathlib import Path

from dourmouse.tests.test_os_screen_contract import FORBIDDEN, strip_comments, top_level_selectors

_ROOT = Path(__file__).resolve().parents[2]
_DIR = _ROOT / "ui" / "assets" / "os" / "screens" / "browser"
NEW_JS = ["split.js", "split-model.js", "split-chat.js"]


def src(name: str) -> str:
    return (_DIR / name).read_text(encoding="utf-8")


def code(name: str) -> str:
    return strip_comments(src(name))


class TestContract:
    def test_the_new_files_follow_the_screen_contract(self):
        for name in NEW_JS:
            for pattern, what in FORBIDDEN:
                m = re.search(pattern, code(name))
                assert not m, f"{name} uses {what}"

    def test_no_new_file_has_an_em_dash(self):
        for name in [*NEW_JS, "split.css"]:
            assert "\u2014" not in src(name), name

    def test_the_stylesheet_is_scoped_to_the_screen_and_uses_the_accent_token(self):
        css = src("split.css")
        sels = [s for s in top_level_selectors(css) if not s.startswith("@keyframes")]
        assert sels and all(s.startswith('[data-screen="BROWSER"]') for s in sels), [s for s in sels if not s.startswith('[data-screen="BROWSER"]')][:5]
        assert not re.search(r"245\s*,\s*158\s*,\s*11|#f59e0b|#ffb224", css.lower()), "use var(--dm-active)"

    def test_the_split_has_no_global_listener_and_binds_its_key_through_ctx(self):
        c = code("split.js")
        assert "ctx.keys.bind('Alt+j'" in c
        assert not re.search(r"(document|window)\s*\.\s*addEventListener", c)
        for key in ("splitRatio", "splitSide", "splitCollapsed"):
            assert f"ctx.prefs.set('{key}'" in c, key
        assert "ctx.prefs.get('splitRatio')" in c and "ctx.prefs.get('splitSide')" in c and "ctx.prefs.get('splitCollapsed')" in c

    def test_the_model_stays_pure(self):
        c = code("split-model.js")
        assert not re.search(r"\b(document|window|navigator|localStorage|ctx)\b", c)


class TestWiring:
    def test_the_native_view_is_placed_from_the_left_region_only(self):
        c = code("index.js")
        assert "import { createSplit, loadSplitStyles, removeSplitStyles } from './split.js';" in c
        assert "viewBounds(pr, split.clip(br))" in c, "the clip is the left region and never the overlay"
        assert "root.clientWidth" not in c, "widths follow the left region, not the whole screen"
        assert c.count("split.availWidth()") == 3
        assert "split.dispose();" in c and "split.setContext(v);" in c

    def test_dragging_the_divider_hides_the_native_view_like_the_width_handle_does(self):
        c = code("index.js")
        m = re.search(r"onDragging:\s*\(on\)\s*=>\s*\{\s*dragging = on;\s*applyView\(\);", c)
        assert m, "the native view would swallow the pointer, so it steps aside while dragging"

    def test_every_layout_change_re_places_the_view(self):
        assert "onLayout: () => schedule()" in code("index.js")
        assert "if (onLayout) onLayout();" in code("split.js")

    def test_the_stylesheet_is_loaded_before_the_first_measurement_and_removed_on_exit(self):
        c = code("index.js")
        assert c.index("await loadSplitStyles()") < c.index("createSplit({")
        assert "removeSplitStyles();" in c
        assert "removeSplitStyles();" in code("split.js")

    def test_the_old_control_bar_stays_in_the_page_chrome_and_a_second_one_is_in_the_column_header(self):
        assert "controlUi = createControl({ control: pane.control, note })" in code("index.js")
        assert "createControl({ control, note })" in code("split-chat.js")
        assert "control: hasC2 ? pane.control : null" in code("index.js")
        assert '.bw-split[data-chat-visible="1"] .cb-control-slot { display: none; }' in src("split.css")


class TestTheChatColumnSource:
    def test_every_message_goes_to_the_browser_agent(self):
        c = code("split-chat.js")
        assert "focusAgent: 'browser'" in c
        assert "return ctx.chat.send(t, { focusAgent: 'browser', ...(opts || {}) });" in c
        assert "waiting.push(t);" in c and "drainWaiting" in c, "a line typed while the model works keeps its route"
        assert "isImperativeAffirm(t)" in c, "yes still answers an open question"

    def test_the_shells_bottom_box_is_not_used_and_the_thread_view_cannot_bring_it_back(self):
        c = code("split-chat.js")
        assert "ctx.chrome.setComposer(null)" in c
        assert re.search(r"setComposer\(o\)\s*\{\s*if \(!o\) return;\s*comp = o;", c), "the thread view's composer state lands in the column"
        assert "setActions() {}" in c and "setLive() {}" in c, "the stage bar and the sidebar dot are left to the screen"

    def test_the_column_uses_the_shells_thread_view_and_approval_cards(self):
        c = code("split-chat.js")
        assert "mountThreadView(" in c and "scroller: scroll" in c
        assert "newThread: false" in c and "autonomous: false" in c and "private: false" in c

    def test_a_pending_approval_makes_the_column_visible(self):
        assert "ctx.approvals.pending()" in code("split-chat.js")
        assert "if (s.attention) ensureVisible();" in code("split.js")

    def test_the_words_are_plain_and_name_what_the_buttons_do(self):
        text = src("split-chat.js") + src("split-model.js")
        for phrase in ("Tell the AI what to do", "asks you before anything risky", "Summarise this page", "Find the price", "Fill this form (I'll confirm)"):
            assert phrase in text or phrase.lower() in text.lower(), phrase
