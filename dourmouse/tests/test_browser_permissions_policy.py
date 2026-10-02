"""Phase B2: the pure decisions in electron/permissions.js behind the pane's per-site
permission prompts, exercised under plain node (the same pattern as
test_browser_tabs_policy.py). The wiring that uses them is test_browser_permissions_shell.py."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ELECTRON = Path(__file__).resolve().parents[2] / "electron"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def run(tmp_path, body):
    script = tmp_path / "p.js"
    script.write_text(f"const p = require({str(ELECTRON / 'permissions.js')!r});\nconst R = {{}};\n{body}\nconsole.log(JSON.stringify(R));\n", encoding="utf-8")
    proc = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_only_six_permissions_can_ever_be_asked_about_and_the_rest_are_refused(tmp_path):
    out = run(tmp_path, """
const names = ['geolocation', 'notifications', 'clipboard-read', 'fullscreen', 'clipboard-sanitized-write',
  'display-capture', 'midi', 'midiSysex', 'openExternal', 'pointerLock', 'hid', 'serial', 'usb', 'window-management',
  'idle-detection', 'storage-access', 'top-level-storage-access', 'speaker-selection', 'unknown', '', 'MEDIA'];
R.cls = Object.fromEntries(names.map((n) => [n, p.classifyRequest(n, {})]));
R.sitePermissions = p.SITE_PERMISSIONS;
""")
    assert out["sitePermissions"] == ["camera", "microphone", "geolocation", "notifications", "clipboard-read", "fullscreen"]
    for ask in ("geolocation", "notifications", "clipboard-read", "fullscreen"):
        assert out["cls"][ask] == {"action": "ask", "keys": [ask]}
    assert out["cls"]["clipboard-sanitized-write"] == {"action": "allow", "keys": []}
    for never in ("display-capture", "midi", "midiSysex", "openExternal", "pointerLock", "hid", "serial", "usb", "window-management",
                  "idle-detection", "storage-access", "top-level-storage-access", "speaker-selection", "unknown", "", "MEDIA"):
        assert out["cls"][never] == {"action": "deny", "keys": []}, never


def test_a_media_request_maps_to_microphone_and_camera_and_an_empty_list_is_screen_sharing(tmp_path):
    out = run(tmp_path, """
R.both = p.classifyRequest('media', { mediaTypes: ['audio', 'video'] });
R.audio = p.classifyRequest('media', { mediaTypes: ['audio'] });
R.video = p.classifyRequest('media', { mediaTypes: ['video'] });
R.none = p.classifyRequest('media', {});
R.empty = p.classifyRequest('media', { mediaTypes: [] });
R.screen = p.classifyRequest('media', { mediaTypes: ['screen'] });
R.mixed = p.classifyRequest('media', { mediaTypes: ['screen', 'audio'] });
R.dup = p.classifyRequest('media', { mediaTypes: ['audio', 'audio'] });
R.checkAudio = p.classifyCheck('media', { mediaType: 'audio' });
R.checkVideo = p.classifyCheck('media', { mediaType: 'video' });
R.checkUnknown = p.classifyCheck('media', { mediaType: 'unknown' });
R.checkNone = p.classifyCheck('media', undefined);
R.checkGeo = p.classifyCheck('geolocation', {});
R.checkMidi = p.classifyCheck('midi', {});
""")
    assert out["both"] == {"action": "ask", "keys": ["microphone", "camera"]}
    assert out["audio"]["keys"] == ["microphone"] and out["video"]["keys"] == ["camera"]
    # getDisplayMedia arrives as a media request with an EMPTY mediaTypes list (seen live), so
    # an empty or missing list is refused, never read as "camera and microphone"
    assert out["none"] == {"action": "deny", "keys": []} and out["empty"] == {"action": "deny", "keys": []}
    assert out["screen"] == {"action": "deny", "keys": []}  # screen sharing is never offered
    assert out["mixed"]["keys"] == ["microphone"]
    assert out["dup"]["keys"] == ["microphone"]
    assert out["checkAudio"]["keys"] == ["microphone"] and out["checkVideo"]["keys"] == ["camera"]
    assert out["checkUnknown"]["keys"] == ["microphone", "camera"] and out["checkNone"]["keys"] == ["microphone", "camera"]
    assert out["checkGeo"]["keys"] == ["geolocation"] and out["checkMidi"]["action"] == "deny"


def test_an_origin_is_scheme_host_port_and_only_for_real_web_pages(tmp_path):
    out = run(tmp_path, """
R.good = ['https://Meet.Example/a?b#c', 'http://127.0.0.1:8080/x', 'https://user:pw@a.example:444/p'].map(p.originOf);
R.bad = ['about:blank', 'data:text/html,x', 'file:///etc/passwd', 'javascript:alert(1)', 'chrome://gpu', 'not a url', '', null, undefined, 42].map(p.originOf);
R.diff = [p.originOf('https://a.example') === p.originOf('http://a.example'), p.originOf('https://a.example') === p.originOf('https://a.example:8443'),
          p.originOf('https://a.example') === p.originOf('https://sub.a.example')];
""")
    assert out["good"] == ["https://meet.example", "http://127.0.0.1:8080", "https://a.example:444"]
    assert out["bad"] == [""] * 10
    assert out["diff"] == [False, False, False]


def test_decisions_are_stored_per_origin_and_permission_and_never_mutate_their_input(tmp_path):
    out = run(tmp_path, """
const O = 'https://meet.example';
const a = {};
const b = p.setDecision(a, O, 'camera', 'allow', 111);
R.aUntouched = JSON.stringify(a);
const c = p.setDecision(b.sites, O, 'microphone', 'block', 222);
R.get = [p.getDecision(c.sites, O, 'camera'), p.getDecision(c.sites, O, 'microphone'), p.getDecision(c.sites, O, 'geolocation'),
         p.getDecision(c.sites, 'https://other.example', 'camera'), p.getDecision(c.sites, 'https://MEET.example', 'camera')];
R.bScamera = JSON.stringify(b.sites);
R.bad = [p.setDecision({}, 'https://a.example/path', 'camera', 'allow').ok, p.setDecision({}, 'about:blank', 'camera', 'allow').ok,
         p.setDecision({}, O, 'display-capture', 'allow').ok, p.setDecision({}, O, 'midi', 'allow').ok, p.setDecision({}, O, 'openExternal', 'allow').ok,
         p.setDecision({}, O, 'camera', 'ask').ok, p.setDecision({}, O, 'camera', 'granted').ok, p.setDecision({}, O, 'camera', undefined).ok];
R.list = p.listSites(c.sites);
R.count = p.countSites(c.sites);
R.proto = [p.getDecision({}, '__proto__', 'camera'), p.getDecision({}, 'constructor', 'toString')];
""")
    assert out["aUntouched"] == "{}"
    # a lookup is exact: callers always pass originOf(url), which folds the host to lower case
    assert out["get"] == ["allow", "block", None, None, None]
    assert out["bad"] == [False] * 8
    assert [(r["origin"], r["permission"], r["decision"]) for r in out["list"]] == [
        ("https://meet.example", "camera", "allow"), ("https://meet.example", "microphone", "block")]
    assert out["count"] == {"origins": 1, "decisions": 2}
    assert out["proto"] == [None, None]


def test_the_number_of_remembered_sites_is_bounded(tmp_path):
    out = run(tmp_path, """
let s = {};
for (let i = 0; i < p.MAX_SITES + 20; i += 1) { const r = p.setDecision(s, 'https://s' + i + '.example', 'camera', 'allow'); s = r.sites; }
R.sites = Object.keys(s).length; R.max = p.MAX_SITES;
const again = p.setDecision(s, 'https://s0.example', 'microphone', 'allow');
R.knownSiteStillWorks = again.ok;
""")
    assert out["sites"] == out["max"] and out["knownSiteStillWorks"] is True


def test_revoking_one_permission_or_a_whole_site(tmp_path):
    out = run(tmp_path, """
const O = 'https://a.example';
let s = p.setDecision({}, O, 'camera', 'allow').sites;
s = p.setDecision(s, O, 'geolocation', 'block').sites;
s = p.setDecision(s, 'https://b.example', 'camera', 'allow').sites;
const one = p.clearDecision(s, O, 'camera');
R.one = [one.removed, Object.keys(one.sites[O])];
const missing = p.clearDecision(s, O, 'microphone');
R.missing = missing.removed;
const all = p.clearDecision(s, O);
R.all = [all.removed, Object.keys(all.sites)];
const last = p.clearDecision(p.setDecision({}, O, 'camera', 'allow').sites, O, 'camera');
R.lastLeavesNoEmptySite = Object.keys(last.sites);
R.unknownSite = p.clearDecision(s, 'https://nope.example').removed;
R.untouched = Object.keys(s).length;
""")
    assert out["one"] == [1, ["geolocation"]]
    assert out["missing"] == 0
    assert out["all"] == [2, ["https://b.example"]]
    assert out["lastLeavesNoEmptySite"] == []
    assert out["unknownSite"] == 0 and out["untouched"] == 2


def test_a_hand_edited_file_keeps_only_what_this_code_could_have_written(tmp_path):
    out = run(tmp_path, """
R.clean = p.sanitizeSites({
  'https://ok.example': { camera: { d: 'allow', at: 5 }, midi: { d: 'allow', at: 1 }, microphone: { d: 'maybe' }, geolocation: 'allow', fullscreen: { d: 'block' } },
  'https://ok.example/with/path': { camera: { d: 'allow' } },
  'javascript:alert(1)': { camera: { d: 'allow' } },
  'about:blank': { camera: { d: 'allow' } },
  'https://empty.example': {},
  'https://str.example': 'allow',
});
R.junk = [p.sanitizeSites(null), p.sanitizeSites([]), p.sanitizeSites('x'), p.sanitizeSites(7)];
""")
    assert out["clean"] == {"https://ok.example": {"camera": {"d": "allow", "at": 5}, "fullscreen": {"d": "block", "at": 0}}}
    assert out["junk"] == [{}, {}, {}, {}]


def test_allow_this_time_ends_with_the_tab_or_when_the_tab_leaves_the_origin(tmp_path):
    out = run(tmp_path, """
const g = p.createTempGrants();
g.add(7, 'https://a.example', ['camera', 'microphone']);
R.has = [g.has(7, 'https://a.example', 'camera'), g.has(7, 'https://a.example', 'microphone'), g.has(8, 'https://a.example', 'camera'),
         g.has(7, 'https://b.example', 'camera'), g.has(7, 'https://a.example', 'geolocation')];
g.dropForeign(7, 'https://a.example');
R.sameOrigin = g.has(7, 'https://a.example', 'camera');
g.dropForeign(7, 'https://b.example');
R.leftOrigin = g.has(7, 'https://a.example', 'camera');
g.add(7, 'https://a.example', ['camera']); g.add(17, 'https://a.example', ['camera']);
g.dropTab(7);
R.afterClose = [g.has(7, 'https://a.example', 'camera'), g.has(17, 'https://a.example', 'camera')];
""")
    assert out["has"] == [True, True, False, False, False]
    assert out["sameOrigin"] is True and out["leftOrigin"] is False
    assert out["afterClose"] == [False, True]  # tab 17 is not tab 7


def test_the_prompt_queue_collects_waiters_and_answers_each_exactly_once(tmp_path):
    out = run(tmp_path, """
let t = 1000;
const q = p.createPromptQueue({ ttlMs: 5000, now: () => t });
const got = [];
const w = (name) => (d) => got.push(name + ':' + d);
const r1 = q.request(1, 'https://a.example', ['camera'], w('a1'));
const r2 = q.request(1, 'https://a.example', ['camera'], w('a2'));       // the same question: one prompt, two waiters
const r3 = q.request(1, 'https://a.example', ['microphone'], w('m'));
R.requests = [r1.ok, r1.merged, r2.ok, r2.merged, r3.ok, r3.merged, q.size()];
R.view = q.forTab(1);
R.noWaiterInView = JSON.stringify(q.forTab(1)).includes('waiters');
const first = q.answer(r1.id, 'allow');
R.answered = [first.origin, first.keys, got.slice()];
R.again = q.answer(r1.id, 'allow');
R.sizeAfter = q.size();
R.next = q.forTab(1).keys;
R.unknownId = q.answer('nope', 'allow');
q.answer(r3.id, 'block');
R.final = got.slice();
""")
    assert out["requests"] == [True, False, True, True, True, False, 2]
    assert out["view"]["origin"] == "https://a.example" and out["view"]["keys"] == ["camera"]
    assert out["noWaiterInView"] is False
    assert out["answered"] == ["https://a.example", ["camera"], ["a1:allow", "a2:allow"]]
    assert out["again"] is None and out["sizeAfter"] == 1 and out["next"] == ["microphone"] and out["unknownId"] is None
    assert out["final"] == ["a1:allow", "a2:allow", "m:block"]


def test_a_prompt_nobody_answers_is_dismissed_and_so_is_one_for_a_page_that_moved_on(tmp_path):
    out = run(tmp_path, """
let t = 0;
const q = p.createPromptQueue({ ttlMs: 5000, now: () => t });
const got = [];
const w = (name) => (d) => got.push(name + ':' + d);
q.request(1, 'https://a.example', ['camera'], w('old'));
t = 4999; R.early = q.expire();
q.request(2, 'https://b.example', ['geolocation'], w('b'));
t = 5000; R.expired = q.expire();
q.request(3, 'https://c.example', ['fullscreen'], w('c'));
R.moved = q.dropForeign(3, 'https://c.example');          // still on c: nothing to drop
R.movedAway = q.dropForeign(3, 'https://other.example');  // left c
q.request(4, 'https://d.example', ['camera'], w('d'));
q.request(4, 'https://d.example', ['microphone'], w('d2'));
R.closed = q.dropTab(4);
q.request(5, 'https://e.example', ['camera'], w('e'));
R.all = q.dropAll();
R.got = got;
R.size = q.size();
""")
    assert out["early"] == 0 and out["expired"] == 1  # only the first one is old enough at t=5000
    assert out["moved"] == 0 and out["movedAway"] == 1 and out["closed"] == 2 and out["all"] == 2
    # "b" was asked at t=4999 and was still waiting when dropAll ran, so it is dismissed last
    assert sorted(out["got"]) == sorted(["old:dismiss", "c:dismiss", "d:dismiss", "d2:dismiss", "b:dismiss", "e:dismiss"])
    assert out["size"] == 0


def test_the_queue_is_bounded_per_tab_and_overall_so_a_page_cannot_pile_up_prompts(tmp_path):
    out = run(tmp_path, """
const q = p.createPromptQueue({ max: 5, maxPerTab: 2 });
const r = [];
r.push(q.request(1, 'https://a.example', ['camera'], () => {}).ok);
r.push(q.request(1, 'https://a.example', ['microphone'], () => {}).ok);
r.push(q.request(1, 'https://a.example', ['geolocation'], () => {}).ok);   // third distinct one on tab 1
r.push(q.request(2, 'https://b.example', ['camera'], () => {}).ok);
r.push(q.request(3, 'https://c.example', ['camera'], () => {}).ok);
r.push(q.request(4, 'https://d.example', ['camera'], () => {}).ok);       // fifth in total
r.push(q.request(5, 'https://e.example', ['camera'], () => {}).ok);       // over the overall limit
R.r = r; R.size = q.size();
""")
    assert out["r"] == [True, True, False, True, True, True, False]
    assert out["size"] == 5


def test_a_waiter_that_throws_does_not_strand_the_others(tmp_path):
    out = run(tmp_path, """
const q = p.createPromptQueue();
const got = [];
const a = q.request(1, 'https://a.example', ['camera'], () => { throw new Error('boom'); });
q.request(1, 'https://a.example', ['camera'], (d) => got.push(d));
q.answer(a.id, 'once');
R.got = got;
""")
    assert out["got"] == ["once"]


def test_the_bar_says_what_the_site_wants_in_plain_words(tmp_path):
    out = run(tmp_path, """
R.s = [p.promptSentence('https://meet.example', ['camera']),
       p.promptSentence('https://meet.example', ['camera', 'microphone']),
       p.promptSentence('https://meet.example', ['microphone', 'camera']),
       p.promptSentence('https://maps.example:8443', ['geolocation']),
       p.promptSentence('https://a.example', ['notifications', 'fullscreen']),
       p.promptSentence('https://a.example', ['clipboard-read']),
       p.promptSentence('https://a.example', []),
       p.promptSentence('not a url', ['camera'])];
""")
    assert out["s"] == [
        "meet.example wants to use your camera",
        "meet.example wants to use your camera and microphone",
        "meet.example wants to use your microphone and camera",
        "maps.example:8443 wants to know your location",
        "a.example wants to show notifications and go full screen",
        "a.example wants to see text and images you copied",
        "a.example wants a permission",
        "not a url wants to use your camera",
    ]


def test_permissions_js_has_no_electron_import_and_cannot_grant_by_itself():
    src = (ELECTRON / "permissions.js").read_text(encoding="utf-8")
    assert "require(" not in src  # pure: no electron, no fs, no network
    assert "—" not in src
