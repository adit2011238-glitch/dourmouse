"""Finding #144: the OS shell's core and kit modules, run in node.

Each case is a small .mjs harness that imports the real module from
ui/assets/os/ and prints JSON, so what is tested is the shipped file, not a
copy. Core modules must import in node with no window, document or
localStorage at the top level. The suite skips when node is missing.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_OS = _ROOT / "ui" / "assets" / "os"
_MOCKUP = _ROOT / "ui" / "os_mockup.html"
_NODE = shutil.which("node") or ""

pytestmark = pytest.mark.skipif(not _NODE, reason="node not on PATH in this environment")


def run(tmp_path: Path, body: str, imports: str = "") -> dict:
    """Run a harness. `imports` uses the names core/x.js and kit/x.js."""
    header = re.sub(
        r"from '((?:core|kit|chrome|screens)/[^']+)'",
        lambda m: f"from '{(_OS / m.group(1)).as_uri()}'",
        imports,
    )
    script = tmp_path / "harness.mjs"
    script.write_text(header + "\nconst R = {};\n" + body + "\nconsole.log(JSON.stringify(R));\n", encoding="utf-8")
    proc = subprocess.run([_NODE, str(script)], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    return json.loads(proc.stdout.strip().splitlines()[-1])


MODULES = [
    "core/api.js", "core/approvals.js", "core/chat.js", "core/ctx.js", "core/events.js", "core/host.js",
    "core/keymap.js", "core/prefs.js", "core/registry.js", "core/ring.js", "core/router.js", "core/scope.js",
    "kit/html.js", "kit/states.js", "kit/approval-card.js", "kit/md.js", "kit/flow-svg.js", "kit/icons.js", "kit/format.js",
    "kit/thread-helpers.js", "kit/thread-view.js", "kit/confirm-card.js", "chrome/startup-check.js",
]


class TestImportSafety:
    @pytest.mark.parametrize("module", MODULES)
    def test_module_imports_in_node_without_a_browser(self, tmp_path, module):
        # node has no window, document or localStorage: a top-level use would throw here
        out = run(tmp_path, "R.ok = typeof m === 'object';", f"import * as m from '{module}';")
        assert out["ok"] is True

    def test_every_js_file_passes_node_check(self):
        for path in sorted(_OS.rglob("*.js")):
            proc = subprocess.run([_NODE, "--check", str(path)], capture_output=True, text=True, timeout=30, check=False)
            assert proc.returncode == 0, f"{path}: {proc.stderr}"


class TestApiErrorMapping:
    IMPORTS = "import { createApi, ApiError, isAbort, createSseParser } from 'core/api.js';"

    def test_server_error_text_is_the_message(self, tmp_path):
        out = run(tmp_path, """
const j = (body, status, headers = {}) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json', ...headers } });
const api = createApi({ fetch: async () => j({ ok: false, error: 'x' }, 500) });
try { await api.get('/a'); R.threw = false; } catch (e) { R.threw = true; R.isApi = e instanceof ApiError; R.message = e.message; R.status = e.status; R.path = e.path; R.offline = e.offline; }
""", self.IMPORTS)
        assert out == {"threw": True, "isApi": True, "message": "x", "status": 500, "path": "/a", "offline": False}

    def test_a_200_with_ok_false_is_an_error_too(self, tmp_path):
        out = run(tmp_path, """
const j = (body, status) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
let api = createApi({ fetch: async () => j({ ok: false, error: 'id is required' }, 200) });
try { await api.post('/b', {}); R.a = 'no throw'; } catch (e) { R.a = e.message; R.aStatus = e.status; }
api = createApi({ fetch: async () => j({ ok: false }, 200) });
try { await api.get('/c'); R.b = 'no throw'; } catch (e) { R.b = e.message; }
api = createApi({ fetch: async () => j({ ok: false, note: 'kept' }, 200) });
const meta = await api.getMeta('/d', { allowOkFalse: true }).then((r) => r.data.note);
R.allowed = meta;
""", self.IMPORTS)
        assert out["a"] == "id is required" and out["aStatus"] == 200
        assert "refused by the server" in out["b"] and "/c" in out["b"]
        assert out["allowed"] == "kept"

    def test_a_non_json_failure_names_the_status_not_a_blank(self, tmp_path):
        out = run(tmp_path, """
const api = createApi({ fetch: async () => new Response('<html>oops</html>', { status: 404, statusText: 'Not Found' }) });
try { await api.get('/nope'); } catch (e) { R.message = e.message; R.status = e.status; R.body = e.body; }
""", self.IMPORTS)
        assert out["status"] == 404 and out["body"] is None
        assert "/nope" in out["message"] and "404" in out["message"]

    def test_the_stale_header_marks_the_data(self, tmp_path):
        out = run(tmp_path, """
const res = () => new Response(JSON.stringify({ n: 1 }), { status: 200, headers: { 'content-type': 'application/json', 'X-Dourmouse-Stale': '1' } });
const api = createApi({ fetch: async () => res() });
const d = await api.get('/api/state');
R.stale = api.isStale(d); R.n = d.n; R.enumerable = Object.keys(d);
const fresh = createApi({ fetch: async () => new Response('{"n":2}', { status: 200, headers: { 'content-type': 'application/json' } }) });
R.freshStale = fresh.isStale(await fresh.get('/x'));
""", self.IMPORTS)
        assert out == {"stale": True, "n": 1, "enumerable": ["n"], "freshStale": False}

    def test_a_network_failure_is_offline_and_an_abort_is_not_an_error(self, tmp_path):
        out = run(tmp_path, """
let api = createApi({ fetch: async () => { throw new TypeError('Failed to fetch'); } });
try { await api.get('/z'); } catch (e) { R.offline = e.offline; R.isApi = e instanceof ApiError; R.message = e.message; }
const ac = new AbortController(); ac.abort();
api = createApi({ fetch: async (_p, init) => { if (init.signal.aborted) { const e = new Error('aborted'); e.name = 'AbortError'; throw e; } return new Response('{}'); } });
try { await api.get('/z', { signal: ac.signal }); } catch (e) { R.abortIsApi = e instanceof ApiError; R.abortIs = isAbort(e); }
""", self.IMPORTS)
        assert out["offline"] is True and out["isApi"] is True and "Failed to fetch" in out["message"]
        assert out["abortIsApi"] is False and out["abortIs"] is True

    def test_the_sse_parser_survives_chunks_split_anywhere(self, tmp_path):
        out = run(tmp_path, """
const got = [];
const p = createSseParser((e) => got.push(e));
const wire = 'data: {"type":"a","n":1}\\n\\ndata: {"type":"b"}\\r\\n\\n: comment\\ndata: not json\\ndata: {"type":"c"}';
for (const ch of wire) p.push(ch);
p.flush();
R.types = got.map((e) => e.type);
""", self.IMPORTS)
        assert out["types"] == ["a", "b", "c"]


class TestEvents:
    IMPORTS = "import { createEvents } from 'core/events.js';"
    FAKE = """
class FakeES { static all = []; constructor(url) { this.url = url; this.readyState = 0; FakeES.all.push(this); }
  close() { this.readyState = 2; } open() { this.readyState = 1; this.onopen && this.onopen(); }
  msg(o) { this.onmessage && this.onmessage({ data: JSON.stringify(o) }); }
  drop(closed = false) { this.readyState = closed ? 2 : 0; this.onerror && this.onerror(); } }
"""

    def test_types_prefixes_and_predicates_are_delivered(self, tmp_path):
        out = run(tmp_path, self.FAKE + """
const ev = createEvents({ EventSourceImpl: FakeES, setTimer: () => 0, clearTimer: () => {} });
ev.start(); const es = FakeES.all[0]; es.open();
const seen = { exact: [], prefix: [], pred: [] };
ev.on('news_item', (e) => seen.exact.push(e.type));
ev.on('security_*', (e) => seen.prefix.push(e.type));
ev.on((e) => e.section === 'alerts', (e) => seen.pred.push(e.type));
es.msg({ type: 'news_item' }); es.msg({ type: 'security_scan' }); es.msg({ type: 'security_download' });
es.msg({ type: 'state_change', section: 'alerts' }); es.msg({ type: 'state_change', section: 'prefs' });
R.seen = seen; R.sources = ev.sources();
""", self.IMPORTS)
        assert out["seen"] == {"exact": ["news_item"], "prefix": ["security_scan", "security_download"], "pred": ["state_change"]}
        assert out["sources"] == 1

    def test_resync_runs_only_after_a_real_drop_and_reconnect(self, tmp_path):
        out = run(tmp_path, self.FAKE + """
const ev = createEvents({ EventSourceImpl: FakeES, setTimer: (fn) => { R.retryQueued = true; return 1; }, clearTimer: () => {} });
ev.start(); const es = FakeES.all[0];
let resyncs = 0; ev.onResync(() => { resyncs += 1; });
es.open(); R.afterFirstOpen = resyncs;
es.drop(false); es.open(); R.afterReconnect = resyncs;
es.open(); R.afterSpuriousOpen = resyncs;
es.drop(true); R.retryQueued = R.retryQueued === true;
""", self.IMPORTS)
        assert out["afterFirstOpen"] == 0 and out["afterReconnect"] == 1 and out["afterSpuriousOpen"] == 1
        assert out["retryQueued"] is True

    def test_unmount_cleanup_removes_every_subscription_of_that_scope(self, tmp_path):
        out = run(tmp_path, self.FAKE + """
const ev = createEvents({ EventSourceImpl: FakeES, setTimer: () => 0, clearTimer: () => {} });
ev.start(); const es = FakeES.all[0]; es.open();
ev.on('chrome_evt', () => {});
const base = ev.count();
const scope = ev.scope(); let hits = 0, resyncs = 0;
scope.on('x', () => { hits += 1; }); scope.onResync(() => { resyncs += 1; }); scope.onStatus(() => {});
R.during = ev.count() - base;
scope.dispose();
R.after = ev.count() - base;
es.msg({ type: 'x' }); es.drop(); es.open();
R.hits = hits; R.resyncs = resyncs;
""", self.IMPORTS)
        assert out == {"during": 3, "after": 0, "hits": 0, "resyncs": 0}

    def test_a_throwing_handler_does_not_stop_the_others(self, tmp_path):
        out = run(tmp_path, self.FAKE + """
const errs = [];
const ev = createEvents({ EventSourceImpl: FakeES, setTimer: () => 0, clearTimer: () => {}, onError: (e) => errs.push(e.message) });
ev.start(); const es = FakeES.all[0]; es.open();
let ok = 0; ev.on('t', () => { throw new Error('boom'); }); ev.on('t', () => { ok += 1; });
es.msg({ type: 't' }); R.ok = ok; R.errs = errs;
""", self.IMPORTS)
        assert out == {"ok": 1, "errs": ["boom"]}


class TestRingAndFormat:
    def test_ring_never_exceeds_its_cap(self, tmp_path):
        out = run(tmp_path, """
const r = ring(5); const dropped = [];
for (let i = 0; i < 1000; i++) dropped.push(r.push(i));
R.size = r.size; R.items = r.items(); R.newest = r.newestFirst()[0]; R.droppedTotal = dropped.reduce((a, b) => a + b, 0);
const copy = r.items(); copy.push(99); R.sizeAfterCopyPush = r.size;
try { ring(0); R.bad = 'no throw'; } catch (e) { R.bad = e.name; }
""", "import { ring } from 'core/ring.js';")
        assert out == {"size": 5, "items": [995, 996, 997, 998, 999], "newest": 999, "droppedTotal": 995,
                       "sizeAfterCopyPush": 5, "bad": "RangeError"}

    def test_time_formatting_accepts_seconds_millis_and_iso(self, tmp_path):
        out = run(tmp_path, """
const now = Date.parse('2026-09-26T12:00:00Z');
R.sec = ago(now / 1000 - 90, now); R.ms = ago(now - 3 * 3600 * 1000, now); R.iso = ago('2026-09-24T12:00:00Z', now);
R.bad = ago('nonsense', now); R.none = ago(null, now); R.now = ago(now, now);
R.secs = seconds(2412); R.negative = seconds(-5); R.plural = [plural(1, 'finding'), plural(2, 'finding')];
R.label = [agoLabel(now, now), agoLabel(now - 180000, now), agoLabel('junk', now)];
""", "import { ago, seconds, plural, agoLabel } from 'kit/format.js';")
        assert out == {"sec": "1m", "ms": "3h", "iso": "2d", "bad": "", "none": "", "now": "now",
                       "secs": "2.4s", "negative": "", "plural": ["1 finding", "2 findings"],
                       "label": ["just now", "3m ago", ""]}


class TestKeymap:
    IMPORTS = "import { createKeymap, isEditableTarget } from 'core/keymap.js';"

    def test_editable_targets_never_fire_a_shortcut(self, tmp_path):
        out = run(tmp_path, """
const km = createKeymap(); let fired = 0;
km.bind('alt+a', () => { fired += 1; }); km.bind('r', () => { fired += 100; });
const key = (target, extra = {}) => km.handle({ key: 'a', altKey: true, target, ...extra });
R.input = key({ tagName: 'INPUT' }); R.textarea = key({ tagName: 'TEXTAREA' }); R.select = key({ tagName: 'SELECT' });
R.ce = key({ tagName: 'DIV', isContentEditable: true });
R.ceAttr = key({ tagName: 'DIV', getAttribute: (n) => (n === 'contenteditable' ? 'true' : null) });
R.firedInEditable = fired;
R.div = key({ tagName: 'DIV', getAttribute: () => null });
R.firedAfter = fired;
R.bareA = km.handle({ key: 'a', target: { tagName: 'DIV', getAttribute: () => null } });
R.editableHelper = [isEditableTarget({ tagName: 'input' }), isEditableTarget({ tagName: 'BUTTON' }), isEditableTarget(null)];
""", self.IMPORTS)
        assert out["input"] is False and out["textarea"] is False and out["select"] is False
        assert out["ce"] is False and out["ceAttr"] is False and out["firedInEditable"] == 0
        assert out["div"] is True and out["firedAfter"] == 1
        assert out["bareA"] is False, "a bare A must not toggle anything (Alt+A only)"
        assert out["editableHelper"] == [True, False, False]

    def test_escape_pops_the_newest_closer_and_reaches_panels_from_a_field(self, tmp_path):
        out = run(tmp_path, """
const km = createKeymap(); const order = [];
const offA = km.pushEsc(() => order.push('a')); km.pushEsc(() => order.push('b'));
R.depth = km.escDepth();
R.first = km.handle({ key: 'Escape', target: { tagName: 'INPUT' } });
offA(); R.depthAfterOff = km.escDepth();
R.second = km.handle({ key: 'Escape', target: { tagName: 'DIV' } });
R.order = order;
""", self.IMPORTS)
        # handle() only calls the newest closer; a real panel's closer pops itself
        assert out == {"depth": 2, "first": True, "depthAfterOff": 1, "second": True, "order": ["b", "b"]}
        out = run(tmp_path, "const km = createKeymap(); R.empty = km.handle({ key: 'Escape', target: { tagName: 'DIV' } });", self.IMPORTS)
        assert out["empty"] is False

    def test_unbinding_and_counts(self, tmp_path):
        out = run(tmp_path, """
const km = createKeymap(); let n = 0;
const off = km.bind('ctrl+k', () => { n += 1; });
const ev = { key: 'k', ctrlKey: true, target: { tagName: 'DIV', getAttribute: () => null } };
km.handle(ev); R.one = n; off(); km.handle(ev); R.two = n; R.count = km.count();
R.wrongMod = km.bind('alt+a', () => { n += 10; }) && km.handle({ key: 'a', target: { tagName: 'DIV', getAttribute: () => null } });
""", self.IMPORTS)
        assert out == {"one": 1, "two": 1, "count": 0, "wrongMod": False}


class TestHtmlEscaping:
    IMPORTS = "import { esc, html, raw, SafeHtml } from 'kit/html.js';"

    def test_all_five_characters_are_escaped(self, tmp_path):
        out = run(tmp_path, "R.out = esc(`&<>\"'`); R.nullish = [esc(null), esc(undefined), esc(0), esc(false)];", self.IMPORTS)
        assert out["out"] == "&amp;&lt;&gt;&quot;&#39;"
        assert out["nullish"] == ["", "", "0", "false"]

    def test_template_values_are_escaped_and_nested_templates_are_not_double_escaped(self, tmp_path):
        out = run(tmp_path, """
const evil = '<img src=x onerror=alert(1)>"\\'';
R.plain = html`<div title="${evil}">${evil}</div>`.text;
R.nested = html`<ul>${['a', 'b<'].map((x) => html`<li>${x}</li>`)}</ul>`.text;
R.gaps = html`<i>${null}${undefined}${false}${0}</i>`.text;
R.rawOk = html`<b>${raw('<i>trusted</i>')}</b>`.text;
R.isSafe = html`x` instanceof SafeHtml;
""", self.IMPORTS)
        assert "<img" not in out["plain"] and "&lt;img" in out["plain"] and 'title="&lt;img' in out["plain"]
        assert "&quot;" in out["plain"] and "&#39;" in out["plain"]
        assert out["nested"] == "<ul><li>a</li><li>b&lt;</li></ul>"
        assert out["gaps"] == "<i>0</i>"
        assert out["rawOk"] == "<b><i>trusted</i></b>" and out["isSafe"] is True

    def test_markdown_from_a_model_cannot_inject_markup(self, tmp_path):
        out = run(tmp_path, """
const evil = '<script>alert(1)</script> [x](javascript:alert(1)) [ok](https://example.com/a?b=1&c="q") **b** `<i>`';
R.out = md(evil).text;
R.fence = md('```js\\n<b>x</b>\\n```').text;
""", "import { md } from 'kit/md.js';")
        assert "<script" not in out["out"] and "&lt;script&gt;" in out["out"]
        assert "javascript:" in out["out"] and "<a href=\"javascript" not in out["out"]
        assert '<a href="https://example.com/a?b=1&amp;c=&quot;q&quot;"' in out["out"]
        assert "<b>b</b>" in out["out"] and "<code>&lt;i&gt;</code>" in out["out"]
        assert "&lt;b&gt;x&lt;/b&gt;" in out["fence"] and "<b>x</b>" not in out["fence"]


class TestRegexesStayLinear:
    """Every new regex is timed on 200 KB of the worst shapes (project rule)."""

    def test_markdown_renderer_on_hostile_200kb_inputs(self, tmp_path):
        out = run(tmp_path, """
const N = 200000; const times = {};
const cases = { brackets: '['.repeat(N), pairs: '[a'.repeat(N / 2), stars: '**a'.repeat(N / 3), ticks: '`a'.repeat(N / 2),
  openLinks: '[a](http://x'.repeat(N / 12), fences: '```js\\n'.repeat(N / 6), lines: 'a\\n'.repeat(N / 2), mixed: 'ab*`[](http://a) '.repeat(N / 17) };
for (const [k, v] of Object.entries(cases)) { const t = Date.now(); md(v); times[k] = Date.now() - t; }
R.max = Math.max(...Object.values(times)); R.times = times;
""", "import { md } from 'kit/md.js';")
        assert out["max"] < 3000, out["times"]

    def test_affirm_router_and_avatar_photo_regexes_on_200kb(self, tmp_path):
        out = run(tmp_path, """
const N = 200000; const t0 = Date.now();
for (const v of ['a' + ' '.repeat(N) + 'b', 'a' + '!'.repeat(N) + 'b', ' '.repeat(N), 'yes' + '. '.repeat(N / 2)]) isImperativeAffirm(v);
for (const h of ['#/' + 'a'.repeat(N), '#' + '/'.repeat(N), '#/a' + '/b'.repeat(N / 2)]) resolveRoute(h);
R.ms = Date.now() - t0;
""", "import { isImperativeAffirm } from 'core/chat.js';\nimport { resolveRoute } from 'core/router.js';")
        assert out["ms"] < 2000


class TestRegistry:
    IMPORTS = "import { IDS, SCREENS, THREAD_SCREENS, DOCK, CONSOLE_LINKS, byId, bySlug } from 'core/registry.js';"

    def test_registry_lists_exactly_the_eighteen_mockup_ids_in_mockup_order(self, tmp_path):
        mockup = _MOCKUP.read_text(encoding="utf-8")
        m = re.search(r"var ORDER = \[(.*?)\];", mockup, re.S)
        assert m, "the mockup no longer declares ORDER"
        expected = re.findall(r'"([A-Z]+)"', m.group(1))
        assert len(expected) == 18
        out = run(tmp_path, "R.ids = IDS; R.slugs = SCREENS.map((s) => s.slug); R.threads = THREAD_SCREENS; R.dock = DOCK.map((d) => d[0]);", self.IMPORTS)
        assert out["ids"] == expected
        assert out["slugs"] == [i.lower() for i in expected]
        assert set(out["dock"]) <= set(expected)

    def test_console_links_are_the_three_owner_decided_screens_and_not_screens(self, tmp_path):
        out = run(tmp_path, "R.links = CONSOLE_LINKS.map((l) => l[0]); R.inIds = CONSOLE_LINKS.some((l) => IDS.includes(l[0])); R.lookup = [byId('HOME').id, bySlug('Security').id, byId('NOPE'), bySlug('vision')];", self.IMPORTS)
        assert out["links"] == ["VISION", "GLOBE", "DESIGN3D"] and out["inIds"] is False
        assert out["lookup"] == ["HOME", "SECURITY", None, None]

    def test_thread_screens_match_the_console(self, tmp_path):
        console = (_ROOT / "ui" / "console.html").read_text(encoding="utf-8")
        m = re.search(r"THREAD_SCREENS\s*=\s*\[([^\]]*)\]", console)
        assert m
        console_threads = set(re.findall(r'"([A-Z0-9]+)"', m.group(1)))
        out = run(tmp_path, "R.threads = THREAD_SCREENS;", self.IMPORTS)
        # DESIGN3D is not one of the eighteen (it stays in the console)
        assert set(out["threads"]) == console_threads - {"DESIGN3D"}


class TestRouterRules:
    IMPORTS = "import { resolveRoute, parseHash } from 'core/router.js';"

    def test_deep_links_and_unknown_destinations(self, tmp_path):
        out = run(tmp_path, """
const r = (h, cur) => resolveRoute(h, cur);
R.home = r(''); R.sec = r('#/security'); R.world = r('#/world'); R.atlas = r('#/atlas'); R.settings = r('#/settings');
R.alerts = r('#/alerts', 'NEWS'); R.market = r('#/markets'); R.junk = r('#/../../etc'); R.unknown = r('#/nonsense');
R.parts = parseHash('#/home/a/b');
""", self.IMPORTS)
        assert out["home"]["id"] == "HOME" and out["home"]["notice"] == ""
        assert out["sec"]["id"] == "SECURITY"
        assert out["world"]["id"] == "ATLAS" and out["atlas"]["id"] == "ATLAS" and out["settings"]["id"] == "SETTINGS"
        assert out["alerts"] == {"id": "NEWS", "openPanel": "notifications", "notice": ""}
        assert out["market"]["id"] == "HOME" and "no screen" in out["market"]["notice"]
        assert out["junk"]["id"] == "HOME" and "Unknown destination" in out["junk"]["notice"]
        assert out["unknown"]["id"] == "HOME" and "nonsense" in out["unknown"]["notice"]
        assert out["parts"] == {"slug": "home", "rest": ["a", "b"]}


class TestChatCore:
    IMPORTS = "import { newTurn, applyEvent, isImperativeAffirm, turnFromLedger, createChat } from 'core/chat.js';\nimport { createApprovals } from 'core/approvals.js';\nimport { createScope } from 'core/scope.js';"

    def test_a_turn_folds_a_real_event_sequence(self, tmp_path):
        out = run(tmp_path, """
const t = newTurn('hi', 1000);
const evs = [{ type: 'brain', model: 'server:org/glm-5' }, { type: 'tool_use', name: 'security_status', raw_arguments: '{"a":1}' },
  { type: 'tool_result', name: 'security_status', text: 'ok body' }, { type: 'assistant_delta', text: 'Hel' }, { type: 'assistant_delta', text: 'lo' },
  { type: 'tool_use', name: 'x', raw_arguments: '' }, { type: 'tool_result', name: 'x', text: 'ERROR: bad' }, { type: 'done', final_text: 'Hello' }];
evs.forEach((e) => applyEvent(t, e));
R.model = t.model; R.reply = t.reply; R.steps = t.steps.map((s) => [s.name, s.running, s.result, s.args]);
""", self.IMPORTS)
        assert out["model"] == "glm-5" and out["reply"] == "Hello"
        assert out["steps"] == [["security_status", False, "ok body", '{\n  "a": 1\n}'], ["x", False, "ERROR: bad", ""]]

    def test_send_it_only_counts_as_a_whole_message(self, tmp_path):
        out = run(tmp_path, "R.a = ['send it', 'Yes!', ' go ahead. '].map(isImperativeAffirm); R.b = ['send it to bob', 'please yes', '', null].map(isImperativeAffirm);", self.IMPORTS)
        assert out["a"] == [True, True, True] and out["b"] == [False, False, False, False]

    def test_threads_fall_back_to_home_and_survive_and_stop_declines_approvals(self, tmp_path):
        out = run(tmp_path, """
const posts = [];
const api = { post: async (p, b) => { posts.push([p, b]); return { ok: true }; },
  stream: async (path, body, onEvent, opts) => {
    R.body = body;
    onEvent({ type: 'confirmation_requested', id: 'c1', prompt: 'Delete?', tool: 'rm' });
    await new Promise((res, rej) => { opts.signal.addEventListener('abort', () => { const e = new Error('a'); e.name = 'AbortError'; rej(e); }); });
  } };
const scope = createScope({ storage: null });
const approvals = createApprovals({ api, scope });
const chat = createChat({ api, scope, approvals, threadScreens: ['HOME', 'NEWS'], storage: null });
R.keys = [chat.keyFor('NEWS'), chat.keyFor('SECURITY')];
R.same = chat.thread('SECURITY') === chat.thread('HOME');
const th = chat.thread('HOME');
const pending = th.send('do it');
await new Promise((r) => setTimeout(r, 10));
R.busy = th.busy(); R.open = approvals.openCount(); R.pendingId = approvals.pending('HOME').id;
await th.stop(); await pending;
R.after = { busy: th.busy(), status: th.turns()[0].status, open: approvals.openCount(), post: posts[0] };
R.auto = chat.autonomous(); chat.setAutonomous(true); R.auto2 = chat.autonomous();
""", self.IMPORTS)
        assert out["keys"] == ["NEWS", "HOME"] and out["same"] is True
        assert out["body"]["screen"] == "HOME" and out["body"]["autonomous"] is False and out["body"]["tab_id"]
        assert out["busy"] is True and out["open"] == 1 and out["pendingId"] == "c1"
        assert out["after"]["busy"] is False and out["after"]["status"] == "stopped" and out["after"]["open"] == 0
        assert out["after"]["post"][0] == "/api/confirm" and out["after"]["post"][1]["approved"] is False
        assert out["auto"] is False and out["auto2"] is True

    def test_decided_approvals_are_forgotten_past_a_cap_but_pending_ones_never(self, tmp_path):
        out = run(tmp_path, """
const api = { post: async () => ({ ok: true }) };
const approvals = createApprovals({ api, scope: { tabId: () => 't' } });
for (let i = 0; i < 320; i += 1) approvals.add('HOME', { id: 'a' + i, prompt: 'p' });
for (let i = 0; i < 310; i += 1) await approvals.decide('a' + i, true);
approvals.add('HOME', { id: 'z', prompt: 'p' });
R.size = approvals.size(); R.open = approvals.openCount();
R.kept = [approvals.get('a315') !== null, approvals.get('z') !== null, approvals.get('a0') === null];
""", self.IMPORTS)
        assert out["size"] == 300 and out["open"] == 11
        assert out["kept"] == [True, True, True], "the ten still-pending approvals and the new one stay; the oldest decided one goes"

    def test_a_reply_typed_while_busy_resolves_the_open_approval(self, tmp_path):
        out = run(tmp_path, """
const posts = [];
const api = { post: async (p, b) => { posts.push(b); return { ok: true }; },
  stream: async (path, body, onEvent) => { onEvent({ type: 'confirmation_requested', id: 'e1', prompt: 'Send Gmail to a@b.c', tool: 'gmail_send' }); await new Promise((r) => setTimeout(r, 40)); } };
const scope = createScope({ storage: null });
const approvals = createApprovals({ api, scope });
const chat = createChat({ api, scope, approvals, threadScreens: [], storage: null });
const th = chat.thread('HOME');
const run = th.send('mail her');
await new Promise((r) => setTimeout(r, 10));
const email = approvals.pending('HOME').email;
await th.send('send it');
R.posts = posts; R.email = email; R.queued = th.queue().length;
await run;
""", self.IMPORTS)
        assert out["email"] is True and out["queued"] == 0
        assert out["posts"][0]["id"] == "e1" and out["posts"][0]["approved"] is True and out["posts"][0]["tab_id"]

    def test_a_failed_decision_keeps_the_card_pending_with_the_servers_words(self, tmp_path):
        out = run(tmp_path, """
const api = { post: async () => { throw Object.assign(new Error('no active chat'), { name: 'ApiError' }); } };
const approvals = createApprovals({ api, scope: createScope({ storage: null }) });
approvals.add('HOME', { id: 'z', prompt: 'p' });
const ok = await approvals.decide('z', true);
const e = approvals.get('z');
R.ok = ok; R.state = e.state; R.error = e.error; R.busy = e.busy;
""", self.IMPORTS)
        assert out == {"ok": False, "state": "pending", "error": "no active chat", "busy": False}

    def test_the_ledger_turn_is_restored_without_a_live_approval(self, tmp_path):
        out = run(tmp_path, """
const t = turnFromLedger({ display_text: 'q', user: 'wrapped q', final_text: 'a', elapsed_ms: 900, timestamp: '2026-09-26T10:00:00', transcript: [
  { type: 'tool_use', name: 'n', raw_arguments: '{}' }, { type: 'tool_result', name: 'n', text: 'r' }, { type: 'confirmation_requested', tool: 'rm', prompt: 'ok?' }, { type: 'assistant_text', text: 'ignored' }] });
R.text = t.text; R.reply = t.reply; R.status = t.status; R.ms = t.elapsedMs; R.approvals = t.approvals.length; R.labels = t.steps.map((s) => s.label || s.name);
""", self.IMPORTS)
        assert out["text"] == "q" and out["reply"] == "a" and out["status"] == "done" and out["ms"] == 900
        assert out["approvals"] == 0 and out["labels"][0] == "n" and out["labels"][1].startswith("APPROVAL REQUESTED rm")


class TestPrefsAndAccents:
    IMPORTS = "import { ACCENTS, pickInk, contrast, isHex, clampDim, createPrefs } from 'core/prefs.js';"

    def test_text_on_every_accent_reads_at_4_5_to_1(self, tmp_path):
        out = run(tmp_path, "R.rows = ACCENTS.map((a) => [a.id, contrast(a.hex, pickInk(a.hex))]); R.count = ACCENTS.length;", self.IMPORTS)
        assert out["count"] == 5
        for accent, ratio in out["rows"]:
            assert ratio >= 4.5, f"{accent}: ink on the accent is only {ratio:.2f}:1"

    def test_hex_and_dim_validation(self, tmp_path):
        out = run(tmp_path, "R.hex = ['#F59E0B', '#abc', 'red', '#12345g', '#123456;x'].map(isHex); R.dim = [0, 35, 80, 999, -3, 'x', 33.6].map(clampDim);", self.IMPORTS)
        assert out["hex"] == [True, False, False, False, False]
        assert out["dim"] == [0, 35, 80, 80, 0, 35, 34]

    def test_a_failed_server_save_is_reported_and_the_local_copy_stands(self, tmp_path):
        out = run(tmp_path, """
const store = new Map(); const ls = { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, v), removeItem: (k) => store.delete(k) };
const api = { post: async () => { throw new Error('server said no'); } };
const prefs = createPrefs({ storage: ls, api, root: null });
const r = await prefs.save('accent', '#60A5FA');
R.r = r; R.local = prefs.accent(); R.defaultAccent = createPrefs({ storage: { getItem: () => null, setItem() {}, removeItem() {} }, root: null }).accent();
""", self.IMPORTS)
        assert out["r"] == {"ok": False, "local": True, "error": "server said no"}
        assert out["local"] == "#60A5FA" and out["defaultAccent"] == "#F59E0B"

    def test_the_photo_wallpaper_setter_refuses_anything_but_a_data_image(self, tmp_path):
        out = run(tmp_path, """
const el = { style: {}, dataset: {} };
globalThis.document = { getElementById: () => el };
const ls = { getItem: () => null, setItem() {}, removeItem() {} };
const prefs = createPrefs({ storage: ls, root: null });
const tries = ['data:image/png;base64,AAAA', 'https://evil.example/x.png', 'data:text/html;base64,AAAA', 'data:image/png;base64,AA"),url("//evil', 'javascript:alert(1)'];
R.applied = tries.map((t) => { el.style.backgroundImage = ''; prefs.applyWall('photo', t); return el.style.backgroundImage !== ''; });
const big = 'data:image/jpeg;base64,' + 'A'.repeat(200000) + '"';
const t0 = Date.now(); prefs.applyWall('photo', big); R.ms = Date.now() - t0;
""", self.IMPORTS)
        assert out["applied"] == [True, False, False, False, False]
        assert out["ms"] < 500


class TestTimersAndScreenContext:
    IMPORTS = "import { createTimers, createScreenCtx } from 'core/ctx.js';\nimport { createEvents } from 'core/events.js';"

    def test_leaving_a_screen_clears_timers_events_keys_and_aborts_the_signal(self, tmp_path):
        out = run(tmp_path, """
class FakeES { constructor() { this.readyState = 1; FakeES.last = this; } close() {} }
const live = new Set();
const timers = createTimers({ setInterval: (fn, ms) => { const id = Symbol(); live.add(id); return id; }, clearInterval: (id) => live.delete(id), doc: { hidden: false } });
const events = createEvents({ EventSourceImpl: FakeES, setTimer: () => 0, clearTimer: () => {} });
events.start(); FakeES.last.onopen();
let keys = 0; let escs = 0; const keymap = { bind: () => { keys += 1; return () => { keys -= 1; }; }, pushEsc: () => { escs += 1; return () => { escs -= 1; }; } };
let threadSubs = 0; const thread = { key: 'HOME', subscribe: () => { threadSubs += 1; return () => { threadSubs -= 1; }; } };
let scopeSubs = 0; let overlaySubs = 0;
const deps = { events, timers, keymap, api: { withSignal: (s) => ({ signal: s }) }, chat: { thread: () => thread },
  approvals: { pending: () => null, declineAll: async () => 0, get: () => null }, scope: { tabId: () => 't', project: () => null, leaveProject() {}, onChange: () => { scopeSubs += 1; return () => { scopeSubs -= 1; }; } },
  chrome: {}, overlays: { open: () => false, onChange: () => { overlaySubs += 1; return () => { overlaySubs -= 1; }; } }, host: {}, prefs: { read: () => null, write: () => true }, toasts: { show() {} }, kit: {},
  renderApproval: (c, e) => ({ off: () => {} }) };
const h = createScreenCtx({ id: 'NEWS', root: {}, deps });
const c = h.ctx; const base = events.count();
c.events.on('a', () => {}); c.events.onResync(() => {}); c.every(1000, () => {}); c.every(2000, () => {}); c.keys.bind('r', () => {}); c.keys.pushEsc(() => {}); c.chat.subscribe(() => {}); c.scope.onChange(() => {}); c.overlays.onChange(() => {});
R.during = { events: events.count() - base, timers: timers.count(), keys, escs, threadSubs, scopeSubs, overlaySubs, aborted: c.signal.aborted };
h.dispose(); h.dispose();
R.after = { events: events.count() - base, timers: timers.count(), keys, escs, threadSubs, scopeSubs, overlaySubs, aborted: c.signal.aborted };
""", self.IMPORTS)
        assert out["during"] == {"events": 2, "timers": 2, "keys": 1, "escs": 1, "threadSubs": 1, "scopeSubs": 1, "overlaySubs": 1, "aborted": False}
        assert out["after"] == {"events": 0, "timers": 0, "keys": 0, "escs": 0, "threadSubs": 0, "scopeSubs": 0, "overlaySubs": 0, "aborted": True}

    def test_a_timer_does_not_run_while_the_tab_is_hidden_and_survives_a_throw(self, tmp_path):
        out = run(tmp_path, """
let tick; const doc = { hidden: true };
const timers = createTimers({ setInterval: (fn) => { tick = fn; return 1; }, clearInterval: () => {}, doc });
let n = 0; timers.every(10, () => { n += 1; if (n === 2) throw new Error('x'); }, 'o');
const logs = []; const orig = console.error; console.error = (e) => logs.push(e.message);
tick(); R.hidden = n; doc.hidden = false; tick(); tick(); tick(); console.error = orig;
R.n = n; R.logged = logs;
""", self.IMPORTS)
        assert out["hidden"] == 0 and out["n"] == 3 and out["logged"] == ["x"]


class TestStates:
    def test_states_module_declares_all_six_states(self):
        src = (_OS / "kit" / "states.js").read_text(encoding="utf-8")
        for name in ("loading", "populated", "empty", "unavailable", "error", "stale"):
            assert re.search(rf"\b{name}\s*[(:]", src), name

    def test_an_abort_is_not_shown_as_an_error(self, tmp_path):
        # the error() renderer returns false for an abort before it touches the DOM
        out = run(tmp_path, "const e = new Error('a'); e.name = 'AbortError'; R.r = states.error({}, e);", "import { states } from 'kit/states.js';")
        assert out["r"] is False


class TestStartupCheck:
    IMPORTS = "import { missingSignins } from 'chrome/startup-check.js';"

    def test_it_names_exactly_what_is_missing_and_the_command_to_paste(self, tmp_path):
        out = run(tmp_path, """
R.all = missingSignins({ claude: { ok: false }, codex: { ok: false } }, { configured: true, me: null }).map((i) => [i.id, i.command || i.href]);
R.claudeOnly = missingSignins({ claude: { ok: false }, codex: { ok: true } }, { configured: true, me: { email: 'a@b' } }).map((i) => i.id);
R.none = missingSignins({ claude: { ok: true }, codex: { ok: true } }, { configured: true, me: { email: 'a@b' } });
R.unreadable = [missingSignins(null, null), missingSignins({}, {}), missingSignins({ claude: {} }, { configured: false })];
""", self.IMPORTS)
        assert out["all"] == [["claude", "claude"], ["codex", "npm i -g @openai/codex && codex login"], ["google", "/api/auth/google/start"]]
        assert out["claudeOnly"] == ["claude"] and out["none"] == []
        assert out["unreadable"] == [[], [], []], "silence means no problem was reported, never a guess"
