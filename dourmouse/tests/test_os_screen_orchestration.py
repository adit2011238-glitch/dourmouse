"""ORCHESTRATION (finding #147): what the shared live model may claim (node) and the source rules."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
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


H = SCREENS / "orchestration" / "helpers.js"


def test_a_fanout_event_builds_and_finishes_a_run(tmp_path):
    out = run_node(tmp_path, H, """
const runs = h.seedRuns({});
h.applyFanout(runs, { run_id: 'r1', total: 2, finished: false, branches: {
  0: { agent: 'security', model: 'm', phase: 'start', task: 'scan  the\\nmac' },
  1: { agent: 'news', model: 'm', phase: 'result', ok: true, elapsed_s: 3.2 } } });
R.rows = h.branchRows(runs.r1).map(r => [r.index, r.agent, r.state, r.elapsed, r.task]);
R.counts = h.runCounts(runs.r1);
R.meet = h.inMeeting(runs);
h.applyFanout(runs, { run_id: 'r1', total: 2, finished: true, branches: { 0: { agent: 'security', phase: 'result', ok: false, error: 'boom' }, 1: { agent: 'news', phase: 'result', ok: true } } });
R.after = h.branchRows(runs.r1).map(r => r.state);
R.liveAfter = h.liveRuns(runs).length;
R.meetAfter = h.inMeeting(runs);
""")
    assert out["rows"] == [[0, "security", "running", None, "scan the mac"], [1, "news", "done", 3.2, ""]]
    assert out["counts"] == {"total": 2, "done": 1, "failed": 0, "running": 1}
    assert out["meet"] == {"security": "r1"}
    assert out["after"] == ["failed", "done"] and out["liveAfter"] == 0 and out["meetAfter"] == {}


def test_running_branches_show_no_invented_elapsed_time(tmp_path):
    out = run_node(tmp_path, H, "R.rows = h.branchRows({ branches: { 0: { agent: 'a', phase: 'start', elapsed_s: null } } });")
    assert out["rows"][0]["elapsed"] is None


def test_the_run_map_is_bounded(tmp_path):
    out = run_node(tmp_path, H, """
const runs = {};
for (let i = 0; i < 40; i++) h.applyFanout(runs, { run_id: 'r' + i, total: 1, branches: {} }, i);
R.n = Object.keys(runs).length; R.cap = h.RUN_CAP; R.has = 'r39' in runs; R.old = 'r0' in runs;
""")
    assert out["n"] == out["cap"] and out["has"] and not out["old"]


def test_agent_status_words_are_only_the_tracker_ones_plus_meeting(tmp_path):
    out = run_node(tmp_path, H, """
const agents = h.seedAgents({ a: { status: 'computing', last: null, feed: [], concurrent_call_ids: { x: 1, y: 2 } }, b: { status: 'auth' }, c: { status: 'idle' } });
R.s = ['a', 'b', 'c', 'nope'].map(n => h.deskStatus(n, agents, {}));
R.meet = h.deskStatus('c', agents, { c: 'r1' });
R.conc = agents.a.concurrent;
h.applyAgentDelta(agents, { agents: { c: { status: 'computing', last: { tool: 't' }, concurrent: 3 } } });
R.after = [agents.c.status, agents.c.concurrent];
""")
    assert out["s"] == ["work", "auth", "idle", "idle"] and out["meet"] == "meet"
    assert out["conc"] == 2 and out["after"] == ["computing", 3]


def test_a_branch_transcript_is_the_meeting_lines_of_its_call_id(tmp_path):
    out = run_node(tmp_path, H, """
const m = { branches: [{ index: 0, call_id: 'c0' }, { index: 1, call_id: 'c1' }], lines: [{ call_id: 'c0', text: 'a' }, { call_id: 'c1', text: 'b' }, { call_id: 'c0', text: 'c' }] };
R.one = h.branchLines(m, h.callIdFor(m, 0)).map(l => l.text); R.all = h.branchLines(m, '').length; R.none = h.callIdFor(m, 9);
""")
    assert out == {"one": ["a", "c"], "all": 3, "none": ""}


def test_helper_regex_is_fast_on_a_large_input(tmp_path):
    big = "a" * 200000
    t = time.time()
    run_node(tmp_path, H, f"R.n = h.trimTask({big!r}).length;")
    assert time.time() - t < 5


def test_source_has_no_mockup_samples_and_makes_no_fake_claims():
    src = source("orchestration")
    for sample in ("research_info", "4 tool calls", "3.2s", "Up to 6 branches", ">queued<"):
        assert sample not in src
    assert "\u2014" not in src


ALLHANDS = SCREENS / "orchestration" / "allhands.js"


class TestAllHands:
    def test_a_run_first_seen_from_an_event_is_a_partial_skeleton_then_fills_in(self, tmp_path):
        out = run_node(tmp_path, ALLHANDS, """
const runs = {};
h.applyAllHands(runs, { run_id: 'r1', status: 'started' }, 100);
R.partial = runs.r1.partial === true && runs.r1.goal === '' && runs.r1.status === 'running';
h.applyAllHands(runs, { run_id: 'r1', brain: 'claude', status: 'running' });
h.applyAllHands(runs, { run_id: 'r1', brain: 'claude', status: 'done', summary: 'fast answer' });
h.applyAllHands(runs, { run_id: 'r1', brain: 'codex', status: 'error', error: 'limit hit' });
R.counts = h.counts(runs.r1);
R.claude = runs.r1.brains.claude;
h.applyAllHands(runs, { run_id: 'r1', status: 'done', synthesis: 'merged' }, 200);
R.final = { status: runs.r1.status, syn: runs.r1.synthesis, word: h.runWord(runs.r1), tone: h.runTone(runs.r1), fin: runs.r1.finished };
R.noid = h.applyAllHands(runs, { status: 'done' });
""")
        assert out["partial"] is True
        assert out["counts"] == {"total": 2, "done": 1, "failed": 1, "working": 0}
        assert out["claude"]["status"] == "done" and out["claude"]["result"] == "fast answer"
        assert out["final"] == {"status": "done", "syn": "merged", "word": "partly failed", "tone": "bad", "fin": 200}
        assert out["noid"] is None

    def test_words_never_call_a_failed_run_done_and_a_merge_failure_is_named(self, tmp_path):
        out = run_node(tmp_path, ALLHANDS, """
const mk = (statuses, extra = {}) => ({ id: 'x', status: 'done', brains: Object.fromEntries(statuses.map((s, i) => ['b' + i, { status: s }])), ...extra });
R.w = [h.runWord(mk(['done', 'done'])), h.runWord(mk(['error', 'error'])), h.runWord(mk(['done', 'error'])), h.runWord(mk(['done'], { error: 'merge died' })), h.runWord({ id: 'y', status: 'running', brains: {} })];
R.p = h.preview('a  b\\n c'.repeat(200), 50).length;
R.newest = h.newestRuns({ a: { id: 'a', started: 1 }, b: { id: 'b', started: 3 }, c: { id: 'c', started: 2 } }, 2).map((r) => r.id);
""")
        assert out["w"] == ["done", "all failed", "partly failed", "merge failed", "running"]
        assert out["p"] <= 50 and out["newest"] == ["b", "c"]

    def test_the_screen_reads_the_list_follows_events_and_escapes_every_model_text(self):
        src = (SCREENS / "orchestration" / "index.js").read_text(encoding="utf-8")
        assert "/api/allhands" in src and "ctx.events.on('allhands'" in src
        assert "innerHTML" not in src and ".innerHTML" not in src
        assert "openExternal('/all-hands?run='" in src

    def test_esc_closes_the_transcript_only_while_one_is_open(self):
        src = (SCREENS / "orchestration" / "index.js").read_text(encoding="utf-8")
        assert "offEsc" in src and "ctx.keys.pushEsc(" in src

    def test_a_crowded_board_says_how_many_runs_it_left_out(self):
        src = (SCREENS / "orchestration" / "index.js").read_text(encoding="utf-8")
        assert "st.evicted" in src and "not drawn" in src

    def test_the_owner_sees_no_internal_tool_names_in_the_empty_states(self):
        src = (SCREENS / "orchestration" / "index.js").read_text(encoding="utf-8")
        for word in ("delegate_parallel call", "fanned out"):
            assert word not in src

    def test_regex_free_helpers_are_fast_on_a_big_run(self, tmp_path):
        t0 = time.time()
        run_node(tmp_path, ALLHANDS, "const runs = {}; for (let i = 0; i < 20000; i++) h.applyAllHands(runs, { run_id: 'r' + (i % 50), brain: 'b' + (i % 7), status: 'done', summary: 'x'.repeat(200000) }); R.n = Object.keys(runs).length;")
        assert time.time() - t0 < 10
