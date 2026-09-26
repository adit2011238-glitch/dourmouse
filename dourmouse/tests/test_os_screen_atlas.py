"""Finding #146: the ATLAS screen's pure helpers and source rules."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_DIR = _ROOT / "ui" / "assets" / "os" / "screens" / "atlas"
_NODE = shutil.which("node")


def node(tmp_path, body):
    if _NODE is None:
        pytest.skip("node not on PATH in this environment")
    script = tmp_path / "h.mjs"
    script.write_text(f"import * as h from {(_DIR / 'helpers.js').as_uri()!r};\nconst R = {{}};\n{body}\nconsole.log(JSON.stringify(R));\n", encoding="utf-8")
    proc = subprocess.run([_NODE, str(script)], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _src():
    return (_DIR / "index.js").read_text(encoding="utf-8") + (_DIR / "helpers.js").read_text(encoding="utf-8")

class TestHelpers:
    def test_rows_come_from_the_sources_the_server_reported(self, tmp_path):
        out = node(
            tmp_path,
            "const s={sources:{quakes:{ok:true,count:8,latency_ms:120},ships:{ok:false,error:'AISSTREAM_API_KEY not set',count:0},air_quality:{ok:true,count:3}}};"
            "R.rows=h.channelRows(s).map(r=>[r.name,r.label,r.ok,r.count,r.error]); R.t=h.totals(h.channelRows(s));",
        )
        assert out["rows"] == [["air_quality", "air quality", True, 3, ""], ["quakes", "quakes", True, 8, ""], ["ships", "ships", False, 0, "AISSTREAM_API_KEY not set"]]
        assert out["t"] == {"feeds": 3, "answered": 2, "failed": 1, "events": 11, "reporting": 2}

    def test_an_empty_or_odd_snapshot_gives_no_rows(self, tmp_path):
        out = node(tmp_path, "R.a=h.channelRows(null).length; R.b=h.channelRows({sources:5}).length; R.c=h.channelRows({sources:{x:null}})[0].error;")
        assert out == {"a": 0, "b": 0, "c": "no answer"}

    def test_pulse_tones_follow_the_servers_four_labels(self, tmp_path):
        out = node(tmp_path, "R.t=['STABLE','ELEVATED','HEIGHTENED','CRITICAL','x',null].map(h.pulseTone);")
        assert out["t"] == ["ok", "active", "warn", "bad", "dim", "dim"]

    def test_the_pulse_note_says_what_really_moves_the_score(self, tmp_path):
        note = node(tmp_path, "R.n=h.PULSE_NOTE;")["n"]
        assert "disaster" in note and "cyber" in note and "market" in note


class TestSourceRules:
    def test_no_mockup_numbers_or_the_wrong_feed_count_remain(self):
        src = _src()
        for sample in ("HEIGHTENED</div>", "44 events", "8 keyless", "Re-pulls all 8", "no API key"):
            assert sample not in src, sample

    def test_it_never_reaches_the_quant_lab(self):
        src = _src()
        assert "/api/atlas" not in src and "atlas_lab" not in src and "atlas-lab" not in src

    def test_it_polls_only_through_ctx_and_repaints_from_a_fetch(self):
        src = (_DIR / "index.js").read_text(encoding="utf-8")
        assert "ctx.every(POLL_MS" in src and "setInterval" not in src and "/api/os/world/refresh" in src

    def test_source_errors_reach_the_dom_only_as_text(self):
        src = (_DIR / "index.js").read_text(encoding="utf-8")
        assert "e.textContent = r.error" in src and "innerHTML" not in src
