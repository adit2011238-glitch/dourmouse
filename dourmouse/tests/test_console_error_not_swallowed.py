"""ui/console.html — a real error must reach the visible reply, not just
the collapsed steps disclosure (live bug, feature sweep 2026-09-07).

Real, reproduced behavior before this fix: a genuine server-side error
(e.g. "HTTP Error 429: Too Many Requests" from a rate-limited backend, or
a real local-model timeout) arrives over SSE as `{"type": "error",
"message": "..."}`. The handler for that event DID already record it —
into node.acts (the "steps" disclosure, style="display:none" until the
user clicks it open) and into the live activity side panel — but never
into `buf`, the text the main chat bubble actually renders. When the
turn then ended with nothing else generated, `md(buf) || "<p><em>No
reply.</em></p>"` fell to the generic fallback: the user saw a bare "No
reply." with zero indication anything failed, let alone why, unless they
happened to click the collapsed toggle.

Verified live against a real 429 and a real 240s local-model timeout
during this session's own testing — both showed as "No reply." with the
real error only visible in `.acts .act.bad .nm` in the DOM.

Fix: the "error" case now also stores the message in `lastError`; the
fallback shows it instead of the bare "No reply." when it's genuinely
what happened, while a real empty response with no error keeps the
honest generic message (never inventing a cause that wasn't there).

No headless browser here (none available in this suite, matching
test_console_stop_declines_confirmation.py's own stated convention) —
source-level coverage that the real wiring is present and correct.
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


class TestErrorReachesTheVisibleReply:
    def test_error_case_records_lasterror(self):
        script = _extract_inline_script()
        m = re.search(r'case "error":(.*?)break;', script, re.S)
        assert m, '"error" case not found in the SSE switch'
        assert "lastError = e.message" in m.group(1)

    def test_lasterror_declared_alongside_buf(self):
        script = _extract_inline_script()
        assert re.search(r"let buf=\"\".*lastError=\"\"", script), (
            "lastError must be declared in the same scope as buf, reset "
            "fresh for every new turn"
        )

    def test_fallback_shows_the_real_error_instead_of_a_bare_no_reply(self):
        script = _extract_inline_script()
        # The live-streaming fallback (not restoreSession's replay path,
        # which has no lastError to show) must branch on lastError.
        assert 'lastError\n      ? `<p><em>No reply — ${esc(lastError)}</em></p>`' in script

    def test_genuinely_empty_response_with_no_error_keeps_the_honest_generic_message(self):
        """Never invent a cause that wasn't there — a real empty response
        with lastError still "" must fall through to the plain message,
        not blame a phantom error."""
        script = _extract_inline_script()
        assert ': "<p><em>No reply.</em></p>")' in script
