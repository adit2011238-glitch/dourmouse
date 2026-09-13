"""ui/console.html — a real nav link to the existing /study page (v14,
user-directed, 2026-09-08).

Real bug found live-testing this session: ui/study.html (backlog #9) is a
complete, working, standalone Study chat page — served at GET /study —
but it was NEVER a SCREENS/show() entry and NEVER linked from anywhere in
console.html's own navigation. A user driving the app through its normal
tabs had no way to discover it existed at all. Fixed by adding a real
overflow-menu button (same "open in new tab" pattern GLOBE's own God's
Eye View link already uses) that opens /study directly — not a new
show(name) screen, since the page has its own separate layout/theme/
streaming logic that was never meant to live inside the SPA.

2026-09-14 (live-caught, user-directed): "study tab doesnt work nor
open" — the plain window.open() above silently no-ops inside pywebview's
WKWebView, the real desktop app this ships in (it only ever worked from
an ordinary browser tab, which is why this wasn't caught before). Now
feature-detects the same window.pywebview.api.* bridge every other
cross-window open in this file already uses, falling back to the
original window.open for a plain browser tab.

No headless browser here (none available in this suite, matching every
other test_console_*.py file's own stated convention) — source-level
coverage that the real wiring is present and correct.
"""

from __future__ import annotations

import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CONSOLE_HTML = _PROJECT_ROOT / "ui" / "console.html"


def _extract_inline_script() -> str:
    html = _CONSOLE_HTML.read_text(encoding="utf-8")
    m = re.search(r"<script>(.*?)</script>", html, re.S)
    assert m, "ui/console.html has no inline <script>...</script> block"
    return m.group(1)


class TestStudyNavLink:
    def test_a_study_button_is_added_to_the_overflow_menu(self):
        script = _extract_inline_script()
        assert "studyBtn" in script
        assert "tabsMoreMenu.appendChild(studyBtn)" in script

    def test_it_opens_the_real_study_page_in_a_new_tab_from_a_browser(self):
        """The fallback path — still real, still there, for a plain
        browser tab where window.pywebview does not exist at all."""
        script = _extract_inline_script()
        idx = script.index("const studyBtn")
        block = script[idx: idx + 1100]
        assert 'window.open("/study", "_blank", "noopener")' in block

    def test_it_prefers_the_real_native_window_bridge_when_present(self):
        """The actual live bug fix: pywebview's WKWebView (the real
        desktop app) silently ignores window.open, so this must reach for
        the same bridge open_agent/open_all_hands already use before
        ever falling back to window.open."""
        script = _extract_inline_script()
        idx = script.index("const studyBtn")
        block = script[idx: idx + 1100]
        assert "window.pywebview.api.open_study()" in block

    def test_it_closes_the_overflow_menu_after_opening(self):
        script = _extract_inline_script()
        idx = script.index("const studyBtn")
        block = script[idx: idx + 1100]
        assert 'tabsMoreMenu.classList.remove("on")' in block
