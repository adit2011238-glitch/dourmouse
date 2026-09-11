"""ui/console.html — the thinking-trace box stays collapsed by default
(v14, user-directed, 2026-09-08).

Real UX complaint: the visible chain-of-thought box (v13.1) used to
force itself OPEN the instant the first thinking_delta arrived on every
single turn — intrusive on every turn, not something the user asked to
happen automatically each time. Fixed by (1) starting the toggle's own
arrow glyph collapsed (▸, not ▾) and (2) no longer flipping the panel's
own display to "block" on the first token — the toggle still appears
the moment there is real content, the panel just no longer forces
itself open. No latency impact either way (it's a pure display toggle
on already-arrived tokens, not a network/inference change).

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


class TestThinkingCollapsedByDefault:
    def test_initial_arrow_glyph_is_collapsed(self):
        html = _CONSOLE_HTML.read_text(encoding="utf-8")
        assert '<span class="tcar">▸</span> THINKING' in html

    def test_first_thinking_delta_no_longer_forces_the_panel_open(self):
        script = _extract_inline_script()
        idx = script.index('case "thinking_delta"')
        block = script[idx: idx + 1400]
        # The old behavior set node.think.style.display="block" and the
        # arrow to "▾" right inside this handler — neither should be
        # here any more.
        assert 'node.think.style.display="block"' not in block
        assert '"▾"' not in block

    def test_the_toggle_still_appears_once_there_is_real_content(self):
        script = _extract_inline_script()
        idx = script.index('case "thinking_delta"')
        block = script[idx: idx + 1400]
        assert 'node.thinkToggle.style.display="inline-flex"' in block
