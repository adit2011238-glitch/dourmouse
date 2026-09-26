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




def test_no_fabricated_statuses_or_sample_agents():
    src = source("office")
    assert "DESK_WORD" in src and "'live'" not in src and "class=\"desk live" not in src
    for sample in ("Executive &amp; Ops", "OFFICE_FLOORS"):
        assert sample not in src
    assert "\u2014" not in src


def test_the_grouping_rule_and_unassigned_floor_are_stated_on_screen():
    src = source("office")
    assert "st.rule" in src and "st.unassigned" in src and "st.unknown" in src


def test_concurrent_count_is_kept_on_the_desk():
    assert "x ${String(cur.concurrent)}" in source("office")
