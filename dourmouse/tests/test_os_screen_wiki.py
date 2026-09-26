"""Finding #146: the WIKI screen's pure helpers and source rules."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_DIR = _ROOT / "ui" / "assets" / "os" / "screens" / "wiki"
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

def _entries():
    return "[{path:'/w/a.md',status:'SUMMARIZED',last_seen:100},{path:'/w/b.md',status:'UNSUMMARIZED',last_seen:300},{path:'/w/c.md',status:'MISSING',last_seen:50},{path:'/w/d.md',status:'SUMMARIZED',last_seen:200},{path:'/w/e.md',status:'weird'}]"


class TestHelpers:
    def test_counts_are_computed_from_the_entries(self, tmp_path):
        out = node(tmp_path, f"R.c=h.counts({_entries()}); R.z=h.counts(null);")
        assert out["c"] == {"SUMMARIZED": 2, "UNSUMMARIZED": 1, "MISSING": 1, "other": 1, "total": 5}
        assert out["z"]["total"] == 0

    def test_filter_query_sort_and_cap(self, tmp_path):
        out = node(
            tmp_path,
            f"const e={_entries()}; R.all=h.visibleRows(e,'ALL','').rows.map(r=>r.path); R.m=h.visibleRows(e,'MISSING','').rows.length;"
            "R.q=h.visibleRows(e,'ALL','B.MD').rows.length; R.cap=h.visibleRows(e,'ALL','',2);",
        )
        assert out["all"][-1] == "/w/c.md" and out["m"] == 1 and out["q"] == 1
        assert out["cap"]["hidden"] == 3 and len(out["cap"]["rows"]) == 2 and out["cap"]["total"] == 5

    def test_last_scan_is_the_newest_last_seen_or_null(self, tmp_path):
        out = node(tmp_path, f"R.a=h.lastScan({_entries()}); R.b=h.lastScan([]);")
        assert out == {"a": 300, "b": None}

    def test_names_and_sizes(self, tmp_path):
        out = node(tmp_path, "R.b=h.baseName('/a/b/c.md'); R.d=h.dirName('/a/b/c.md'); R.s=[0,900,2048,5242880,-1,null].map(h.fmtSize);")
        assert out == {"b": "c.md", "d": "/a/b", "s": ["0 B", "900 B", "2.0 KB", "5.0 MB", "", ""]}

    def test_the_filter_is_fast_on_a_large_list(self, tmp_path):
        start = time.time()
        node(tmp_path, "const e=Array.from({length:20000},(_,i)=>({path:'/w/'+'x'.repeat(50)+i,status:'SUMMARIZED'})); R.n=h.visibleRows(e,'ALL','x9').total;")
        assert time.time() - start < 10


class TestSourceRules:
    def test_no_mockup_samples_remain(self):
        src = _src()
        for sample in ("REMAINING_WORK", "old_notes", ">128<", ">14<"):
            assert sample not in src, sample

    def test_scan_is_disabled_and_says_why_and_nothing_posts(self):
        src = (_DIR / "index.js").read_text(encoding="utf-8")
        assert "disabled: true" in src and "Not available from this window" in src
        assert "ctx.api.post" not in src

    def test_summaries_and_paths_reach_the_dom_only_as_text(self):
        src = (_DIR / "index.js").read_text(encoding="utf-8")
        assert "innerHTML" not in src and "el('div', 'wik-sum', e.summary" in src

    def test_no_roots_and_error_are_their_own_states(self):
        src = (_DIR / "index.js").read_text(encoding="utf-8")
        assert "states.unavailable" in src and "DOURMOUSE_WIKI_ROOTS" in src and "states.error(" in src and "ROW_CAP" in src
