"""Finding #151: the CODE screen's pure helpers (node) and source rules."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_DIR = _ROOT / "ui" / "assets" / "os" / "screens" / "code"
_NODE = shutil.which("node")


def node(tmp_path, body):
    if _NODE is None:
        pytest.skip("node not on PATH in this environment")
    script = tmp_path / "h.mjs"
    script.write_text(f"import * as h from {(_DIR / 'helpers.js').as_uri()!r};\nconst R = {{}};\n{body}\nconsole.log(JSON.stringify(R));\n", encoding="utf-8")
    proc = subprocess.run([_NODE, str(script)], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestParseDiff:
    def test_numbers_and_counts_follow_the_hunk_header(self, tmp_path):
        out = node(tmp_path, r"""
const d = h.parseDiff('diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -10,3 +10,4 @@ def f():\n a\n-b\n+B\n+c\n d\n');
R.kinds = d.rows.map((r) => r.k);
R.add = d.rows.filter((r) => r.k === 'add').map((r) => r.n);
R.del = d.rows.filter((r) => r.k === 'del').map((r) => r.o);
R.ctx = d.rows.filter((r) => r.k === 'ctx').map((r) => [r.o, r.n]);
R.counts = [d.added, d.deleted, d.hidden];
""")
        assert out["kinds"] == ["meta", "meta", "meta", "hunk", "ctx", "del", "add", "add", "ctx"]
        assert out["add"] == [11, 12] and out["del"] == [11]
        assert out["ctx"] == [[10, 10], [12, 13]]
        assert out["counts"] == [2, 1, 0]

    def test_a_file_line_starting_with_dashes_is_content_inside_a_hunk_not_a_header(self, tmp_path):
        out = node(tmp_path, r"R.d = h.parseDiff('@@ -1,1 +1,1 @@\n--- not a header\n+++ also content\n').rows.map((r) => r.k);")
        assert out["d"] == ["hunk", "del", "add"]

    def test_row_cap_reports_what_it_hid(self, tmp_path):
        out = node(tmp_path, r"const t = '@@ -1,0 +1,10 @@\n' + Array.from({ length: 10 }, (_, i) => '+' + i).join('\n'); const d = h.parseDiff(t, 4); R.n = [d.rows.length, d.hidden, d.added];")
        assert out["n"] == [4, 7, 10]

    def test_a_200kb_diff_of_brackets_is_linear(self, tmp_path):
        out = node(tmp_path, r"""
const big = '@@ -1 +1 @@\n' + '+' + '['.repeat(200000) + '\n' + ('-[\n'.repeat(20000));
const t0 = Date.now(); const d = h.parseDiff(big); R.ms = Date.now() - t0; R.a = d.added;
""")
        assert out["ms"] < 1000 and out["a"] == 1

    def test_empty_and_missing_input(self, tmp_path):
        out = node(tmp_path, "R.a = h.parseDiff('').rows.length; R.b = h.parseDiff(null).rows.length;")
        assert out == {"a": 0, "b": 0}


class TestLabels:
    def test_status_words_and_tones(self, tmp_path):
        out = node(tmp_path, "R.w = ['M','A','D','?','Z'].map(h.statusWord); R.t = ['M','A','D','?','U'].map(h.statusTone);")
        assert out["w"] == ["modified", "added", "deleted", "new, untracked", "changed"]
        assert out["t"] == ["warn", "ok", "bad", "ok", "bad"]

    def test_totals_line_uses_the_servers_numbers(self, tmp_path):
        out = node(tmp_path, "R.a = h.totalsLine({ total: 1, added: 3, deleted: 0 }); R.b = h.totalsLine({ total: 0, added: 0, deleted: 0 }); R.c = h.totalsLine(null);")
        assert out == {"a": "1 file, +3 -0", "b": "0 files, +0 -0", "c": ""}


class TestTerminal:
    def test_newest_code_tool_steps_first_and_other_tools_ignored(self, tmp_path):
        out = node(tmp_path, r"""
const turns = [
  { steps: [{ kind: 'tool', name: 'run_python', result: 'a' }, { kind: 'tool', name: 'web_search', result: 'x' }] },
  { steps: [{ kind: 'plan' }, { kind: 'tool', name: 'claude_code', result: 'b' }, { kind: 'tool', name: 'run_python', result: 'c' }] },
];
R.names = h.terminalSteps(turns, 3).map((s) => s.result);
R.one = h.terminalSteps(turns, 1).map((s) => s.result);
R.none = h.terminalSteps([], 3).length;
""")
        assert out == {"names": ["c", "b", "a"], "one": ["c"], "none": 0}

    def test_a_running_or_failed_step_is_never_reported_as_returned(self, tmp_path):
        out = node(tmp_path, "R.s = [{ running: true }, { ok: false }, { result: 'ERROR: boom' }, { result: 'fine' }, {}].map(h.stepExit);")
        assert out["s"] == ["running", "failed", "failed", "returned", "returned"]


class TestProject:
    def test_scope_path_then_saved_then_first(self, tmp_path):
        out = node(tmp_path, r"""
const list = [{ id: 'self', path: '/a' }, { id: 'x1', path: '/b' }];
R.a = h.pickProject(list, { path: '/b' }, 'self'); R.b = h.pickProject(list, { path: '/zzz' }, 'x1');
R.c = h.pickProject(list, null, 'gone'); R.d = h.pickProject([], null, '');
""")
        assert out == {"a": "x1", "b": "x1", "c": "self", "d": ""}


class TestSource:
    FILES = ["index.js", "helpers.js", "diff-widget.js", "terminal-widget.js", "code.css"]

    def test_no_mockup_sample_data_and_no_em_dash(self):
        for name in self.FILES:
            text = (_DIR / name).read_text(encoding="utf-8")
            for sample in ("5442", "handle_one_request", "460s", "1785"):
                assert sample not in text, (name, sample)
            assert "—" not in text and "–" not in text, name

    def test_widgets_put_text_in_through_textcontent_only(self):
        for name in ("diff-widget.js", "terminal-widget.js"):
            text = (_DIR / name).read_text(encoding="utf-8")
            assert "innerHTML" not in text and "insertAdjacentHTML" not in text and "setHtml" not in text

    def test_every_button_carries_a_spec_sentence(self):
        text = (_DIR / "index.js").read_text(encoding="utf-8")
        assert text.count("btn(") >= 4
        for line in text.splitlines():
            if "btn(" in line and "function btn" not in line:
                assert "'" in line
