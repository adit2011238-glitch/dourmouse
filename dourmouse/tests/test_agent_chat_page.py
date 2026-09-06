"""Real, scoped checks for ui/agent_chat.html — the human-viewable half of
the agent-to-agent chat surface (backlog item 7). Not yet folded into
test_ui_contrast.py's full per-screen token matrix (LOGIN_TEXT_TOKENS-style)
— that's a real follow-up, not skipped by accident; this covers structure,
the shared design system link, and keyboard-focus visibility instead.
"""

from __future__ import annotations

from pathlib import Path


def _agent_chat_path() -> Path:
    return Path(__file__).resolve().parents[2] / "ui" / "agent_chat.html"


def _html() -> str:
    return _agent_chat_path().read_text(encoding="utf-8")


class TestAgentChatPageExists:
    def test_file_exists(self):
        assert _agent_chat_path().is_file()

    def test_links_the_shared_design_system_css(self):
        assert 'href="assets/dourmouse-ui.css"' in _html()

    def test_no_banned_ai_slop_patterns(self):
        html = _html().lower()
        assert "border-radius: 999" not in html
        assert "border-radius:999" not in html
        assert "purple" not in html
        assert "violet" not in html
        assert "linear-gradient" not in html

    def test_polls_the_real_messages_endpoint(self):
        assert "/api/messages" in _html()

    def test_has_a_send_form_that_posts(self):
        html = _html()
        assert 'id="send-form"' in html
        assert "method: 'POST'" in html

    def test_has_focus_visible_styling_on_interactive_controls(self):
        html = _html()
        assert "input[type=text]:focus-visible" in html
        assert "button:focus-visible" in html

    def test_escapes_message_content_before_inserting_into_the_dom(self):
        """Real XSS guard: an agent's own message body/from/to could
        contain HTML — must go through the esc() helper, never raw
        innerHTML interpolation of untrusted text."""
        html = _html()
        assert "function esc(" in html
        assert "esc(m.from)" in html
        assert "esc(m.body)" in html
