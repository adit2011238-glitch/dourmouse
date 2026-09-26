"""TIMETABLE (finding #147): what the screen may claim (node) and the source rules."""

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



H = SCREENS / "timetable" / "helpers.js"


def test_overdue_is_derived_from_the_next_run_only(tmp_path):
    out = run_node(tmp_path, H, """
const now = new Date(2026, 8, 26, 12, 0).getTime();
R.past = h.isOverdue({ enabled: true, next_run: '2026-09-26 11:59' }, now);
R.future = h.isOverdue({ enabled: true, next_run: '2026-09-26 12:01' }, now);
R.paused = h.isOverdue({ enabled: false, next_run: '2026-09-26 11:59' }, now);
R.junk = h.isOverdue({ enabled: true, next_run: 'unknown' }, now);
""")
    assert out == {"past": True, "future": False, "paused": False, "junk": False}


def test_unattended_check_uses_the_real_tool_tier(tmp_path):
    out = run_node(tmp_path, H, """
const tiers = h.toolTiers({ subagents: [{ tools: [{ name: 'a', permission: 'regular' }, { name: 'b', permission: 'requires_confirmation' }] }] });
R.a = h.cannotRunUnattended({ tool: 'a' }, tiers); R.b = h.cannotRunUnattended({ tool: 'b' }, tiers); R.unknown = h.cannotRunUnattended({ tool: 'zzz' }, tiers);
""")
    assert out == {"a": False, "b": True, "unknown": False}


def test_args_preview_is_bounded_and_honest_when_empty(tmp_path):
    out = run_node(tmp_path, H, "R.e = h.argsPreview({}); R.n = h.argsPreview({ q: 'x'.repeat(5000) }).length; R.t = h.argsPreview({ a: 1 });")
    assert out == {"e": "no arguments", "n": 140, "t": '{"a":1}'}


def test_enabled_routines_sort_first(tmp_path):
    out = run_node(tmp_path, H, "R.ids = h.sortEntries([{ id: 'b', enabled: false }, { id: 'c', enabled: true }, { id: 'a', enabled: true }]).map(e => e.id);")
    assert out["ids"] == ["a", "c", "b"]


def test_source_makes_no_claims_the_code_contradicts():
    src = source("timetable")
    for sample in ("History sync from the desktop", "Market close scan", "never silently backfilled", "machine asleep"):
        assert sample not in src
    assert "catch-up" in src
    assert "\u2014" not in src
