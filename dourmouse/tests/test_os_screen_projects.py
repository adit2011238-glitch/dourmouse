"""Finding #150: the PROJECTS screen's pure helpers (node) and its source rules.

The DOM code is verified live in a real browser (EVIDENCE/149_os_projects.png);
what can be checked without one is checked here. The three routes are tested
in test_os_api_projects.py.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_DIR = _ROOT / "ui" / "assets" / "os" / "screens" / "projects"
_NODE = shutil.which("node")


def node(tmp_path, body):
    if _NODE is None:
        pytest.skip("node not on PATH in this environment")
    script = tmp_path / "h.mjs"
    script.write_text(f"import * as h from {(_DIR / 'helpers.js').as_uri()!r};\nconst R = {{}};\n{body}\nconsole.log(JSON.stringify(R));\n", encoding="utf-8")
    proc = subprocess.run([_NODE, str(script)], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestHelpers:
    def test_the_active_and_archived_split_uses_fourteen_days(self, tmp_path):
        out = node(tmp_path, """
const now = Date.parse('2026-09-26T12:00:00Z');
const day = 86400000;
const iso = (d) => new Date(now - d * day).toISOString();
const list = [
  { path: '/a', name: 'a', last_active: iso(1) },
  { path: '/b', name: 'b', last_active: iso(13.9) },
  { path: '/c', name: 'c', last_active: iso(14.1) },
  { path: '/d', name: 'd', last_active: null },
  { path: '', name: 'nopath', last_active: iso(0) },
  null,
];
const s = h.splitProjects(list, now);
R.active = s.active.map((p) => p.name);
R.archived = s.archived.map((p) => p.name);
R.total = s.total;
R.junk = h.splitProjects('nope', now).total;
""")
        assert out["active"] == ["a", "b"] and out["archived"] == ["c", "d"] and out["total"] == 4 and out["junk"] == 0

    def test_counts_are_the_servers_numbers_never_invented(self, tmp_path):
        out = node(tmp_path, """
R.line = h.countsLine({ session_counts: { claude_code: 3, codex_cli: 1, manual: 0 } });
R.empty = h.countsLine({});
R.bad = h.countsLine(null);
R.n = h.sessionCount({ session_count: 'x' });
R.word1 = h.sessionsWord({ session_count: 1 });
R.word0 = h.sessionsWord({ session_count: 0 });
R.origin = h.originLine({ sources: ['claude_code', 'manual'] });
""")
        assert out["line"] == "3 Claude Code, 1 Codex" and out["empty"] == "" and out["bad"] == ""
        assert out["n"] == 0 and out["word1"] == "session" and out["word0"] == "sessions"
        assert out["origin"] == "Claude Code and created here"

    def test_the_scope_record_carries_the_tab_id_from_the_open_call(self, tmp_path):
        out = node(tmp_path, """
const p = { path: '/x/y', name: 'Y', tab_id: 'project-1' };
R.a = h.scopeFrom(p, { tab_id: 'project-2', name: 'Y2' });
R.b = h.scopeFrom(p, null);
R.mine = h.isActiveScope(p, { tab_id: 'project-1' });
R.other = h.isActiveScope(p, { tab_id: 'project-9' });
R.none = h.isActiveScope(p, null);
R.notab = h.isActiveScope({ path: '/z' }, { tab_id: 'project-1' });
""")
        assert out["a"] == {"tab_id": "project-2", "name": "Y2", "path": "/x/y"}
        assert out["b"]["tab_id"] == "project-1"
        assert out["mine"] is True and out["other"] is False and out["none"] is False and out["notab"] is False

    def test_the_confirm_texts_say_what_will_and_will_not_happen(self, tmp_path):
        out = node(tmp_path, """
R.stop = h.stopPrompt({ name: 'Lab' });
R.make = h.createPrompt({ name: 'Lab', path: '/home/u/Documents/Dourmouse Projects/Lab' }, 'notes');
R.make2 = h.createPrompt({ name: 'Lab', path: '/p' }, '');
""")
        assert "nothing is deleted" in out["stop"] and "stay exactly where they are" in out["stop"]
        assert "/home/u/Documents/Dourmouse Projects/Lab" in out["make"] and "Nothing existing is changed" in out["make"]
        assert "description" in out["make"] and "description" not in out["make2"]

    def test_name_validation(self, tmp_path):
        out = node(tmp_path, """
R.ok = h.validateName('  Alpha ');
R.empty = h.validateName('   ');
R.long = h.validateName('x'.repeat(121));
R.ctl = h.validateName('a\\nb');
""")
        assert out["ok"] == {"ok": True, "name": "Alpha"}
        assert out["empty"]["ok"] is False and out["long"]["ok"] is False and out["ctl"]["ok"] is False

    def test_excerpt_is_bounded_and_collapses_whitespace(self, tmp_path):
        out = node(tmp_path, """
R.short = h.excerpt('a   b\\n c');
R.long = h.excerpt('x'.repeat(500), 50);
const t0 = Date.now();
h.excerpt('[ '.repeat(100000));
R.ms = Date.now() - t0;
""")
        assert out["short"] == "a b c" and len(out["long"]) == 50 and out["ms"] < 500

    def test_source_status_names_where_it_looked(self, tmp_path):
        out = node(tmp_path, """
R.s = h.sourceStatusLines({ claude_code: { configured: true, root: '/r' }, codex_cli: { configured: false } });
R.none = h.sourceStatusLines(null);
""")
        assert out["s"][0] == {"label": "Claude Code history", "found": True, "where": "/r"}
        assert out["s"][1]["found"] is False and out["none"][0]["found"] is False


class TestSource:
    def test_no_mockup_sample_data_is_left_in_the_screen(self):
        text = "\n".join(p.read_text(encoding="utf-8") for p in _DIR.glob("*.js"))
        for sample in ("atlas-strategy-lab", "entropy generation", "5442", "79 findings", "sessions this week"):
            assert sample not in text

    def test_every_control_has_a_spec_sentence(self):
        text = (_DIR / "index.js").read_text(encoding="utf-8")
        buttons = re.findall(r"<button\b[^>]*>", text)
        assert buttons and all("data-spec" in b for b in buttons)
        assert 'data-spec="Opens HOME' in text

    def test_changes_go_through_the_confirm_card_and_open_uses_the_bookkeeper(self):
        text = (_DIR / "index.js").read_text(encoding="utf-8")
        assert "confirmHere(" in text
        assert "/api/projects/open" in text and "/api/os/projects/create" in text
        assert not re.search(r"post\('/api/projects/delete'", text.split("function stopTracking")[0])
