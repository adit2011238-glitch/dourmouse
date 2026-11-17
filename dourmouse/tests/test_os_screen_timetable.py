"""Automations screen (plan 2.2, finding 207; first written as TIMETABLE, finding #147): what the screen may claim (node)
and the source rules. The screen says only what the server sent: the next run is the schedule's next moment, the result
word is the last recorded run, and nothing is called a run that was not one."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCREENS = ROOT / "ui" / "assets" / "os" / "screens"
NODE = shutil.which("node")
H = SCREENS / "timetable" / "helpers.js"


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


def test_a_row_says_why_it_is_not_running(tmp_path):
    out = run_node(tmp_path, H, """
const now = new Date(2026, 10, 17, 12, 0).getTime();
const base = { trigger: 'Every weekday at 08:00', enabled: true, next_run: '2026-11-18 08:00', auto_disabled: false, watch_error: '' };
R.on = h.rowSub(base, now);
R.paused = h.rowSub({ ...base, enabled: false }, now);
R.failed = h.rowSub({ ...base, enabled: false, auto_disabled: true }, now);
R.watch = h.rowSub({ ...base, watch_error: 'cannot read the folder (PermissionError)' }, now);
R.event = h.rowSub({ ...base, next_run: '' }, now);
""")
    assert out["on"] == "Every weekday at 08:00 · Next Tomorrow 08:00"
    assert out["paused"].endswith("Paused") and out["failed"].endswith("Switched off after 3 failures")
    assert out["watch"].endswith("cannot read the folder (PermissionError)")
    assert out["event"] == "Every weekday at 08:00"


def test_a_step_that_waits_or_stopped_at_its_limit_is_a_warning_not_a_failure(tmp_path):
    out = run_node(tmp_path, H, "R.t = ['ok', 'failed', 'parked', 'capped', 'running', ''].map(h.resultTone);")
    assert out["t"] == ["ok", "bad", "warn", "warn", "", ""]


def test_the_approval_card_lists_when_what_use_delivery_and_limits(tmp_path):
    out = run_node(tmp_path, H, """
R.rows = h.cardRows({ trigger: 'Daily at 08:00', action: 'Daily digest', tools: 'Only daily digest.', delivery: 'A notice when it finishes', caps_text: 'At most 6 runs a day, 8 steps each' }).map((r) => r[0]);
R.none = h.cardRows(null);
""")
    assert out["rows"] == ["When", "Does", "Can use", "Tells you", "Limits"] and out["none"] == []


def test_history_lines_and_parked_runs(tmp_path):
    out = run_node(tmp_path, H, """
R.note = h.historyNote({ tools: ['gated_tool'], duration_ms: 1500, manual: true });
R.none = h.historyNote({ tools: [], duration_ms: 0, manual: false });
R.parked = h.parkedRuns([{ id: 'a', status: 'parked' }, { id: 'b', status: 'parked', job_id: 'g1' }, { id: 'c', status: 'ok' }]).map((r) => r.id);
R.dur = [h.durationText(250), h.durationText(2400), h.durationText(0), h.durationText('x')];
R.count = [h.countsText([]), h.countsText([{ enabled: true }]), h.countsText([{ enabled: true }, { enabled: false }])];
""")
    assert out["note"] == "gated tool · 1.5 s · Run by you" and out["none"] == ""
    assert out["parked"] == ["a"], "a prompt job's own approvals stay on the Jobs board"
    assert out["dur"] == ["250 ms", "2.4 s", "", ""]
    assert out["count"] == ["", "1 automation · 1 on", "2 automations · 1 on"]


def test_the_screen_never_runs_anything_from_a_bare_click():
    src = source("timetable")
    assert "approve: true" in src, "creating needs the explicit approve flag"
    assert "data-approve" in src and "data-preview" in src, "a card is shown before anything is created"
    for path in ("/api/os/automations/remove", "/api/os/automations/run", "/api/os/automations/approve"):
        assert path in src
    # run, remove and a parked step each go through the confirmation card
    assert src.count("confirmHere(") >= 3
    assert "—" not in src


def test_the_screen_passes_the_shells_style_lint(tmp_path):
    lint = (ROOT / "scripts" / "ui_style_lint.mjs").as_uri()
    files = sorted((SCREENS / "timetable").iterdir())
    css = json.dumps([str(f) for f in files if f.suffix == ".css"])
    js = json.dumps([str(f) for f in files if f.suffix == ".js"])
    if NODE is None:
        pytest.skip("node not on PATH in this environment")
    script = tmp_path / "lint.mjs"
    script.write_text(f"""import {{ readFileSync }} from 'node:fs';
const m = await import({lint!r});
const found = [];
for (const f of {css}) found.push(...m.lintCss(readFileSync(f, 'utf8'), f));
for (const f of {js}) found.push(...m.lintText(readFileSync(f, 'utf8'), f));
console.log(JSON.stringify(found.map((x) => x.at + ' ' + x.rule)));""", encoding="utf-8")
    proc = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == []
