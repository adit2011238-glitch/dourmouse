"""Finding #146: the NEWS screen's pure helpers and source rules."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_DIR = _ROOT / "ui" / "assets" / "os" / "screens" / "news"
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
    def test_only_http_links_are_offered(self, tmp_path):
        out = node(tmp_path, "R.a=h.safeLink('https://a.org/x'); R.b=h.safeLink('javascript:alert(1)'); R.c=h.safeLink('a b'); R.d=h.safeLink(''); R.e=h.safeLink('//evil.com/x');")
        assert out == {"a": "https://a.org/x", "b": "", "c": "", "d": "", "e": ""}

    def test_channel_words_and_the_item_key(self, tmp_path):
        out = node(tmp_path, "R.a=h.channelWord('conflict_events'); R.b=h.channelWord('news'); R.c=h.channelWord(''); R.k=h.itemKey({channel:'n',title:'t',link:'l'});")
        assert out == {"a": "conflict", "b": "news", "c": "feed", "k": "n|t|l"}

    def test_the_watcher_line_says_what_the_server_reported(self, tmp_path):
        out = node(
            tmp_path,
            "R.off=h.watcherLine({running:false,status:null}).tone; R.bad=h.watcherLine({running:true,status:{last_poll_ok:false,last_error:'boom',poll_interval_seconds:180}});"
            "R.wait=h.watcherLine({running:true,status:{last_poll_ok:null}}).tone; R.ok=h.watcherLine({running:true,status:{last_poll_ok:true,poll_interval_seconds:180}}).text;",
        )
        assert out["off"] == "off" and out["bad"]["tone"] == "bad" and "boom" in out["bad"]["text"] and "180" in out["bad"]["text"]
        assert out["wait"] == "wait" and "180 seconds" in out["ok"]

    def test_the_research_prompt_names_the_headline_and_the_side_effect(self, tmp_path):
        out = node(tmp_path, "R.p=h.researchPrompt({title:'Grid load'});")
        assert "Grid load" in out["p"] and "local research database" in out["p"] and "model" in out["p"]

    def test_the_feed_cap_is_stated(self, tmp_path):
        assert node(tmp_path, "R.c=h.FEED_CAP;") == {"c": 60}


class TestSourceRules:
    def test_no_mockup_sample_headlines_or_wrong_claims_remain(self):
        src = _src()
        for sample in ("Grid operators report", "Storm system tracked", "Air quality advisories", "reuters", "Polls every 120s", "Keyless RSS"):
            assert sample not in src, sample

    def test_headline_text_only_reaches_the_dom_as_text(self):
        src = (_DIR / "index.js").read_text(encoding="utf-8")
        assert "el('button', 'news-title', String(item.title" in src
        assert "innerHTML" not in src and "setHtml(" in src
        assert "setHtml(root, html`" in src

    def test_the_live_list_is_a_ring_and_the_screen_never_polls(self):
        src = (_DIR / "index.js").read_text(encoding="utf-8")
        assert "ring(FEED_CAP)" in src and "ctx.every" not in src
        assert "ctx.events.on('news_item'" in src and "ctx.events.onResync" in src

    def test_to_research_asks_first_and_the_atlas_quant_lab_is_not_linked(self):
        src = (_DIR / "index.js").read_text(encoding="utf-8")
        assert "approvalCard" in src and "/api/os/news/research" in src
        assert "'Escape'" in src
