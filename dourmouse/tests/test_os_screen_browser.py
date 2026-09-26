"""Finding #153: the BROWSER screen's pure helpers (node) and the rules its DOM code must keep.

The DOM code (the native view's bounds, the hide-on-overlay and hide-on-unmount duties, the
iframe fallback) is verified live, in real Chrome and in a second Electron instance
(EVIDENCE/153_*); what can be checked without a browser is checked here.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_DIR = _ROOT / "ui" / "assets" / "os" / "screens" / "browser"
_NODE = shutil.which("node")


def node(tmp_path, body):
    if _NODE is None:
        pytest.skip("node not on PATH in this environment")
    script = tmp_path / "h.mjs"
    script.write_text(
        f"import * as h from {(_DIR / 'helpers.js').as_uri()!r};\nconst R = {{}};\n{body}\nconsole.log(JSON.stringify(R));\n",
        encoding="utf-8",
    )
    proc = subprocess.run([_NODE, str(script)], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def source(name="index.js"):
    return (_DIR / name).read_text(encoding="utf-8")


class TestAddresses:
    def test_a_typed_address_becomes_http_or_https_and_a_bad_scheme_is_refused_with_the_reason(self, tmp_path):
        out = node(tmp_path, """
R.bare = h.normalizeAddress('example.com/a?b=1');
R.https = h.normalizeAddress('https://Example.com');
R.http = h.normalizeAddress('http://example.com/x');
R.local = h.normalizeAddress('localhost:8080/x');
R.ip = h.normalizeAddress('127.0.0.1:9000');
R.slashes = h.normalizeAddress('//example.com/p');
for (const s of ['javascript:alert(1)', 'JAVASCRIPT:alert(1)', 'file:///etc/passwd', 'data:text/html,x', 'about:blank', 'chrome://settings', 'ftp://h/x', 'blob:abc', 'view-source:http://a.b', 'vbscript:x', 'mailto:a@b.c', 'ws://h/x'])
  R['bad ' + s] = h.normalizeAddress(s);
R.creds = h.normalizeAddress('http://user:pw@example.com/');
R.space = h.normalizeAddress('two words');
R.empty = h.normalizeAddress('   ');
R.tab = h.normalizeAddress('exam\\tple.com');
R.long = h.normalizeAddress('a'.repeat(2001));
R.noHost = h.normalizeAddress('https://');
""")
        assert out["bare"] == {"ok": True, "url": "https://example.com/a?b=1"}
        assert out["https"] == {"ok": True, "url": "https://example.com/"}
        assert out["http"]["url"] == "http://example.com/x"
        assert out["local"] == {"ok": True, "url": "http://localhost:8080/x"}
        assert out["ip"] == {"ok": True, "url": "http://127.0.0.1:9000/"}
        assert out["slashes"]["url"] == "https://example.com/p"
        for k, v in out.items():
            if k.startswith("bad "):
                assert v["ok"] is False and "refused" in v["reason"], k
        assert out["bad javascript:alert(1)"]["reason"].startswith("javascript:")
        assert out["bad file:///etc/passwd"]["reason"].startswith("file:")
        for k in ("creds", "space", "empty", "tab", "long", "noHost"):
            assert out[k]["ok"] is False and out[k]["reason"], k

    def test_normalizing_is_linear_on_a_huge_input(self, tmp_path):
        started = time.time()
        out = node(tmp_path, """
const big = 'a'.repeat(200000);
const t0 = Date.now();
R.a = h.normalizeAddress(big).ok;
R.b = h.normalizeAddress('http://' + big).ok;
R.c = h.normalizeAddress('['.repeat(200000)).ok;
R.d = h.eventTarget('/' + big).ok;
R.e = h.eventTarget('x'.repeat(200000) + ':').ok;
R.ms = Date.now() - t0;
""")
        assert not any(out[k] for k in "abcde")
        assert out["ms"] < 500 and time.time() - started < 10

    def test_an_open_request_may_name_a_web_page_or_a_path_on_this_server_and_nothing_else(self, tmp_path):
        out = node(tmp_path, """
R.web = h.eventTarget('https://example.com/a');
R.app = h.eventTarget('/file_preview?path=%2Fa.pdf');
R.slashes = h.eventTarget('//evil.example/x');
R.back = h.eventTarget('/a\\\\b');
R.file = h.eventTarget('file:///etc/passwd');
R.bare = h.eventTarget('example.com');
R.none = h.eventTarget('');
R.nul = h.eventTarget(null);
""")
        assert out["web"] == {"ok": True, "url": "https://example.com/a", "kind": "web"}
        assert out["app"]["kind"] == "app"
        for k in ("slashes", "back", "file", "bare", "none", "nul"):
            assert out[k]["ok"] is False, k

    def test_the_lock_says_what_the_scheme_really_is(self, tmp_path):
        out = node(tmp_path, """
R.s = h.lockInfo('https://a.b/').kind; R.p = h.lockInfo('http://a.b/').kind; R.n = h.lockInfo('').kind;
R.app = h.lockInfo('/file_preview').kind; R.odd = h.lockInfo('gopher://x').kind;
R.web = [h.isWebUrl('https://a.b'), h.isWebUrl('/x'), h.isWebUrl('javascript:1'), h.isWebUrl('http://')];
""")
        assert (out["s"], out["p"], out["n"], out["app"], out["odd"]) == ("secure", "plain", "none", "app", "other")
        assert out["web"] == [True, False, False, False]


class TestPaneState:
    def test_the_pane_state_is_read_as_it_is_and_junk_is_made_safe(self, tmp_path):
        out = node(tmp_path, """
R.real = h.paneModel({ open: true, url: 'https://a.b/', title: 'A page', loading: false, canGoBack: true, canGoForward: false });
R.junk = h.paneModel({ open: 'yes', url: 5, title: null, loading: 1, error: 'x' });
R.none = h.paneModel(null);
R.err = h.paneModel({ url: 'http://x/', error: { code: -102, description: 'ERR_CONNECTION_REFUSED', url: 'http://x/' } });
R.longTitle = h.paneModel({ title: 'x'.repeat(5000) }).title.length;
""")
        assert out["real"] == {"open": True, "url": "https://a.b/", "title": "A page", "loading": False, "canGoBack": True, "canGoForward": False, "error": None}
        assert out["junk"] == {"open": False, "url": "", "title": "", "loading": False, "canGoBack": False, "canGoForward": False, "error": None}
        assert out["none"]["url"] == "" and out["none"]["error"] is None
        assert out["err"]["error"] == {"code": -102, "description": "ERR_CONNECTION_REFUSED", "url": "http://x/"}
        assert out["longTitle"] == 300

    def test_a_failed_load_is_told_in_the_engines_words_plus_a_plain_reading(self, tmp_path):
        out = node(tmp_path, """
R.known = h.failLine({ code: -102, description: 'ERR_CONNECTION_REFUSED', url: '' });
R.unknown = h.failLine({ code: -999, description: 'ERR_SOMETHING', url: '' });
R.noCode = h.failLine({ code: null, description: '', url: '' });
R.nothing = h.failLine(null);
R.title = [h.tabTitle({ title: 'Hello', url: 'https://a.b/x' }), h.tabTitle({ title: '', url: 'https://a.b/x' }), h.tabTitle({ title: '', url: '' }), h.tabTitle({ title: 'about:blank', url: '' })];
""")
        assert "server refused the connection" in out["known"] and "ERR_CONNECTION_REFUSED" in out["known"]
        assert "ERR_SOMETHING" in out["unknown"] and "-999" in out["unknown"]
        assert out["noCode"] == "The page did not load." and out["nothing"] == ""
        assert out["title"] == ["Hello", "a.b", "No page", "No page"]


class TestGeometry:
    def test_the_native_view_is_clipped_to_the_stage_body_and_gone_when_nothing_shows(self, tmp_path):
        out = node(tmp_path, """
const r = (l, t, w, hh) => ({ left: l, top: t, right: l + w, bottom: t + hh });
R.whole = h.viewBounds(r(100, 200, 800, 500), r(0, 0, 2000, 2000));
R.clipped = h.viewBounds(r(100, 200, 800, 500), r(0, 0, 2000, 400));
R.scrolledAway = h.viewBounds(r(100, 900, 800, 500), r(0, 0, 2000, 400));
R.collapsed = h.viewBounds(r(0, 0, 0, 0), r(0, 0, 2000, 2000));
R.frac = h.viewBounds(r(10.4, 20.6, 100.2, 50.2), null);
R.none = h.viewBounds(null, null);
R.same = [h.sameBounds({ x: 1, y: 2, width: 3, height: 4 }, { x: 1, y: 2, width: 3, height: 4 }), h.sameBounds({ x: 1, y: 2, width: 3, height: 4 }, { x: 1, y: 2, width: 3, height: 5 }), h.sameBounds(null, null)];
""")
        assert out["whole"] == {"x": 100, "y": 200, "width": 800, "height": 500}
        assert out["clipped"] == {"x": 100, "y": 200, "width": 800, "height": 200}
        assert out["scrolledAway"] is None and out["collapsed"] is None and out["none"] is None
        assert out["frac"] == {"x": 11, "y": 21, "width": 99, "height": 49}
        assert out["same"] == [True, False, False]

    def test_widths_and_heights_are_clamped_and_a_bad_stored_value_means_fill(self, tmp_path):
        out = node(tmp_path, """
R.w = [h.clampWidth(100, 1000), h.clampWidth(5000, 1000), h.clampWidth(600, 1000), h.clampWidth('x', 900), h.clampWidth(600, 100)];
R.h = [h.clampHeight(10, 800), h.clampHeight(9999, 800), h.clampHeight(500, 800)];
R.stored = [h.parseStoredWidth('fill'), h.parseStoredWidth('560'), h.parseStoredWidth('12'), h.parseStoredWidth('abc'), h.parseStoredWidth(null), h.parseStoredWidth('99999'), h.parseStoredWidth('560.5')];
R.storedH = [h.parseStoredHeight('fill'), h.parseStoredHeight('500'), h.parseStoredHeight('5'), h.parseStoredHeight('')];
R.preset = [h.presetForWidth(null), h.presetForWidth(380), h.presetForWidth(560), h.presetForWidth(840), h.presetForWidth(700)];
R.presets = h.PRESETS.map((p) => [p.id, p.width]);
""")
        assert out["w"] == [280, 1000, 600, 900, 280]
        assert out["h"] == [240, 800, 500]
        assert out["stored"] == [None, 560, None, None, None, None, None]
        assert out["storedH"] == [None, 500, None, None]
        assert out["preset"] == ["fill", "phone", "tablet", "desktop", ""]
        assert out["presets"] == [["phone", 380], ["tablet", 560], ["desktop", 840], ["fill", None]]


class TestWatchStrip:
    def test_it_claims_the_pane_only_inside_electron_and_only_when_the_addresses_agree(self, tmp_path):
        out = node(tmp_path, """
const ago = () => '5m';
const on = { attached: true, page_url: 'https://a.b/x', last: { at: 'z', kind: 'open', text: 'went to a.b' }, engine_ready: true };
R.same = h.watchLine(on, 'https://a.b/x#frag', 'electron', ago);
R.other = h.watchLine(on, 'https://c.d/', 'electron', ago);
R.browser = h.watchLine(on, 'https://a.b/x', 'browser', ago);
R.blank = h.watchLine({ attached: true, page_url: '', last: null, engine_ready: true }, '', 'electron', ago);
R.off = h.watchLine({ attached: false, engine_ready: true, last: null }, '', 'electron', ago);
R.err = h.watchLine({ attached: false, engine_ready: true, launch_error: 'no chrome', last: null }, '', 'electron', ago);
R.noEngine = h.watchLine({ attached: false, engine_ready: false, engine: 'not installed', last: null }, '', 'electron', ago);
R.none = h.watchLine(null, '', 'electron', ago);
""")
        assert out["same"]["tone"] == "live" and "attached to this pane" in out["same"]["text"] and "5m ago" in out["same"]["text"]
        assert "went to a.b" in out["same"]["text"]
        assert "not the address shown here" in out["other"]["text"]
        assert "attached to this pane" not in out["browser"]["text"] and "not this window" in out["browser"]["text"]
        assert "attached to this pane" in out["blank"]["text"]
        assert "not attached" in out["off"]["text"] and out["off"]["tone"] == ""
        assert out["err"]["tone"] == "warn" and "no chrome" in out["err"]["text"]
        assert out["noEngine"]["tone"] == "warn" and "not installed" in out["noEngine"]["text"]
        assert out["none"] == {"tone": "", "text": ""}
        for k in ("same", "other", "browser", "blank", "off"):
            assert "researcher" not in out[k]["text"].lower()

    def test_the_footnotes_do_not_claim_what_a_plain_browser_cannot_do(self, tmp_path):
        out = node(tmp_path, "R.e = h.footnotes('electron'); R.b = h.footnotes('browser');")
        assert [f["head"] for f in out["e"]] == ["Adjustable", "Shared with the AI", "No proxy"]
        assert [f["head"] for f in out["b"]] == ["Adjustable", "Not shared", "Proxied"]
        assert not any("gone" in f["text"].lower() for f in out["e"] + out["b"])


class TestHistory:
    def test_the_proxied_views_history_walks_the_addresses_opened_here(self, tmp_path):
        out = node(tmp_path, """
const x = h.makeHistory(3);
R.empty = [x.canBack(), x.canForward(), x.current()];
x.push('a'); x.push('b'); x.push('c');
R.three = [x.current(), x.canBack(), x.canForward()];
R.back = [x.back(), x.back(), x.back(), x.canBack()];
R.fwd = [x.forward(), x.canForward()];
x.push('d');
R.branch = [x.current(), x.canForward(), x.size()];
x.push('d');
R.dup = x.size();
x.push('e'); x.push('f');
R.cap = [x.size(), x.current()];
x.clear();
R.cleared = [x.size(), x.current()];
""")
        assert out["empty"] == [False, False, ""]
        assert out["three"] == ["c", True, False]
        assert out["back"] == ["b", "a", "a", False]
        assert out["fwd"] == ["b", True]
        assert out["branch"] == ["d", False, 3]
        assert out["dup"] == 3
        assert out["cap"] == [3, "f"]
        assert out["cleared"] == [0, ""]


class TestSourceRules:
    def test_no_sample_content_of_the_mockup_is_left(self):
        text = source() + source("helpers.js") + source("browser.css")
        for sample in ("Illiquidity", "arxiv.org", "Federal Reserve", "2401.09876", "researcher agent is reading", "for your question", "rewriting proxy is gone"):
            assert sample not in text, sample

    def test_the_iframe_is_sandboxed_without_the_escape_pair_and_only_this_server_feeds_it(self):
        text = source()
        assert "allow-same-origin" not in text
        assert "'allow-scripts allow-forms'" in text
        assert "/api/browser-pane/proxy?url=" in text
        assert "encodeURIComponent(url)" in text

    def test_the_native_view_is_always_hidden_on_leaving_and_while_a_panel_is_over_it(self):
        text = source()
        assert re.search(r"function shutDown\(\)[\s\S]{0,700}pane\.hide\(\)", text)
        assert "ctx.signal.addEventListener('abort', shutDown)" in text
        assert "hooks.get(ctx)" in text and "h.shutDown()" in text
        assert "ctx.overlays.open()" in text and "ctx.overlays.onChange(" in text
        for observed in ("new ResizeObserver", "new MutationObserver", "'scroll'", "data-sidebar"):
            assert observed in text, observed

    def test_it_uses_the_shells_pane_fan_out_and_never_leaves_the_page_by_itself(self):
        text = source()
        assert "ctx.host.onPaneState(" in text
        assert re.search(r"offPane = ctx\.host\.onPaneState\(", text) and re.search(r"function shutDown\(\)[\s\S]{0,900}offPane\(\)", text)
        assert not re.search(r"\.onState\s*\(", text.replace("onPaneState", ""))
        assert "window.open" not in text and "_blank" not in text
        assert "ctx.host.openExternal(" in text
        assert "about:blank" not in text
        assert "dourmouseShell" not in text.replace("window.dourmouseShell.pane, so", "")

    def test_it_handles_the_open_request_and_never_sends_the_agents_page_anywhere(self):
        text = source()
        assert "ctx.events.on('browser_pane_open'" in text
        assert "ctx.events.onResync(" in text
        assert "eventTarget(" in text and "normalizeAddress(" in text

    def test_every_control_has_a_spec_sentence_and_no_em_dash_anywhere(self):
        text = source()
        for control in ("bwBack", "bwFwd", "bwReload", "bwAddr", "bwPresets", "bwResR", "bwResB"):
            m = re.search(rf'id="{control}"[^>]*data-spec="[^"]{{20,}}"', text)
            assert m, control
        assert "b.dataset.spec = PRESET_SPEC" in text and "spec: 'Opens the address" in text
        for path in _DIR.iterdir():
            assert "—" not in path.read_text(encoding="utf-8"), path.name
