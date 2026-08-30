"""ui/console.html — typewriter reveal for bursty backends (v13.2).

Live-measured (a Python script timestamping every SSE event against the
real running server, not a guess): the "cloud" backend does not deliver
assistant_delta/thinking_delta incrementally over wall-clock time at all.
Ollama Cloud buffers the ENTIRE response (reasoning AND content) and
bursts hundreds of delta events within sub-millisecond of each other the
instant generation finishes -- confirmed both on a short question (39
thinking_delta events spanning 0.55ms total) and a long essay (two
completion calls, each showing brain_thinking heartbeats every 3s with
ZERO delta events, then an 80-token thinking burst + 400-token content
burst arriving within ~10ms).

The old code (buf+=e.text; paint()) was faithfully live -- the backend
just never gave it anything to be live WITH, so the UI sat on "THINKING"
for 10-20s and then the whole answer slammed onto the screen in one
paint(). makeRevealer() decouples "when text arrived" from "when text is
shown": a fixed-cadence ticker drains a pending queue into the visible
buffer a few characters at a time, giving the live-token feel the user
explicitly asked for ("the exact same experience [as Claude Desktop]...
quick token display") regardless of how bursty the transport actually is.

No headless browser here (none available in this suite, matching
test_console_session_restore.py's own stated convention) -- source-level
coverage that the real wiring is present and syntactically correct.
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


class TestMakeRevealer:
    def test_defined_with_expected_shape(self):
        script = _extract_inline_script()
        assert "function makeRevealer(onUpdate, opts)" in script
        for member in ("enqueue(text)", "setTotal(text)", "flushAll()", "finish()"):
            assert member in script, member

    def test_chunk_size_scales_with_backlog_and_is_bounded(self):
        # The whole point: an arbitrarily large one-shot burst must still
        # finish revealing in bounded time (maxTicks*tickMs), not type out
        # a multi-thousand-character essay one character at a time.
        script = _extract_inline_script()
        m = re.search(r"function makeRevealer\(onUpdate, opts\)\{(.*?)\n\}\n", script, re.S)
        assert m, "makeRevealer body not found"
        body = m.group(1)
        assert "Math.ceil(pending.length / maxTicks)" in body
        assert "Math.max(baseChunk" in body

    def test_set_total_only_enqueues_the_new_suffix(self):
        script = _extract_inline_script()
        m = re.search(r"setTotal\(text\)\{(.*?)\n *\},", script, re.S)
        assert m, "setTotal not found"
        body = m.group(1)
        assert "revealed.length + pending.length" in body
        assert "text.slice(known)" in body

    def test_functionally_reveals_progressively_not_all_at_once(self, tmp_path):
        """Real execution, not just source grep: feed one huge burst into
        a revealer and confirm it takes multiple ticks to fully reveal,
        with the visible text strictly growing along the way -- proving
        this is an actual typewriter, not a same-tick pass-through."""
        node = shutil.which("node")
        if not node:
            pytest.skip("node not on PATH in this environment")
        script = _extract_inline_script()
        m = re.search(r"(function makeRevealer\(onUpdate, opts\)\{.*?\n\}\n)", script, re.S)
        assert m, "makeRevealer body not found"
        harness = m.group(1) + """
const seen = [];
const r = makeRevealer((text)=>{ seen.push(text.length); }, {tickMs: 1, baseChunk: 4, maxTicks: 20});
r.enqueue("x".repeat(2000));
let ticks = 0;
const iv = setInterval(()=>{
  ticks++;
  if(!r.draining || ticks > 200){
    clearInterval(iv);
    const distinctLengths = new Set(seen);
    const ok = distinctLengths.size >= 5 && seen[seen.length-1] === 2000 &&
      seen.every((v,i)=> i===0 || v >= seen[i-1]);
    console.log(JSON.stringify({ok, updates: seen.length, finalLen: seen[seen.length-1], distinct: distinctLengths.size}));
    process.exit(ok ? 0 : 1);
  }
}, 2);
"""
        js_file = tmp_path / "revealer_harness.js"
        js_file.write_text(harness, encoding="utf-8")
        result = subprocess.run([node, str(js_file)], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, f"typewriter did not reveal progressively:\n{result.stdout}\n{result.stderr}"


class TestRunWiresRevealersInsteadOfDirectBufMutation:
    def test_content_and_think_revealers_constructed(self):
        script = _extract_inline_script()
        assert "const contentRevealer = makeRevealer((text)=>{ buf=text; paint(); });" in script
        assert "thinkBuf=text; paintThink();" in script

    def test_case_handlers_go_through_the_revealers(self):
        script = _extract_inline_script()
        assert 'case "assistant_delta": foldThink(); contentRevealer.enqueue(e.text||""); break;' in script
        assert 'case "assistant_text": foldThink(); contentRevealer.setTotal(e.text||""); break;' in script
        assert 'case "done": if(e.final_text) contentRevealer.setTotal(e.final_text); break;' in script
        assert "thinkRevealer.enqueue(e.text||\"\");" in script

    def test_stop_flushes_both_revealers_immediately(self):
        script = _extract_inline_script()
        m = re.search(r"stopBtn\.onclick = \(\)=>\{(.*?)\};", script, re.S)
        assert m
        body = m.group(1)
        assert "contentRevealer.flushAll(); thinkRevealer.flushAll();" in body

    def test_finally_waits_for_the_natural_drain_before_final_render(self):
        script = _extract_inline_script()
        m = re.search(r"\}finally\{(.*?)clearInterval\(tick\);", script, re.S)
        assert m, "finally block preamble not found"
        body = m.group(1)
        assert "contentRevealer.finish(); thinkRevealer.finish();" in body
        assert "contentRevealer.draining || thinkRevealer.draining" in body
        assert "contentRevealer.flushAll(); thinkRevealer.flushAll();" in body
