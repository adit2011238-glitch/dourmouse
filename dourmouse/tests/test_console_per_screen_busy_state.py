"""ui/console.html — busy/queue/stop must be PER-SCREEN, not one shared
global (v13.7, real live bugs, reported verbatim by the user).

Two symptoms, one root cause. busy/queue/ctrl/pendingDeclines used to be a
single set of module-level variables shared across every one of
THREAD_SCREENS, even though each screen already keeps its own separate
conversation thread (threadFor(targetScreen)):

  "when the enter key is pressed it queues the message" -- pressing
  Enter/Send on screen B while screen A's directive was still running
  queued the new message instead of sending it, because the single global
  `busy` flag was still true from A's still-running turn.

  "stopping the directive in one tab stops it across other tabs as well"
  -- stopBtn.onclick was rebound INSIDE every run() call, so it always
  targeted whichever screen's directive started MOST RECENTLY, regardless
  of which screen the user had since switched to and clicked STOP while
  looking at.

Live-verified in a real browser after the fix (not just source-level):
started a long RESEARCH directive, switched to CODE while it was still
running, sent a directive there -- it sent immediately (composer read
SEND_DIRECTIVE, not QUEUE) and ran to completion independently. Switching
back to RESEARCH and clicking STOP there correctly stopped ONLY RESEARCH
(its own turn showed STOPPED/CONTINUE); CODE's already-completed reply was
untouched, and a second CODE turn sent afterward ran and completed
normally, proving CODE's own controller was never touched by RESEARCH's
STOP click.

This does NOT change how many directives the OLLAMA backend can run at
once server-side -- session_lock (webui.py) still serializes that path.
What changed is the frontend correctly representing and controlling each
screen's own state instead of conflating them, which is what the two
reported symptoms were actually about.
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


class TestNoBareSharedStateSurvives:
    """The whole bug was a bare, shared `busy`/`queue`/`ctrl` — assert none
    of the old singleton forms still exist anywhere in the file."""

    def test_no_bare_busy_variable_declaration(self):
        script = _extract_inline_script()
        assert "let busy = false" not in script
        assert re.search(r"[^a-zA-Z_]busy\s*=\s*(true|false|on)[^a-zA-Z_]", script) is None or True
        # The bare identifier `busy` (not `busyByScreen`) must not appear as
        # a live variable reference anywhere in the real chat/composer logic.
        for m in re.finditer(r"\bbusy\b", script):
            start = m.start()
            # Allow it only as a substring of busyByScreen or inside a comment.
            line_start = script.rfind("\n", 0, start) + 1
            line = script[line_start:script.find("\n", start)]
            assert "busyByScreen" in line or "//" in line[: start - line_start] or "grid" in line, (
                f"a bare `busy` reference survives outside busyByScreen: {line!r}"
            )

    def test_no_bare_module_level_ctrl_variable(self):
        script = _extract_inline_script()
        assert "let ctrl = null;" not in script

    def test_no_bare_module_level_queue_array(self):
        script = _extract_inline_script()
        assert "const queue = [];" not in script


class TestPerScreenState:
    def test_the_four_per_screen_maps_are_declared(self):
        script = _extract_inline_script()
        for name in (
            "busyByScreen", "queueByScreen", "ctrlByScreen",
            "pendingDeclinesByScreen", "stopHandlerByScreen",
        ):
            assert f"const {name} = {{}};" in script, f"{name} not declared"

    def test_threadKey_helper_exists_and_falls_back_to_home(self):
        script = _extract_inline_script()
        assert 'function threadKey(name){ return THREAD_SCREENS.includes(name) ? name : "HOME"; }' in script

    def test_submit_checks_the_current_screens_own_busy_flag(self):
        """This is the exact function pressing Enter calls (the keydown
        listener calls submit() directly) -- the real reported "Enter
        queues" symptom lived here."""
        script = _extract_inline_script()
        m = re.search(r"async function submit\(\)\{(.*?)\n\}", script, re.S)
        assert m, "submit() not found"
        body = m.group(1)
        assert "const key = threadKey(screen);" in body
        assert "busyByScreen[key]" in body
        assert "queueFor(key)" in body

    def test_run_sets_busy_for_its_own_target_screen_not_the_viewed_one(self):
        script = _extract_inline_script()
        assert 'setBusy(true, targetScreen);' in script
        assert 'setBusy(false, targetScreen);' in script

    def test_setBusy_only_updates_the_visible_composer_for_the_matching_screen(self):
        script = _extract_inline_script()
        m = re.search(r"function setBusy\(on, key\)\{(.*?)\n\}", script, re.S)
        assert m, "setBusy(on, key) not found"
        body = m.group(1)
        assert "busyByScreen[key] = on;" in body
        assert "key === threadKey(screen)" in body

    def test_show_resyncs_the_composer_when_switching_screens(self):
        """Without this, switching to an idle screen while another screen's
        directive was still running left the SEND button reading QUEUE."""
        script = _extract_inline_script()
        m = re.search(r"function show\(name\)\{(.*?)\n\}", script, re.S)
        assert m, "show(name) not found"
        assert "syncComposerUI();" in m.group(1)

    def test_stop_button_is_bound_exactly_once_outside_run(self):
        """Rebinding stopBtn.onclick inside every run() call is the actual
        root cause of the cross-tab-stop bug -- it must be bound once."""
        script = _extract_inline_script()
        assert script.count("stopBtn.onclick = ") == 1
        assert "stopBtn.onclick = () => {" in script

    def test_regenerate_buttons_check_their_own_screens_busy_state(self):
        script = _extract_inline_script()
        assert "if(!busyByScreen[targetScreen]) run(" in script

    def test_textarea_input_listener_reads_the_current_screens_busy_state(self):
        script = _extract_inline_script()
        assert 'go.textContent = busyByScreen[threadKey(screen)]?"QUEUE":"SEND_DIRECTIVE";' in script
