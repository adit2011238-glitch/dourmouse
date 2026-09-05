"""ui/console.html had no way to reach a real ALL HANDS run (v13.8, real
live-reproduced gap).

Live-reproduced: sending "/all check disk space, get bitcoin price, and get
news headlines" through the real console genuinely started a real ALL HANDS
run server-side (dourmouse/all_hands.py, a real run_id) and the chat answer
said "ALL HANDS STARTED ... Progress is streaming in the ALL HANDS window."
-- but no such window opened, and none could: ui/index.html, ui/workspace.html
and ui/all_hands.html all have a real `case 'allhands_started'` handler
(index.html opens `/all-hands?run=<id>` in a real panel), but ui/console.html
-- the actual default UI (`/` serves console per v8.7) -- had ZERO handling
for this event type at all, so it silently fell through the SSE switch. The
run genuinely happens; on this UI there was no way to see or reach it. This
directly matches the user's own reported complaint, verbatim: "the multi
prompt feature doesnt really work."

Fix: a real `case "allhands_started"` in console.html's event switch,
inserting a clickable link to the SAME `/all-hands?run=<id>` route the other
frontends already use -- the smallest safe surface that actually closes the
gap, without porting the whole in-page iframe panel system these other pages
use.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CONSOLE_HTML = _PROJECT_ROOT / "ui" / "console.html"
_WEBUI_PY = _PROJECT_ROOT / "dourmouse" / "webui.py"


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
            [node, "--check", str(js_file)], capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, (
            f"node --check failed:\n{result.stdout}\n{result.stderr}"
        )


class TestAllHandsEventIsHandled:
    def test_a_real_case_exists_in_the_event_switch(self):
        script = _extract_inline_script()
        assert 'case "allhands_started"' in script

    def test_it_links_to_the_real_server_route(self):
        """dourmouse/webui.py actually serves /all-hands (checked directly
        against that file, not assumed) -- the link must point at the same
        route the other frontends already use, not a guessed path."""
        webui = _WEBUI_PY.read_text(encoding="utf-8")
        assert '"/all-hands"' in webui or "'/all-hands'" in webui
        script = _extract_inline_script()
        assert '"/all-hands?run="' in script

    def test_it_reads_the_real_field_names_the_server_emits(self):
        """webui.py emits {"type": "allhands_started", "run_id": ...} --
        checked directly against that file so this test can't silently
        drift from the real wire shape."""
        webui = _WEBUI_PY.read_text(encoding="utf-8")
        assert '"type": "allhands_started", "run_id"' in webui
        script = _extract_inline_script()
        m = re.search(r'case "allhands_started":\{(.*?)\n\s*break; \}', script, re.S)
        assert m, "allhands_started case body not found"
        body = m.group(1)
        assert "e.run_id" in body

    def test_the_link_opens_in_a_new_tab_not_navigating_away_from_the_console(self):
        script = _extract_inline_script()
        m = re.search(r'case "allhands_started":\{(.*?)\n\s*break; \}', script, re.S)
        assert m
        body = m.group(1)
        assert 'target="_blank"' in body
        assert 'rel="noopener"' in body

    def test_the_box_is_inserted_before_node_body_so_a_later_paint_cannot_wipe_it(self):
        """Matches addApproval's own established pattern (see that
        function's own comment) -- content set via node.body.innerHTML in
        paint() would silently delete anything inserted INSIDE node.body,
        so this must be a sibling, not a child."""
        script = _extract_inline_script()
        m = re.search(r'case "allhands_started":\{(.*?)\n\s*break; \}', script, re.S)
        assert m
        body = m.group(1)
        assert "node.body.parentNode.insertBefore(box, node.body)" in body

    def test_run_id_is_html_escaped_before_insertion(self):
        """A run_id is server-generated (uuid hex), never user text, but
        the escape discipline should still be there -- consistent with
        every other real value this file interpolates into innerHTML."""
        script = _extract_inline_script()
        m = re.search(r'case "allhands_started":\{(.*?)\n\s*break; \}', script, re.S)
        assert m
        body = m.group(1)
        assert "esc(e.run_id)" in body
        assert "esc(href)" in body
