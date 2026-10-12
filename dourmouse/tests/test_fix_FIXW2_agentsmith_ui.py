"""FIX-W2 P4-42 (UI): the AGENTSMITH detail shows the module hash and labels preview_sha256 as the review
hash (module, test and schema), the one APPROVE sends."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parents[2] / "ui" / "assets" / "os" / "screens" / "agentsmith"
_NODE = shutil.which("node")
MOD, REV = "11" * 32, "22" * 32


def node(tmp_path, body):
    if _NODE is None:
        pytest.skip("node not on PATH in this environment")
    script = tmp_path / "s.mjs"
    script.write_text(f"import * as h from {(_DIR / 'helpers.js').as_uri()!r};\nconst R = {{}};\n{body}\nconsole.log(JSON.stringify(R));\n", encoding="utf-8")
    proc = subprocess.run([_NODE, str(script)], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_the_two_hashes_are_shown_and_labelled_apart(tmp_path):
    out = node(tmp_path, f"const d = {{ module_lines: 24, module_preview_sha256: '{MOD}', preview_sha256: '{REV}' }}; R.m = h.moduleHashLine(d); R.r = h.reviewHashLine(d);")
    assert MOD in out["m"] and REV not in out["m"] and "module sha256" in out["m"] and "24 lines" in out["m"]
    assert REV in out["r"] and MOD not in out["r"]
    assert "module, its test and the parameters schema" in out["r"] and "APPROVE sends this one" in out["r"]


def test_the_approval_prompt_names_the_review_hash(tmp_path):
    out = node(tmp_path, f"R.p = h.approvePrompt({{ tool_name: 'greet', module_lines: 24, preview_sha256: '{REV}' }});")
    assert "review hash" in out["p"] and REV[:12] in out["p"]


def test_the_detail_uses_the_helpers_and_the_approve_call_still_sends_the_review_hash():
    js = (_DIR / "index.js").read_text(encoding="utf-8")
    assert "moduleHashLine(x)" in js and "reviewHashLine(x)" in js
    assert "x.preview_sha256" not in js, "the screen no longer prints preview_sha256 as the module's hash"
    assert "sha256: d.preview_sha256" in js
