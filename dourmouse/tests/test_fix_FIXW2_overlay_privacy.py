"""FIX-W2 H-FD2-2: the overlay never shows the mic and camera as armed on a guess. When the privacy state
cannot be read at all, both are reported as unknown and drawn as such."""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from dourmouse import overlay, tray


def test_an_unreadable_state_is_unknown_not_armed(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("tray module broken")

    monkeypatch.setattr(tray, "load_state", boom)
    assert overlay._read_privacy_state() == {"mic_enabled": None, "camera_enabled": None}


def test_a_real_state_is_passed_through(monkeypatch):
    monkeypatch.setattr(tray, "load_state", lambda: tray.KillSwitchState(mic_enabled=False, camera_enabled=True))
    assert overlay._read_privacy_state() == {"mic_enabled": False, "camera_enabled": True}


def test_a_corrupt_state_file_is_the_trays_fail_closed_killed_state(tmp_path, monkeypatch):
    p = tmp_path / "privacy.json"
    p.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(tray, "_state_path", lambda: p)
    assert overlay._read_privacy_state() == {"mic_enabled": False, "camera_enabled": False}


def test_the_poller_forwards_unknown_unchanged():
    poller = overlay.OverlayStatusPoller(
        "http://x", lambda s: None, fetch=lambda url: {"threads": []},
        privacy_reader=lambda: {"mic_enabled": None, "camera_enabled": None},
    )
    out = poller.poll_once()
    assert out["mic_enabled"] is None and out["camera_enabled"] is None


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_page_draws_true_false_and_unknown_three_ways(tmp_path):
    html = overlay._OVERLAY_HTML
    script_src = html[html.index("<script>") + len("<script>"):html.index("</script>")]
    harness = f"""
const els = {{}};
global.window = {{}};
global.document = {{ getElementById: (id) => (els[id] = els[id] || {{ textContent: '', className: '' }}) }};
{script_src}
const out = {{}};
for (const [name, s] of Object.entries({{
  on: {{ online: true, mic_enabled: true, camera_enabled: true }},
  off: {{ online: true, mic_enabled: false, camera_enabled: false }},
  unknown: {{ online: true, mic_enabled: null, camera_enabled: null }},
  offline: {{ online: false, mic_enabled: true, camera_enabled: true }},
}})) {{ window.__dourmouseOverlayUpdate(s); out[name] = [els.micDot.className, els.camDot.className]; }}
console.log(JSON.stringify(out));
"""
    script = tmp_path / "h.js"
    script.write_text(harness, encoding="utf-8")
    proc = subprocess.run([shutil.which("node"), str(script)], capture_output=True, text=True, timeout=20, check=False)
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    assert got["on"] == ["dot on", "dot on"] and got["off"] == ["dot off", "dot off"]
    assert got["unknown"] == ["dot unknown", "dot unknown"] and got["offline"] == ["dot offline", "dot offline"]
    assert ".dot.unknown" in html
