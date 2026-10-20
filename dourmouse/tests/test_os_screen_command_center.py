"""COMMAND CENTER (finding #186, owner request 2026-10-21: "see the full
chain of thought of the AI sentries live, live updates, command centre,
everything"). helpers.js runs real node here; the backend half (dispatch.py
_broadcast_live_trace/_emit_event, webui.py _LIVE_EVENTS_HUB) is tested in
dourmouse/tests/test_dispatch.py's TestCommandCenterLiveTrace."""

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


HELPERS = SCREENS / "command_center" / "helpers.js"


def test_a_new_call_id_opens_its_own_lane_in_arrival_order(tmp_path):
    out = run_node(
        tmp_path, HELPERS,
        """
        const lanes = {}; const order = [];
        h.applyTrace(lanes, order, {type:'plan', agent:'research_info', call_id:'c1', steps:[{subagent:'research_info', task:'look it up'}]});
        h.applyTrace(lanes, order, {type:'tool_use', agent:'mail', call_id:'c2', name:'gmail_search', raw_arguments:'{}'});
        R.order = order; R.c1Agent = lanes.c1.agent; R.c2Agent = lanes.c2.agent;
        """,
    )
    assert out["order"] == ["c2", "c1"]  # most recently touched first
    assert out["c1Agent"] == "research_info"
    assert out["c2Agent"] == "mail"


def test_full_text_is_kept_not_truncated(tmp_path):
    long_text = "x" * 5000
    out = run_node(
        tmp_path, HELPERS,
        f"""
        const lanes = {{}}; const order = [];
        h.applyTrace(lanes, order, {{type:'thinking_delta', agent:'research_info', call_id:'c1', text:{long_text!r}}});
        R.len = lanes.c1.lines[0].text.length;
        R.kind = lanes.c1.lines[0].kind;
        """,
    )
    assert out["len"] == 5000  # the whole thing, not a 160-char preview like the SECURITY activity line
    assert out["kind"] == "thinking"


def test_assistant_text_marks_the_lane_done(tmp_path):
    out = run_node(
        tmp_path, HELPERS,
        """
        const lanes = {}; const order = [];
        h.applyTrace(lanes, order, {type:'thinking_delta', agent:'a', call_id:'c1', text:'reasoning'});
        h.applyTrace(lanes, order, {type:'assistant_text', agent:'a', call_id:'c1', text:'done.'});
        R.done = lanes.c1.done;
        R.n = lanes.c1.lines.length;
        """,
    )
    assert out["done"] is True
    assert out["n"] == 2


def test_tool_use_and_tool_result_are_both_kept_in_order(tmp_path):
    out = run_node(
        tmp_path, HELPERS,
        """
        const lanes = {}; const order = [];
        h.applyTrace(lanes, order, {type:'tool_use', agent:'a', call_id:'c1', name:'web_search', raw_arguments:'{"q":"x"}'});
        h.applyTrace(lanes, order, {type:'tool_result', agent:'a', call_id:'c1', name:'web_search', text:'3 results'});
        R.kinds = lanes.c1.lines.map(l => l.kind);
        R.texts = lanes.c1.lines.map(l => l.text);
        """,
    )
    assert out["kinds"] == ["tool", "result"]
    assert "web_search" in out["texts"][0]
    assert "3 results" in out["texts"][1]


def test_an_entry_with_no_call_id_gets_a_per_agent_lane_instead_of_being_dropped(tmp_path):
    """Several real dispatch.py call sites emit with no ctx at all (no call_id) --
    this must still show up somewhere, not silently vanish from the live feed."""
    out = run_node(
        tmp_path, HELPERS,
        """
        const lanes = {}; const order = [];
        h.applyTrace(lanes, order, {type:'assistant_delta', text:'hi', agent:'orchestrator'});
        R.keys = Object.keys(lanes);
        R.text = lanes['agent:orchestrator'].lines[0].text;
        """,
    )
    assert out["keys"] == ["agent:orchestrator"]
    assert out["text"] == "hi"


def test_lane_cap_evicts_the_oldest_not_the_one_just_touched(tmp_path):
    body = ["const lanes = {}; const order = [];"]
    for i in range(12):
        body.append(f"h.applyTrace(lanes, order, {{type:'thinking_delta', agent:'a{i}', call_id:'c{i}', text:'t'}});")
    body.append("R.orderLen = order.length; R.laneCount = Object.keys(lanes).length; R.has_c11 = 'c11' in lanes; R.has_c0 = 'c0' in lanes;")
    out = run_node(tmp_path, HELPERS, "\n".join(body))
    assert out["orderLen"] == out["laneCount"] == 8  # LANE_CAP
    assert out["has_c11"] is True  # most recent survives
    assert out["has_c0"] is False  # oldest was evicted


def test_screen_subscribes_to_agent_trace_and_security_star_live_and_never_truncates_the_analyst():
    src = source("command_center")
    assert "ctx.events.on('agent_trace'" in src
    assert "ctx.events.on('security_*'" in src
    assert "a.summary || ''" in src  # the FULL summary, not a 160-char slice like security/helpers.js's activityLine
    assert ".slice(0, 160)" not in src
    assert "innerHTML" not in src and "insertAdjacentHTML" not in src and " eval(" not in src


def test_screen_is_read_only_no_post_no_mutating_tool():
    src = source("command_center")
    assert "ctx.api.post" not in src
    assert "fetch(" not in src
