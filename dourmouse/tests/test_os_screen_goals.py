"""GOALS (finding #147): what the screen may claim (node) and the source rules. Routes are in test_os_api_goals.py."""

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



H = SCREENS / "goals" / "helpers.js"


def test_progress_is_counted_never_guessed(tmp_path):
    out = run_node(tmp_path, H, """
R.a = h.progress({ tasks_total: 4, tasks_done: 1, tasks_verified: 1 });
R.none = h.progress({ tasks_total: 0, tasks_done: 0 });
R.all = h.progress({ tasks_total: 3, tasks_done: 3, tasks_verified: 2 });
""")
    assert out["a"] == {"done": 1, "total": 4, "pct": 25, "verified": 1}
    assert out["none"] is None
    assert out["all"]["pct"] == 100 and out["all"]["verified"] == 2


def test_paused_goal_says_what_it_was(tmp_path):
    out = run_node(tmp_path, H, """
R.w = h.goalWord({ status: 'PAUSED', paused_from: 'EXECUTING' });
R.plain = h.goalWord({ status: 'WAITING_FOR_APPROVAL' });
R.tone = ['FAILED', 'COMPLETED', 'PAUSED', 'EXECUTING'].map(h.goalTone);
""")
    assert out["w"] == "Paused (was executing)" and out["plain"] == "Needs your approval"
    assert out["tone"] == ["bad", "ok", "warn", ""]


def test_board_split_and_task_tags(tmp_path):
    out = run_node(tmp_path, H, """
const s = h.splitBoard([{ status: 'EXECUTING' }, { status: 'PAUSED' }, { status: 'COMPLETED' }, { status: 'CANCELLED' }]);
R.live = s.live.length; R.fin = s.finished.length;
R.t = ['COMPLETED', 'WAITING_FOR_APPROVAL', 'PENDING', 'FAILED', 'ODD'].map(x => h.taskTag(x).word);
""")
    assert out["live"] == 2 and out["fin"] == 2
    assert out["t"] == ["done", "needs approval", "waiting", "failed", "odd"]


def test_audit_lines_state_pause_and_held_writes(tmp_path):
    out = run_node(tmp_path, H, """
R.a = h.describeAudit({ type: 'goal_status_changed', detail: { status: 'PAUSED', paused_from: 'EXECUTING' } });
R.b = h.describeAudit({ type: 'goal_status_changed', detail: { status: 'EXECUTING', held_while_paused: true } });
R.c = h.describeAudit({ type: 'verification', detail: { verified: false, error: null } });
R.d = h.describeAudit({ type: 'mystery' });
""")
    assert "paused from EXECUTING" in out["a"] and "applies on resume" in out["b"]
    assert out["c"] == "Task verification: not verified" and out["d"] == "mystery"


def test_form_validation_mirrors_the_server(tmp_path):
    out = run_node(tmp_path, H, """
R.empty = h.validateForm({ objective: '  ' });
R.long = h.validateForm({ objective: 'x'.repeat(601) });
R.steps = h.validateForm({ objective: 'ok', steps: Array(13).fill('a').join('\\n') });
R.ok = h.validateForm({ objective: 'ok', steps: 'a\\n\\nb' });
R.body = h.createBody({ objective: ' ok ', steps: 'a\\n\\nb', criteria: 'c', priority: 'high' });
""")
    assert out["empty"] and "600" in out["long"] and "12" in out["steps"] and out["ok"] == ""
    assert out["body"] == {"objective": "ok", "steps": ["a", "b"], "success_criteria": ["c"], "priority": "high"}


def test_the_prompts_say_what_will_happen(tmp_path):
    out = run_node(tmp_path, H, """
R.cancel = h.cancelPrompt({ objective: 'Tidy', tasks_total: 3, tasks_done: 1 });
R.approve = h.approvePrompt({ description: 'send', pending_prompts: ['send mail to X?'] }, true);
R.decline = h.approvePrompt({ description: 'send', pending_prompts: [] }, false);
""")
    assert "1 of 3 tasks are already done and stay done" in out["cancel"]
    assert "send mail to X?" in out["approve"] and "marked failed" in out["decline"]


def test_source_has_no_mockup_samples_and_no_em_dash():
    src = source("goals")
    for sample in ("write finding #078", "engineering audit and the roadmap", "width:62%"):
        assert sample not in src
    assert "\u2014" not in src
