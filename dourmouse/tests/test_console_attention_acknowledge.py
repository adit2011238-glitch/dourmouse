"""ui/console.html — the ACKNOWLEDGE button on NEEDS ATTENTION cards
(v14, user-directed, 2026-09-11): the still-open half of the Grounded
Mode noise problem. Plain DISMISS only ever clears one card; the exact
same zero-tool pattern fires again next turn on that screen. ACKNOWLEDGE
also teaches the system that screen's zero-tool answers are fine — see
dourmouse/webui.py's AttentionQueue.dismiss(acknowledge=True) and
test_attention_queue.py's real end-to-end HTTP proof.

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


class TestAttentionAcknowledgeButton:
    def test_the_button_only_renders_for_ungrounded_answer_items(self):
        script = _extract_inline_script()
        idx = script.index('const ackBtn')
        block = script[idx: idx + 300]
        assert 'it.kind === "ungrounded_answer"' in block

    def test_it_posts_acknowledge_true(self):
        script = _extract_inline_script()
        idx = script.index('.attnack')
        block = script[idx: idx + 600]
        assert '"/api/attention/dismiss"' in block
        assert "acknowledge: true" in block

    def test_a_non_ungrounded_item_never_gets_the_button(self):
        """The button string itself must be conditional (empty string
        for every other kind), not just visually hidden — never present
        in the DOM for a tool_error/timeout/etc item at all."""
        script = _extract_inline_script()
        idx = script.index('const ackBtn')
        block = script[idx: idx + 300]
        assert ': ""' in block
