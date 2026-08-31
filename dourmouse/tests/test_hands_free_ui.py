"""ui/workspace.html's pollHandsFreeStatus() -- the listening-indicator
half of hands-free step 3 (server auto-start wiring covered separately by
test_webui_hands_free_wiring.py, the orchestration logic by
test_hands_free.py). Covered by real Node execution against the actual
shipped function (same discipline as test_workspace_panel_control.py),
with a fake fetch + fake pill element standing in for the DOM/network --
no real browser, no real server.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_WORKSPACE_HTML = _PROJECT_ROOT / "ui" / "workspace.html"


def _classic_script() -> str:
    html = _WORKSPACE_HTML.read_text(encoding="utf-8")
    m = re.search(r"<script>(.*?)</script>", html, re.S)
    assert m
    return m.group(1)


def _fn_source() -> str:
    script = _classic_script()
    m = re.search(
        r"async function pollHandsFreeStatus\(\)\s*\{.*?\n\}\n",
        script, re.S,
    )
    assert m, "pollHandsFreeStatus not found"
    return m.group(0)


def _run(tmp_path, status_json, extra_setup=""):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH in this environment")
    harness = f"""
const _pill = {{ textContent: "", className: "", title: "" }};
function $(id) {{ return id === "handsFreePill" ? _pill : null; }}
{extra_setup}
{_fn_source()}
pollHandsFreeStatus().then(() => {{
  console.log(JSON.stringify({{
    textContent: _pill.textContent, className: _pill.className, title: _pill.title,
  }}));
}});
"""
    js_file = tmp_path / "poll_hf.js"
    js_file.write_text(harness, encoding="utf-8")
    result = subprocess.run([node, str(js_file)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


class TestPollHandsFreeStatus:
    def test_enabled_and_running_shows_live(self, tmp_path):
        setup = (
            'function fetch(_url) { return Promise.resolve({ '
            'json: () => Promise.resolve({enabled: true, running: true, reason: "listening"}) }); }'
        )
        out = _run(tmp_path, None, setup)
        assert out["textContent"] == "● HANDS-FREE: LISTENING"
        assert out["className"] == "hfpill live"

    def test_enabled_but_not_running_shows_error(self, tmp_path):
        setup = (
            'function fetch(_url) { return Promise.resolve({ '
            'json: () => Promise.resolve({enabled: true, running: false, reason: "no mic device found"}) }); }'
        )
        out = _run(tmp_path, None, setup)
        assert out["textContent"] == "● HANDS-FREE: ERROR"
        assert out["className"] == "hfpill error"
        assert out["title"] == "no mic device found"

    def test_disabled_shows_off(self, tmp_path):
        setup = (
            'function fetch(_url) { return Promise.resolve({ '
            'json: () => Promise.resolve({enabled: false, running: false, '
            'reason: "DOURMOUSE_HANDS_FREE is off"}) }); }'
        )
        out = _run(tmp_path, None, setup)
        assert out["textContent"] == "● HANDS-FREE: OFF"
        assert out["className"] == "hfpill off"
        assert "off" in out["title"].lower()

    def test_fetch_failure_shows_unknown_not_a_crash(self, tmp_path):
        setup = 'function fetch(_url) { return Promise.reject(new Error("network down")); }'
        out = _run(tmp_path, None, setup)
        assert out["textContent"] == "● HANDS-FREE: ?"
        assert out["className"] == "hfpill error"
        assert "network down" in out["title"]

    def test_missing_pill_element_is_a_safe_noop(self, tmp_path):
        node = shutil.which("node")
        if not node:
            pytest.skip("node not on PATH in this environment")
        harness = f"""
function $(_id) {{ return null; }}
function fetch(_url) {{ throw new Error("should never be called"); }}
{_fn_source()}
pollHandsFreeStatus().then(() => console.log(JSON.stringify({{ok: true}})));
"""
        js_file = tmp_path / "poll_hf_noop.js"
        js_file.write_text(harness, encoding="utf-8")
        result = subprocess.run([node, str(js_file)], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout.strip().splitlines()[-1]) == {"ok": True}
