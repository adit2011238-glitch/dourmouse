"""ui/console.html — session restore on reload (world-monitor-expansion).

``GET /api/session/current`` (dourmouse/webui.py, 16f59ef) is a real,
already-tested backend endpoint (test_webui.py::TestSessionTranscriptEndpoint)
that serves the live ChatSession's own turn-by-turn transcript straight off
the ledger chat.py has always persisted. This file covers the frontend-only
work of actually reading it back on page boot and rebuilding HOME's thread
from it: real syntax on the extracted inline script (Rule 2.1 — no
fabricated pass), and that the real behavior this task called for is
actually present in the source — fetching the endpoint on boot, restoring
ONLY HOME's thread, replaying each transcript event through the same
tool_use/tool_result/... chip machinery a live turn uses, and silently
rendering nothing on the honest empty-session (404) case.

No headless browser here (none is available in this suite); DOM behavior
that would need one is out of scope for this file, matching
test_console_projects_import.py's own stated convention. It WAS live-
verified once, manually, via a real dourmouse.webui server: a real /api/chat
turn was driven through a fake tool-calling client, persisted to a real
session file, then a real page load (against the actual edited
ui/console.html, served by the real server) rebuilt HOME's thread from
GET /api/session/current — YOU/DOURMOUSE turns, the "N steps" disclosure,
the USING/RESULT chips (including the expandable tool_use argument detail),
final_text, and the COPY/REGENERATE/SHELVE buttons all rendered correctly,
and SHELVE was confirmed to still write into the existing localStorage
shelf. This file is the durable, repeatable regression coverage for that
same wiring.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CONSOLE_HTML = _PROJECT_ROOT / "ui" / "console.html"


def _extract_inline_script() -> str:
    html = _CONSOLE_HTML.read_text(encoding="utf-8")
    # See test_console_projects_import.py's own comment on why this must be
    # the non-greedy match against the FIRST <script>...</script> only —
    # console.html also has a later <script type="module"> + importmap.
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


class TestSessionRestore:
    """The real markers a genuine session-restore-on-reload must have."""

    def test_fetches_the_real_endpoint_on_boot(self):
        script = _extract_inline_script()
        assert '"/api/session/current"' in script
        assert "function restoreSession(" in script
        # Actually called during boot, not just defined and never invoked.
        assert "restoreSession();" in script

    def test_a_404_is_silent_not_an_error(self):
        script = _extract_inline_script()
        m = re.search(r"async function restoreSession\(\)\{(.*?)\n\}\n", script, re.S)
        assert m, "restoreSession() not found"
        body = m.group(1)
        assert "r.status===404" in body
        assert "return" in body

    def test_restores_each_turn_to_its_own_screens_thread(self):
        """v13: a real bug fixed — restore used to hardcode threadFor("HOME")
        for every turn, flattening RESEARCH/CODE/MEDIA/NEWS/DESIGN3D's own
        v8.29 per-screen conversations onto HOME on every reload. The turn's
        persisted `screen` field (chat.py's ask()/_persist(), threaded
        through /api/session/current) now picks the thread, gated through
        THREAD_SCREENS exactly like a live run() does, with HOME as the
        fallback for an unrecognized/missing screen (older records predate
        the field and always meant HOME anyway)."""
        script = _extract_inline_script()
        m = re.search(r"async function restoreSession\(\)\{(.*?)\n\}\n", script, re.S)
        assert m
        body = m.group(1)
        assert "THREAD_SCREENS.includes(turn.screen)" in body
        assert 'targetScreen = THREAD_SCREENS.includes(turn.screen) ? turn.screen : "HOME"' in body
        assert "threadFor(targetScreen)" in body

    def test_replays_transcript_through_the_same_chip_machinery(self):
        script = _extract_inline_script()
        assert "function restoreTranscriptEvent(" in script
        m = re.search(r"function restoreTranscriptEvent\(node, e\)\{(.*?)\n\}\n", script, re.S)
        assert m, "restoreTranscriptEvent() not found"
        body = m.group(1)
        # Same case set the live SSE switch in run() dispatches into real
        # act()/node.mdl/node.noteStep() chip calls.
        for case in (
            '"tool_use"', '"function"', '"tool_result"', '"result"',
            '"delegate_parallel_branch"', '"plan"', '"error"', '"brain"',
        ):
            assert case in body, f"missing transcript-event case: {case}"
        assert "act(node.acts," in body
        assert "node.noteStep()" in body

    def test_rebuilds_you_and_reply_turns(self):
        script = _extract_inline_script()
        m = re.search(r"async function restoreSession\(\)\{(.*?)\n\}\n", script, re.S)
        assert m
        body = m.group(1)
        assert "addReply(threadEl)" in body
        assert "turn.final_text" in body
        assert "turn.transcript" in body

    def test_you_bubble_shows_display_text_not_the_internal_wrapper(self):
        """v13: a real bug fixed — a focus_agent turn's persisted `user`
        field is what the MODEL saw (webui.py wraps it into a "[ROUTING
        DIRECTIVE] Complete this task using ONLY the '<agent>' subagent..."
        instruction before session.ask() ever runs), and restore used to
        show that raw internal instruction to the user as if they'd typed
        it. `turn.display_text` (chat.py's new, additive field) is the
        original unwrapped text; older records without it fall back to
        `turn.user`, unchanged from before."""
        script = _extract_inline_script()
        m = re.search(r"async function restoreSession\(\)\{(.*?)\n\}\n", script, re.S)
        assert m
        body = m.group(1)
        assert 'turn.display_text || turn.user || ""' in body
        assert "addYou(threadEl, shown)" in body

    def test_restored_reply_gets_the_same_tail_buttons_as_a_live_one(self):
        script = _extract_inline_script()
        assert "function restoreReplyTail(" in script
        m = re.search(r"function restoreReplyTail\(node, text, buf, targetScreen\)\{(.*?)\n\}\n", script, re.S)
        assert m, "restoreReplyTail() not found"
        body = m.group(1)
        assert "COPY" in body
        assert "REGENERATE" in body
        assert "SHELVE" in body
        assert "shelvePicker(" in body

    def test_unrecognized_screen_falls_back_to_home(self):
        """A turn persisted before the `screen` field existed (or one
        carrying a screen name that isn't a real THREAD_SCREENS entry) must
        still restore somewhere visible rather than being silently dropped
        — HOME, exactly as every turn used to restore before this fix."""
        script = _extract_inline_script()
        m = re.search(r"async function restoreSession\(\)\{(.*?)\n\}\n", script, re.S)
        assert m
        body = m.group(1)
        assert ': "HOME"' in body
