"""OFFICE (finding #147): source rules. The floor route is tested in test_os_api_office.py and the
live model in test_os_screen_orchestration.py (the two screens share helpers)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCREENS = ROOT / "ui" / "assets" / "os" / "screens"
NODE = shutil.which("node")


def run_node(tmp_path: Path, module: Path, body: str) -> dict:
    if NODE is None:
        pytest.skip("node not on PATH in this environment")
    script = tmp_path / "s.mjs"
    script.write_text(
        f"import * as h from {module.as_uri()!r};\nconst R = {{}};\n{body}\nconsole.log(JSON.stringify(R));\n",
        encoding="utf-8",
    )
    proc = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def source(slug: str) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted((SCREENS / slug).iterdir()) if p.suffix in (".js", ".css"))




HELPERS = SCREENS / "office" / "helpers.js"


def test_no_fabricated_statuses_or_sample_agents():
    src = source("office")
    assert "rate_limited" not in src and "data-status=\"rate" not in src  # no source reports a rate limit, so no desk shows one
    assert "\u2014" not in src
    for sample in ("Executive &amp; Ops", "OFFICE_FLOORS"):
        assert sample not in src
    assert "/api/os/office/floor" in src and "/api/os/agents" in src


def test_the_screen_has_floor_and_team_and_reads_only():
    src = source("office")
    assert 'data-view="floor"' in src and 'data-view="team"' in src
    assert "ctx.api.post" not in src and "innerHTML" not in src


def test_visible_lines_drop_old_messages_and_keep_the_newest_per_pair(tmp_path):
    out = run_node(
        tmp_path, HELPERS,
        """
        const now = Date.parse('2026-11-15T06:10:00Z');
        const lines = [
          {from: 'a', to: 'b', subject: 'new', at: now / 1000 - 10},
          {from: 'a', to: 'b', subject: 'older', at: now / 1000 - 30},
          {from: 'b', to: 'c', subject: 'stale', at: now / 1000 - 600},
        ];
        R.out = h.visibleLines(lines, now).map(l => l.subject);
        """,
    )
    assert out["out"] == ["new"]


def test_ticker_says_what_really_happened_and_ignores_feed_polls(tmp_path):
    out = run_node(
        tmp_path, HELPERS,
        """
        const seen = {}; const name = (k) => ({mail: 'Mail and Calendar', news: 'News and World'})[k] || k;
        R.a = h.tickerFromActivity({agents: {mail: {status: 'computing', last: {tool: 'gmail_search'}}, news: {status: 'live', last: {tool: 'news_headlines'}}}}, seen, name, 1).map(t => t.text);
        R.b = h.tickerFromActivity({agents: {mail: {status: 'computing', last: {tool: 'gmail_search'}}}}, seen, name, 2).map(t => t.text);
        R.c = h.tickerFromActivity({agents: {mail: {status: 'idle', last: null}}}, seen, name, 3).map(t => t.text);
        const runs = {};
        R.d = h.tickerFromFanout({run_id: 'r1', total: 3}, runs, 4).map(t => t.text);
        R.e = h.tickerFromFanout({run_id: 'r1', total: 3, finished: true}, runs, 5).map(t => t.text);
        """,
    )
    assert out["a"] == ["Mail and Calendar is using gmail_search"]
    assert out["b"] == []
    assert out["c"] == ["Mail and Calendar finished"]
    assert out["d"] == ["A task was split across 3 agents"] and out["e"] == ["The split task finished"]


def test_team_rows_say_untested_until_a_test_exists_and_summary_line_is_plain(tmp_path):
    out = run_node(
        tmp_path, HELPERS,
        """
        const profiles = [{id: 'coder', name: 'Coder', job: 'writes code', tests: {passed: 0, total: 0}, availability: 'untested'},
                          {id: 'media', name: 'Media', job: 'plays', tests: {passed: 4, total: 5}, availability: 'available'}];
        R.rows = h.teamRows(profiles, [{id: 'coder', status: 'working'}]);
        R.sum0 = h.summaryLine({working: 0, needs_you: 0});
        R.sum1 = h.summaryLine({working: 2, needs_you: 1});
        R.ini = [h.initials('Mac Operator'), h.initials('Docs'), h.initials('')];
        """,
    )
    assert out["rows"][0]["tests"] == "Not tested yet" and out["rows"][0]["status"] == "working"
    assert out["rows"][1]["tests"] == "4 of 5 tests passed" and out["rows"][1]["status"] == "idle"
    assert out["sum0"] == "Everyone is idle" and out["sum1"] == "2 working, 1 need you"
    assert out["ini"] == ["MO", "DO", "?"]
