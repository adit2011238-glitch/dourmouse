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

import json
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


class TestAvgTurnLatencyLabel:
    """v13: the HOME "AVG TURN TIME" metric — reuses chat.py's own
    per-turn elapsed_ms (already persisted, /api/session/current), no new
    timing mechanism. Real Node execution against the extracted function
    with a synthetic `sys` global, same discipline
    test_workspace_hand_gestures.py already established for pure functions
    in this codebase's frontend code."""

    def _extract(self) -> str:
        script = _extract_inline_script()
        m = re.search(r"function avgTurnLatencyLabel\(\)\{.*?\n\}\n", script, re.S)
        assert m, "avgTurnLatencyLabel() not found in ui/console.html's inline script"
        return m.group(0)

    def _run(self, tmp_path, sys_value: str) -> str:
        node = shutil.which("node")
        if not node:
            pytest.skip("node not on PATH in this environment")
        driver = f"let sys = {sys_value};\n{self._extract()}\nconsole.log(avgTurnLatencyLabel());"
        js_file = tmp_path / "avg_latency.js"
        js_file.write_text(driver, encoding="utf-8")
        result = subprocess.run(
            [node, str(js_file)], capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    def test_no_session_yet_shows_dash(self, tmp_path):
        assert self._run(tmp_path, "{session: null}") == "—"

    def test_404_shaped_session_shows_dash(self, tmp_path):
        assert self._run(tmp_path, '{session: {ok: false}}') == "—"

    def test_empty_turns_shows_dash(self, tmp_path):
        assert self._run(tmp_path, '{session: {ok: true, turns: []}}') == "—"

    def test_sub_second_average_shown_in_ms(self, tmp_path):
        sys_value = '{session: {ok: true, turns: [{elapsed_ms: 400}, {elapsed_ms: 600}]}}'
        assert self._run(tmp_path, sys_value) == "500ms"

    def test_multi_second_average_shown_in_seconds_one_decimal(self, tmp_path):
        sys_value = '{session: {ok: true, turns: [{elapsed_ms: 18900}, {elapsed_ms: 6900}]}}'
        assert self._run(tmp_path, sys_value) == "12.9s"

    def test_turns_missing_elapsed_ms_are_excluded_from_the_average(self, tmp_path):
        sys_value = '{session: {ok: true, turns: [{elapsed_ms: null}, {elapsed_ms: 800}]}}'
        assert self._run(tmp_path, sys_value) == "800ms"

    def test_slash_command_zero_elapsed_turns_do_not_skew_the_average(self, tmp_path):
        """chat.py's record_slash() persists elapsed_ms=0.0 ON PURPOSE
        (slash commands bypass the LLM loop entirely, see its own
        docstring) — a real bug caught before shipping: filtering only
        `!= null` would have kept these as genuine 0ms turns and dragged a
        real average toward zero. No genuine LLM turn ever lands on
        exactly 0ms, so `> 0` is the honest exclusion."""
        sys_value = '{session: {ok: true, turns: [{elapsed_ms: 0.0}, {elapsed_ms: 800}]}}'
        assert self._run(tmp_path, sys_value) == "800ms"

    def test_all_slash_commands_shows_dash_not_a_fabricated_zero(self, tmp_path):
        sys_value = '{session: {ok: true, turns: [{elapsed_ms: 0.0}, {elapsed_ms: 0.0}]}}'
        assert self._run(tmp_path, sys_value) == "—"


class TestCodeToolDefaultAndPersistence:
    """v13: the CODE screen's toolchain pin now survives a reload (real
    gap fixed, flagged earlier this session) and defaults to
    "code_claude" rather than "" AUTO — the user's own explicit
    instruction, given real measured numbers from this same session: local
    Ollama under real system load logged 33-94s per completion (ollama's
    own access log), NVIDIA is externally 403'ing on every real call, and
    Claude Code CLI is the one backend confirmed both signed-in and fast
    (7.6-8.1s per real call). Real Node execution against the extracted
    functions with a synthetic `localStorage`, same discipline this file's
    other pure-function tests already use."""

    def _extract_key(self) -> str:
        script = _extract_inline_script()
        m = re.search(r'const CODE_TOOL_KEY = "([^"]+)";', script)
        assert m, "CODE_TOOL_KEY constant not found in ui/console.html's inline script"
        return m.group(1)

    def _extract(self) -> str:
        script = _extract_inline_script()
        parts = []
        for name in ("function codeToolGet(){", "function codeToolSet(t){"):
            i = script.index(name)
            depth = 0
            j = i
            started = False
            while j < len(script):
                if script[j] == "{":
                    depth += 1
                    started = True
                elif script[j] == "}":
                    depth -= 1
                    if started and depth == 0:
                        j += 1
                        break
                j += 1
            parts.append(script[i:j])
        return "\n".join(parts)

    def _run(self, tmp_path, driver_tail: str) -> str:
        node = shutil.which("node")
        if not node:
            pytest.skip("node not on PATH in this environment")
        fake_storage = (
            "const _store = {};\n"
            "const localStorage = {\n"
            "  getItem: k => (k in _store ? _store[k] : null),\n"
            "  setItem: (k, v) => { _store[k] = String(v); },\n"
            "};\n"
            # The real CODE_TOOL_KEY constant declared alongside these
            # functions in the actual source — extracted separately so a
            # typo/rename in either place shows up as a real failure here.
            + f'const CODE_TOOL_KEY = {json.dumps(self._extract_key())};\n'
        )
        driver = f"{fake_storage}\n{self._extract()}\n{driver_tail}"
        js_file = tmp_path / "code_tool.js"
        js_file.write_text(driver, encoding="utf-8")
        result = subprocess.run(
            [node, str(js_file)], capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    def test_defaults_to_code_claude_when_nothing_stored(self, tmp_path):
        assert self._run(tmp_path, "console.log(codeToolGet());") == "code_claude"

    def test_set_then_get_round_trips(self, tmp_path):
        out = self._run(
            tmp_path,
            'codeToolSet("code_ollama"); console.log(codeToolGet());',
        )
        assert out == "code_ollama"

    def test_explicit_auto_choice_persists_as_empty_string_not_the_default(self, tmp_path):
        """A user who deliberately picks AUTO (id "") must stay on AUTO
        after a reload — the "code_claude" fallback is ONLY for genuinely
        untouched storage (the `!== null` check), never a re-guess
        overriding an explicit empty choice."""
        out = self._run(
            tmp_path,
            'codeToolSet(""); console.log(JSON.stringify(codeToolGet()));',
        )
        assert out == '""'
