"""Home screen (finding 200): the pure helpers under node, and the rules the screen must keep."""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

_HOME = pathlib.Path(__file__).resolve().parents[2] / "ui" / "assets" / "os" / "screens" / "home"


def _node(script: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    out = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=30, cwd=_HOME)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_text_lines_are_built_from_the_payload_and_leave_out_zero_parts():
    got = _node("""
import * as h from './helpers.js';
const t = (working, needs) => ({ greeting: { date: 'Saturday 14 November' }, office: { working }, needs_total: needs });
console.log(JSON.stringify({
  named: h.greetingLine({ part: 'morning', name: 'Adit' }), unnamed: h.greetingLine({ part: 'evening', name: '' }), none: h.greetingLine(null),
  both: h.subLine(t(4, 1)), one: h.subLine(t(1, 2)), quiet: h.subLine(t(0, 0)), nodate: h.subLine({ office: { working: 0 }, needs_total: 0 }),
}));""")
    assert got["named"] == "Good morning, Adit" and got["unnamed"] == "Good evening" and got["none"] == "Good day"
    assert got["both"] == "Saturday 14 November. 4 agents are working, 1 thing needs you."
    assert got["one"] == "Saturday 14 November. 1 agent is working, 2 things need you."
    assert got["quiet"] == "Saturday 14 November. Nothing needs you." and got["nodate"] == "Nothing needs you."


def test_tabs_covers_and_titles():
    got = _node("""
import * as h from './helpers.js';
const a = h.quickTab(() => 0.5), b = h.quickTab(() => 0.6);
console.log(JSON.stringify({
  quick: a.startsWith('quick:'), valid: /^[A-Za-z0-9_.:-]{1,120}$/.test(a), pj: h.projectTab('p1', 't2'),
  cover: h.coverVar({ cover: '3' }), spread: h.coverVar({ id: 'abc' }) === h.coverVar({ id: 'abc' }), range: /^var\\(--tk-cover-[1-6]\\)$/.test(h.coverVar({ id: 'zzz' })),
  long: h.threadTitle('x'.repeat(200)).length, blank: h.threadTitle('  '), squash: h.threadTitle('a   b\\n c'),
}));""")
    assert got["quick"] and got["valid"] and got["pj"] == "pj:p1:t2"
    assert got["cover"] == "var(--tk-cover-3)" and got["spread"] and got["range"]
    assert got["long"] == 60 and got["blank"] == "New thread" and got["squash"] == "a b c"


def test_the_screen_keeps_its_rules():
    js = (_HOME / "index.js").read_text(encoding="utf-8") + (_HOME / "helpers.js").read_text(encoding="utf-8")
    css = (_HOME / "home.css").read_text(encoding="utf-8")
    for text in (js, css):
        assert "—" not in text
        assert "gradient(" not in text and "text-transform" not in text and "letter-spacing" not in text
    # token colours only, no literals
    import re

    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", css)
    # the quick ask never touches a project route; saving is a separate, explicit click
    assert "/api/os/today" in js and "quickTab()" in js and "thread/new" in js and "thread/turn" in js
    assert "tabId: tab" in js
