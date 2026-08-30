"""ui/console.html — email-send confirmations drop the APPROVE button (v13.2).

Explicit user request: sending an email should need exactly ONE thing —
typing "send it" in the directive box — not a separate click. The server
side of this already existed (webui.py's _is_imperative_affirm resolves
ANY pending confirmation from a short exact-match phrase like "send it");
this is the client no longer offering a shortcut around it for the two
real email-sending tools specifically.

_isEmailSendPrompt() detects gmail_send/email_own_send by their exact
confirm_prompt wording (general_roster.py owns both strings verbatim) —
chosen over threading a tool-name field through the confirmation_gate
interface (Callable[[str], bool] everywhere, including ~20 single-arg
test fakes across the suite) for what is purely a cosmetic UI split.
Every OTHER confirmation-gated tool (delete_path, write_note,
run_privileged_command, ...) keeps the normal APPROVE/DECLINE box
unchanged.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CONSOLE_HTML = _PROJECT_ROOT / "ui" / "console.html"
_GENERAL_ROSTER = _PROJECT_ROOT / "dourmouse" / "general_roster.py"


def _extract_inline_script() -> str:
    html = _CONSOLE_HTML.read_text(encoding="utf-8")
    m = re.search(r"<script>(.*?)</script>", html, re.S)
    assert m, "ui/console.html has no inline <script>...</script> block"
    return m.group(1)


class TestConsoleScriptSyntax:
    def test_node_check_passes(self, tmp_path):
        node = shutil.which("node")
        if not node:
            pytest.skip("node not on PATH in this environment")
        script = _extract_inline_script()
        js_file = tmp_path / "console_extracted.js"
        js_file.write_text(script, encoding="utf-8")
        result = subprocess.run(
            [node, "--check", str(js_file)],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, (
            f"node --check failed on the extracted console.html script:\n"
            f"{result.stdout}\n{result.stderr}"
        )


class TestEmailSendPromptDetectionMatchesTheRealWording:
    """The two detector prefixes must actually match what general_roster.py
    generates today — a wording change on either side silently breaking
    the other is exactly the failure mode this guards against."""

    def test_gmail_send_confirm_prompt_starts_with_the_detected_prefix(self):
        roster_src = _GENERAL_ROSTER.read_text(encoding="utf-8")
        m = re.search(r'confirm_prompt=lambda a: \(\s*f"(Send Gmail to [^"]*)"', roster_src)
        assert m, "gmail_send's confirm_prompt wording not found or changed shape"

    def test_email_own_send_confirm_prompt_starts_with_the_detected_prefix(self):
        roster_src = _GENERAL_ROSTER.read_text(encoding="utf-8")
        m = re.search(
            r'confirm_prompt=lambda a: \(\s*f"(Send mail FROM the Dourmouse identity[^"]*)"',
            roster_src,
        )
        assert m, "email_own_send's confirm_prompt wording not found or changed shape"


class TestAddApprovalDropsTheButtonForEmail:
    def test_detector_function_present(self):
        script = _extract_inline_script()
        assert "function _isEmailSendPrompt(promptText)" in script
        assert 'p.startsWith("Send Gmail to ")' in script
        assert 'p.startsWith("Send mail FROM the Dourmouse identity")' in script

    def test_email_box_has_no_approve_button_only_cancel(self):
        script = _extract_inline_script()
        m = re.search(r"const isEmail = _isEmailSendPrompt\(evt\.prompt\);\s*box\.innerHTML = isEmail\s*\?\s*`(.*?)`\s*:", script, re.S)
        assert m, "email-branch innerHTML template not found"
        email_html = m.group(1)
        assert "APPROVE" not in email_html
        assert "CANCEL" in email_html
        assert "send it" in email_html

    def test_non_email_box_keeps_the_normal_approve_decline_buttons(self):
        script = _extract_inline_script()
        m = re.search(r": `(<div class=\"t\">◆ APPROVAL REQUIRED</div>.*?)`;", script, re.S)
        assert m, "non-email-branch innerHTML template not found"
        normal_html = m.group(1)
        assert "APPROVE" in normal_html
        assert "DECLINE" in normal_html

    def test_email_branch_wires_only_a_decline_callback(self):
        script = _extract_inline_script()
        m = re.search(r"if\(isEmail\)\{(.*?)\} else \{", script, re.S)
        assert m, "isEmail branch in addApproval's button wiring not found"
        body = m.group(1)
        assert "decide(false)" in body
        assert "decide(true)" not in body
