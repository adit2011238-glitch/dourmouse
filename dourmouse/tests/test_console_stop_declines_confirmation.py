"""ui/console.html — STOP must free a session_lock stuck on a pending
confirmation (v13.2, live bug).

Real, reproduced behavior before this fix: WebConfirmationGate.__call__
(dourmouse/webui.py) blocks the whole chat request thread in
_PendingConfirmation.wait() for up to _CONFIRM_TIMEOUT_SECONDS (300s) while
holding server.session_lock (see webui.py's `with self.server.session_lock:`
around the dispatch call). The client's STOP button only called
`ctrl.abort()` on its OWN fetch — that only stops the browser from reading
the SSE stream, it does nothing server-side. The dispatch thread stayed
blocked in pending.wait(), so session_lock stayed held, so EVERY other
screen/agent's next request queued behind it for up to 5 minutes — the
"stop button doesn't work" symptom.

Fix: STOP now also auto-declines any confirmation box still open for the
in-flight request, via the same POST /api/confirm approved:false path a
manual DECLINE click already used (_resolve_confirmation in webui.py,
already covered by test_webui.py's confirm-endpoint tests) — this wakes
pending.wait() immediately and releases session_lock, exactly like a human
clicking DECLINE.

No headless browser here (none available in this suite, matching
test_console_session_restore.py's own stated convention) — this is
source-level coverage that the real wiring is present and syntactically
correct, plus a live-verified note in the code comments themselves.
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


class TestStopDeclinesPendingConfirmation:
    def test_pending_declines_map_declared(self):
        script = _extract_inline_script()
        assert "let pendingDeclines = new Map();" in script

    def test_run_resets_the_map_alongside_the_abort_controller(self):
        script = _extract_inline_script()
        m = re.search(r"ctrl = new AbortController\(\);(.*?)stopBtn\.onclick", script, re.S)
        assert m, "ctrl/pendingDeclines reset block not found before stopBtn.onclick"
        assert "pendingDeclines = new Map();" in m.group(1)

    def test_stop_click_aborts_and_declines(self):
        script = _extract_inline_script()
        m = re.search(r"stopBtn\.onclick = \(\)=>\{(.*?)\};", script, re.S)
        assert m, "stopBtn.onclick handler not found"
        body = m.group(1)
        assert "ctrl.abort()" in body
        # Must actually invoke every registered decline callback, not just
        # abort the client-side read.
        assert "pendingDeclines.values()" in body
        assert "decline()" in body

    def test_confirmation_requested_registers_into_the_map(self):
        script = _extract_inline_script()
        assert 'addApproval(node,e,pendingDeclines)' in script

    def test_add_approval_accepts_and_uses_the_decline_map(self):
        script = _extract_inline_script()
        m = re.search(r"function addApproval\(node, evt, declineMap\)\{(.*?)\n\}\n", script, re.S)
        assert m, "addApproval(node, evt, declineMap) not found with the new signature"
        body = m.group(1)
        # Registers a real decline callback keyed by this box's confirm id...
        assert "declineMap.set(evt.id" in body
        # ...and unregisters it once resolved either way (approve or decline
        # from the UI itself), so STOP can never double-resolve an
        # already-answered confirmation.
        assert "declineMap.delete(evt.id)" in body
