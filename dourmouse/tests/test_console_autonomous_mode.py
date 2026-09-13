"""ui/console.html — the "Autonomous Project" composer toggle (Phase 5,
bounded autonomous multi-step execution).

Real, explicit, off-by-default opt-in: a chip next to MIC/SPK in the
composer dockrow. When on, the main directive's POST /api/chat body
carries {"autonomous": true}, which webui.py's _handle_chat_authed reads
to raise the turn ceiling (max_turns=_AUTONOMOUS_MAX_TURNS instead of 8)
and set force_plain_dispatch=True (see dourmouse/webui.py and
dourmouse/dispatch.py for the backend half — this file covers only the
client-side wiring, source-level, matching every other test_console_*.py
file's own stated convention: no headless browser in this suite).
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


class TestAutonomousToggleExists:
    def test_the_chip_markup_exists(self):
        html = _CONSOLE_HTML.read_text(encoding="utf-8")
        assert 'id="autoProjectChip"' in html

    def test_off_by_default(self):
        script = _extract_inline_script()
        assert "let autonomousMode = false;" in script

    def test_the_tooltip_is_honest_about_what_it_does(self):
        """Real, checkable claims only — matches this app's own established
        honesty convention (Rule 2.1): no "AI will handle everything"
        marketing copy, a plain statement of the real turn cap, the real
        pause/resume behavior, and the real per-tab-only scope.

        Phase 6: this chip's tooltip moved from the plain OS title
        attribute to the new custom data-tip bubble (see [data-tip] in
        console.html's own <style> block) — same content, different
        delivery, so this checks the new attribute."""
        html = _CONSOLE_HTML.read_text(encoding="utf-8")
        m = re.search(r'id="autoProjectChip"[^>]*data-tip="([^"]+)"', html)
        assert m, "autoProjectChip has no data-tip tooltip"
        tooltip = m.group(1)
        assert "off by default" in tooltip.lower()
        assert "pausing for your approval" in tooltip.lower()
        assert "resuming automatically" in tooltip.lower()
        assert "this tab" in tooltip.lower()


class TestAutonomousToggleWiring:
    def test_clicking_the_chip_flips_the_flag_and_the_visual_state(self):
        script = _extract_inline_script()
        assert '$("autoProjectChip").onclick=' in script
        idx = script.index('$("autoProjectChip").onclick=')
        block = script[idx: idx + 200]
        assert "autonomousMode=!autonomousMode" in block
        assert '$("autoProjectChip").classList.toggle("on",autonomousMode)' in block

    def test_the_main_directive_sends_the_real_flag(self):
        """The one real functional wire: the composer's own fetch("/api/chat")
        call, not the specialized sendMail/design_3d ones (which the plan
        never asked to touch)."""
        script = _extract_inline_script()
        idx = script.index('fetch("/api/chat"')
        block = script[idx: idx + 400]
        assert "autonomous:autonomousMode" in block

    def test_not_persisted_across_reload(self):
        """Deliberate: unlike backendMode (localStorage-persisted),
        autonomousMode must never silently survive a reload — see the
        var's own comment for why (an honest opt-in shouldn't quietly
        carry over to a session the user doesn't remember arming it in)."""
        script = _extract_inline_script()
        idx = script.index("let autonomousMode = false;")
        block = script[max(0, idx - 400): idx]
        assert "localStorage" not in block


class TestAutonomousApprovalCopy:
    def test_addApproval_shows_the_extra_line_only_when_autonomous(self):
        script = _extract_inline_script()
        idx = script.index("function addApproval")
        block = script[idx: idx + 900]
        assert "evt.autonomous" in block
        assert "keep going automatically" in block

    def test_the_email_shortcut_box_is_unaffected(self):
        """The isEmail branch (a genuinely different, older UI shortcut)
        must not gain this line — only the ordinary APPROVE/DECLINE box
        the plan actually asked to change."""
        script = _extract_inline_script()
        idx = script.index("function addApproval")
        block = script[idx: idx + 900]
        email_branch_end = block.index("<div class=\"r\"><button class=\"btn\">CANCEL")
        assert "evt.autonomous" not in block[:email_branch_end]
