"""Finding #151: the RESEARCH screen's pure helpers (node) and source rules."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_DIR = _ROOT / "ui" / "assets" / "os" / "screens" / "research"
_NODE = shutil.which("node")


def node(tmp_path, body):
    if _NODE is None:
        pytest.skip("node not on PATH in this environment")
    script = tmp_path / "h.mjs"
    script.write_text(f"import * as h from {(_DIR / 'helpers.js').as_uri()!r};\nconst R = {{}};\n{body}\nconsole.log(JSON.stringify(R));\n", encoding="utf-8")
    proc = subprocess.run([_NODE, str(script)], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestLoop:
    def test_boxes_are_built_only_where_the_server_said_so(self, tmp_path):
        out = node(tmp_path, "R.t = h.loopBoxes([{ key: 'a', title: 'A', built: true }, { key: 'b', title: 'B', built: false }, { key: 'c', title: 'C' }]).map((b) => b.tone); R.e = h.loopBoxes(null);")
        assert out == {"t": ["built", "todo", "todo"], "e": []}

    def test_sub_and_counts_use_only_real_numbers(self, tmp_path):
        out = node(tmp_path, r"""
R.a = h.stagesSub({ built: 11, total: 15 }); R.b = h.stagesSub(null);
R.c = h.countsLine({ research_question: 1, claim: 12, source: 'x', hypothesis: 0 }); R.d = h.countsLine(null);
""")
        assert out["a"] == "pipeline · 11 of 15 stages have a tool" and out["b"] == "research pipeline"
        assert out["c"] == "1 question, 12 claims, 0 hypotheses" and out["d"] == ""


class TestClaims:
    def test_tags_mean_what_the_store_says(self, tmp_path):
        out = node(tmp_path, r"""
R.t = [{ status: 'ACTIVE' }, { status: 'ACTIVE', contested: true }, { status: 'ACTIVE', contradicted_by: ['c1'] }, { status: 'rejected' }, {}].map((c) => h.claimTag(c).text);
""")
        assert out["t"] == ["quote matched", "contested", "contested", "rejected", "quote matched"]

    def test_only_http_and_https_are_linked(self, tmp_path):
        out = node(tmp_path, r"""
R.u = ['https://a.example/x?y=1', 'http://b.example', 'javascript:alert(1)', 'data:text/html,x', 'file:///etc/passwd', '', 'https://a b', null].map(h.safeUrl);
R.h = [h.hostOf('https://Sub.Example.com:8080/p'), h.hostOf('ftp://x'), h.hostOf('')];
""")
        assert out["u"] == ["https://a.example/x?y=1", "http://b.example", "", "", "", "", "", ""]
        assert out["h"] == ["Sub.Example.com", "", ""]

    def test_a_200kb_url_is_rejected_quickly(self, tmp_path):
        out = node(tmp_path, "const t0 = Date.now(); R.r = h.safeUrl('https://' + 'a'.repeat(200000)); R.ms = Date.now() - t0;")
        assert out["r"] == "" and out["ms"] < 500

    def test_filename_cannot_carry_a_path(self, tmp_path):
        out = node(tmp_path, "R.a = h.safeFilename('../../etc/passwd'); R.b = h.safeFilename(''); R.c = h.safeFilename('is-coffee-good.md');")
        assert out["a"] == "..-..-etc-passwd".lstrip(".") or "/" not in out["a"]
        assert "/" not in out["a"] and out["b"] == "research.md" and out["c"] == "is-coffee-good.md"


class TestGraphLayout:
    def test_root_on_top_rows_by_kind_and_edges_resolve_to_positions(self, tmp_path):
        out = node(tmp_path, r"""
const nodes = [{ id: 'q1', type: 'research_question', label: 'Q' }, { id: 'q2', type: 'research_question', label: 'sub' },
  { id: 'c1', type: 'claim', label: 'c1' }, { id: 'c2', type: 'claim', label: 'c2' }, { id: 'x1', type: 'contradiction', label: 'x' }];
const edges = [{ src: 'q1', src_type: 'research_question', relation: 'decomposes_into', dst: 'q2', dst_type: 'research_question' },
  { src: 'c1', src_type: 'claim', relation: 'contradicted_by', dst: 'c2', dst_type: 'claim' },
  { src: 'ghost', src_type: 'claim', relation: 'about', dst: 'c1', dst_type: 'claim' }];
const g = h.layoutGraph(nodes, edges, 'q1');
const at = (id) => g.nodes.find((n) => n.id === id);
R.ys = [at('q1').y, at('q2').y, at('c1').y, at('x1').y];
R.lines = g.lines.length; R.h = g.height > 0; R.inside = g.nodes.every((n) => n.x > 0 && n.x < g.width);
""")
        assert out["ys"][0] < out["ys"][1] < out["ys"][2] < out["ys"][3]
        assert out["lines"] == 2, "an edge to a node that was not returned is dropped, not drawn to nowhere"
        assert out["inside"]

    def test_many_nodes_wrap_into_rows(self, tmp_path):
        out = node(tmp_path, r"""
const nodes = [{ id: 'r', type: 'hypothesis', label: 'H' }].concat(Array.from({ length: 12 }, (_, i) => ({ id: 'c' + i, type: 'claim', label: 'c' + i })));
const g = h.layoutGraph(nodes, [], 'r'); R.rows = new Set(g.nodes.filter((n) => n.type === 'claim').map((n) => n.y)).size;
""")
        assert out["rows"] == 3


class TestSource:
    FILES = ["index.js", "helpers.js", "source-widget.js", "graph-svg.js", "research.css"]

    def test_no_mockup_sample_data_and_no_em_dash(self):
        for name in self.FILES:
            text = (_DIR / name).read_text(encoding="utf-8")
            for sample in ("4 of 14", "range requests", "A 206 response", "Chunk size", "does not exist yet"):
                assert sample.lower() not in text.lower(), (name, sample)
            assert "—" not in text and "–" not in text, name

    def test_the_source_widget_never_uses_html_and_links_only_through_the_host(self):
        text = (_DIR / "source-widget.js").read_text(encoding="utf-8")
        assert "innerHTML" not in text and "setHtml" not in text and "href" not in text
        assert "safeUrl" in text and "openExternal" in text
