"""Plan 1.8 (finding 204): the attention feed, deep-link arguments, and the page's side of both."""

from __future__ import annotations

import http.client
import json
import re
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path

import pytest

from dourmouse import attention
from dourmouse.attention import AttentionStore, clean_params, href_for, in_quiet_hours, safe_text
from dourmouse.deeplink import parse_deeplink


class Hub:
    def __init__(self):
        self.events = []

    def broadcast(self, event):
        self.events.append(event)


@pytest.fixture
def store(tmp_path):
    s = AttentionStore(tmp_path)
    s.hub = Hub()
    s.bind_hub(s.hub)
    return s


# ---- text rules ------------------------------------------------------------------------------------------------


def test_safe_text_hides_keys_and_control_characters_and_cuts_long_text():
    assert "sk-abcdefgh12345678" not in safe_text("key sk-abcdefgh12345678 here", 90)
    assert "[hidden]" in safe_text("password: hunter2", 90)
    assert safe_text("a\x00b\nc", 90) == "a b c"
    assert len(safe_text("word " * 100, 90)) == 90
    assert safe_text("a" * 40 + " plain", 200) == "[hidden] plain"  # a long token-like run is hidden


def test_params_are_validated_not_trimmed():
    assert clean_params({"goal": "g12"}) == {"goal": "g12"}
    for bad in ({"Goal": "x"}, {"anchor": "x"}, {"a": "x" * 121}, {"a": "x\ny"}, {f"k{i}": "v" for i in range(7)}, "no"):
        with pytest.raises(ValueError):
            clean_params(bad)


def test_href_encodes_values_and_puts_anchor_last():
    assert href_for("goals", {"goal": "g 1"}, "steps") == "#/goals?goal=g%201&anchor=steps"
    assert href_for("home") == "#/home"


# ---- store -----------------------------------------------------------------------------------------------------


def test_add_list_counts_and_order(store):
    store.add("finished", "Done", screen="goals", params={"goal": "g1"})
    store.add("needs_you", "Approve", screen="office")
    store.add("heads_up", "FYI", screen="security")
    items = store.items()
    assert [i["kind"] for i in items] == ["needs_you", "heads_up", "finished"]
    assert items[0]["href"] == "#/office"
    c = store.counts()
    assert (c["needs_you"], c["finished"], c["heads_up"], c["unread"], c["open"]) == (1, 1, 1, 3, 3)
    assert c["screens"] == {"office": 1}


@pytest.mark.parametrize("kwargs", [
    {"kind": "nope", "title": "t"}, {"kind": "finished", "title": ""}, {"kind": "finished", "title": "t", "screen": "A/b"},
    {"kind": "finished", "title": "t", "anchor": "a b"}, {"kind": "finished", "title": "t", "source": "Bad Source"},
])
def test_add_refuses_bad_input(store, kwargs):
    kind = kwargs.pop("kind")
    title = kwargs.pop("title")
    with pytest.raises(ValueError):
        store.add(kind, title, **kwargs)


def test_dedupe_returns_the_open_item_and_a_settled_one_can_come_back(store):
    a = store.add("needs_you", "Approve", dedupe_key="k1")
    b = store.add("needs_you", "Approve", dedupe_key="k1")
    assert a["id"] == b["id"] and a["new"] and not b["new"]
    assert store.resolve("k1") == 1
    assert store.items() == []
    c = store.add("needs_you", "Approve", dedupe_key="k1")
    assert c["new"] and c["id"] != a["id"]


def test_read_dismiss_and_clear_keep_needs_you(store):
    n = store.add("needs_you", "Approve")
    f = store.add("finished", "Done")
    store.add("heads_up", "FYI")
    assert store.mark_read([f["id"]]) == 1
    assert store.counts()["unread"] == 2
    assert store.clear_done() == 2
    assert [i["id"] for i in store.items()] == [n["id"]]
    assert store.dismiss(n["id"]) and not store.dismiss(n["id"])


def test_items_survive_a_restart(tmp_path):
    AttentionStore(tmp_path).add("needs_you", "Approve", screen="office", params={"job": "j1"})
    again = AttentionStore(tmp_path)
    assert again.items()[0]["params"] == {"job": "j1"}


def test_old_settled_items_are_trimmed_but_waiting_ones_are_not(tmp_path):
    now = [1_000_000.0]
    s = AttentionStore(tmp_path, clock=lambda: now[0])
    s.add("needs_you", "Waiting")
    old = s.add("finished", "Old")
    s.dismiss(old["id"])
    now[0] += 31 * 24 * 3600
    s.add("heads_up", "New")
    assert {i["title"] for i in s.items(include_done=True)} == {"Waiting", "New"}


# ---- delivery: kinds and quiet hours ------------------------------------------------------------------------------


def test_quiet_hours_overnight_and_same_day():
    prefs = {"quiet_hours": {"enabled": True, "start": "22:00", "end": "07:00"}}
    assert in_quiet_hours(prefs, datetime(2026, 1, 1, 23, 0))
    assert in_quiet_hours(prefs, datetime(2026, 1, 1, 6, 59))
    assert not in_quiet_hours(prefs, datetime(2026, 1, 1, 7, 0))
    assert not in_quiet_hours(prefs, datetime(2026, 1, 1, 12, 0))
    day = {"quiet_hours": {"enabled": True, "start": "13:00", "end": "14:00"}}
    assert in_quiet_hours(day, datetime(2026, 1, 1, 13, 30)) and not in_quiet_hours(day, datetime(2026, 1, 1, 14, 0))
    assert not in_quiet_hours({"quiet_hours": {"enabled": False, "start": "00:00", "end": "23:59"}}, datetime(2026, 1, 1, 1, 0))


def test_prefs_default_set_and_refuse(store):
    assert store.prefs()["kinds"]["heads_up"] == {"toast": True, "native": False}
    out = store.set_prefs({"kinds": {"finished": {"native": False}}, "quiet_hours": {"enabled": True, "start": "21:30"}})
    assert out["kinds"]["finished"] == {"toast": True, "native": False}
    assert out["quiet_hours"] == {"enabled": True, "start": "21:30", "end": "07:00"}
    assert store.prefs() == out
    for bad in ({"quiet_hours": {"start": "25:00"}}, {"kinds": {"loud": {"toast": True}}}, {"kinds": []}):
        with pytest.raises(ValueError):
            store.set_prefs(bad)


def test_added_event_carries_delivery_and_quiet_hours_silence_it(store):
    store.add("needs_you", "Approve", now=datetime(2026, 1, 1, 12, 0))
    ev = store.hub.events[-1]
    assert ev["type"] == "attention" and ev["toast"] is True and ev["native"] is True and ev["counts"]["needs_you"] == 1
    store.set_prefs({"quiet_hours": {"enabled": True, "start": "22:00", "end": "07:00"}})
    store.add("needs_you", "Approve two", now=datetime(2026, 1, 1, 23, 0))
    ev = store.hub.events[-1]
    assert ev["toast"] is False and ev["native"] is False
    assert len(store.items()) == 2  # silenced, still in the feed
    store.set_prefs({"quiet_hours": {"enabled": False}, "kinds": {"heads_up": {"toast": False}}})
    store.add("heads_up", "FYI", now=datetime(2026, 1, 1, 12, 0))
    assert store.hub.events[-1]["toast"] is False and store.hub.events[-1]["native"] is False


def test_native_text_never_carries_private_text_or_a_detail(store):
    store.add("needs_you", "Mail from Alice about the invoice", detail="body text", source="mail")
    assert store.hub.events[-1]["native_text"] == {"title": "Something needs you", "body": ""}
    store.add("needs_you", "Needs you: tidy downloads", detail="Send Gmail to bob: the secret plan", source="jobs")
    assert store.hub.events[-1]["native_text"] == {"title": "Needs you: tidy downloads", "body": ""}
    store.add("heads_up", "Test", detail="plain", source="test")
    assert store.hub.events[-1]["native_text"]["body"] == "plain"


def test_a_broken_hub_never_breaks_add(store):
    class Broken:
        def broadcast(self, event):
            raise RuntimeError("stream closed")

    store.bind_hub(Broken())
    assert store.add("finished", "Done")["new"]


# ---- approvals and the gate ------------------------------------------------------------------------------------------


def test_approval_flow_items(tmp_path):
    attention.set_attention_store(AttentionStore(tmp_path))
    try:
        attention.approval_requested("confirm-1")
        assert [i["title"] for i in attention.get_attention_store().items()] == ["A step needs your approval"]
        attention.approval_settled("confirm-1", answered=True, window_gone=False, approved=True)
        assert attention.get_attention_store().items() == []
        attention.approval_requested("confirm-2")
        attention.approval_settled("confirm-2", answered=False, window_gone=True, approved=False)
        items = attention.get_attention_store().items()
        assert [i["title"] for i in items] == ["Declined because the window closed"] and items[0]["kind"] == "needs_you"
        attention.approval_requested("confirm-3")
        attention.approval_settled("confirm-3", answered=False, window_gone=False, approved=False)
        assert {i["title"] for i in attention.get_attention_store().items()} == {"Declined because the window closed", "A step timed out waiting for you"}
    finally:
        attention.set_attention_store(None)


def test_gate_raises_and_settles_items(tmp_path, monkeypatch):
    from dourmouse.webui import WebConfirmationGate

    attention.set_attention_store(AttentionStore(tmp_path))
    store = attention.get_attention_store()
    try:
        gate = WebConfirmationGate(lambda e: None)
        gate.set_should_stop(lambda: True)  # the window is gone
        monkeypatch.setattr("dourmouse.config.auto_approve_enabled", lambda: False)
        assert gate("Send Gmail to bob: secret body") is False
        titles = [i["title"] for i in store.items()]
        assert titles == ["Declined because the window closed"]
        assert all("secret" not in json.dumps(i) for i in store.items())
        # answered normally: nothing is left behind
        store.dismiss(store.items()[0]["id"])
        gate = WebConfirmationGate(lambda e: threading.Timer(0.05, lambda: gate.resolve("confirm-1", True)).start())
        monkeypatch.setattr("dourmouse.config.auto_approve_enabled", lambda: False)
        assert gate("Do a thing") is True
        assert store.items() == []
    finally:
        attention.set_attention_store(None)


def test_job_events_reach_the_feed(tmp_path):
    from dourmouse.goal_runtime import GoalRuntime

    attention.set_attention_store(AttentionStore(tmp_path))
    try:
        rt = GoalRuntime.__new__(GoalRuntime)
        rt._bus = None
        rt._state_store = None
        rt._events_broadcast = Hub()
        rt._notify("g1", "Needs you: tidy", "waiting", kind="needs_you")
        rt._notify("g1", "Needs you: tidy", "waiting", kind="needs_you")  # no second copy
        item = attention.get_attention_store().items()[0]
        assert item["screen"] == "goals" and item["params"] == {"goal": "g1"} and len(attention.get_attention_store().items()) == 1
        rt._notify("g1", "Done: tidy", "all done", kind="finished")
        kinds = [i["kind"] for i in attention.get_attention_store().items()]
        assert kinds == ["finished"]  # finishing settled the waiting item
    finally:
        attention.set_attention_store(None)


# ---- deep links -------------------------------------------------------------------------------------------------


def test_deeplink_keeps_arguments_and_refuses_bad_ones():
    ok = parse_deeplink("dourmouse://office?goal=g12&anchor=steps")
    assert ok["ok"] and ok["href"] == "#/office?goal=g12&anchor=steps" and ok["params"] == {"goal": "g12", "anchor": "steps"}
    assert parse_deeplink("projects/p1/t2?x=1")["href"] == "#/projects/p1/t2?x=1"
    for bad in ("office?goal=<x>", "office?Goal=1", "office?goal", "office?a=1&a=2", "office?" + "&".join(f"k{i}=1" for i in range(7)),
                "office?goal=%00", "nowhere?x=1", "office?goal=" + "x" * 81):
        assert not parse_deeplink(bad)["ok"], bad


# ---- routes ----------------------------------------------------------------------------------------------------------


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("DOURMOUSE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("DOURMOUSE_CONFIG_DIR", str(tmp_path / "cfg"))
    from dourmouse.general_roster import build_general_registry
    from dourmouse.webui import run_server

    attention.set_attention_store(None)
    srv = run_server(build_general_registry(), port=0, client=None, config=None)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    t.join(timeout=2)
    attention.set_attention_store(None)


def call(srv, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    conn.request(method, path, body=json.dumps(body) if body is not None else None)
    resp = conn.getresponse()
    data = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, data


def test_routes_end_to_end(server):
    st, d = call(server, "GET", "/api/os/attention")
    assert st == 200 and d["items"] == [] and d["prefs"]["quiet_hours"]["enabled"] is False
    st, d = call(server, "POST", "/api/os/attention/test")
    assert st == 200 and d["item"]["title"] == "This is a test notification" and d["delivery"]["toast"] is True
    st, d = call(server, "GET", "/api/os/attention")
    assert len(d["items"]) == 1 and d["counts"]["heads_up"] == 1 and d["counts"]["unread"] == 1
    item_id = d["items"][0]["id"]
    assert call(server, "POST", "/api/os/attention/read", {"ids": [item_id]})[1]["counts"]["unread"] == 0
    assert call(server, "POST", "/api/os/attention/read", {"ids": ["x"]})[0] == 400
    assert call(server, "POST", "/api/os/attention/dismiss", {"id": "1"})[0] == 400
    assert call(server, "POST", "/api/os/attention/dismiss", {"id": item_id})[0] == 200
    assert call(server, "POST", "/api/os/attention/dismiss", {"id": item_id})[0] == 404
    st, d = call(server, "POST", "/api/os/attention/prefs", {"quiet_hours": {"enabled": True, "start": "20:00", "end": "06:00"}})
    assert st == 200 and d["prefs"]["quiet_hours"]["start"] == "20:00"
    assert call(server, "POST", "/api/os/attention/prefs", {"quiet_hours": {"start": "99:99"}})[0] == 400
    assert call(server, "POST", "/api/os/attention/clear")[1]["cleared"] == 0


def test_the_test_route_goes_through_the_event_hub(server):
    seen = []
    hub = server.events_broadcast
    original = hub.broadcast
    hub.broadcast = lambda e: (seen.append(e), original(e))[1]
    call(server, "POST", "/api/os/attention/test")
    assert [e["type"] for e in seen if e.get("type") == "attention"] == ["attention"]


def test_server_start_binds_the_hub_so_runtimes_reach_windows(server):
    attention.notify("finished", "Done: something", screen="goals", params={"goal": "g9"})
    assert attention.get_attention_store()._hub is server.events_broadcast


# ---- the page: router arguments, ctx.params, the client -------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parents[2]
_OS = _ROOT / "ui" / "assets" / "os"
_NODE = shutil.which("node") or ""
needs_node = pytest.mark.skipif(not _NODE, reason="node not on PATH in this environment")


def run_js(tmp_path, body, imports):
    header = re.sub(r"from '((?:core|kit|chrome)/[^']+)'", lambda m: f"from '{(_OS / m.group(1)).as_uri()}'", imports)
    script = tmp_path / "h.mjs"
    script.write_text(header + "\nconst R = {};\n" + body + "\nconsole.log(JSON.stringify(R));\n", encoding="utf-8")
    proc = subprocess.run([_NODE, str(script)], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    return json.loads(proc.stdout.strip().splitlines()[-1])


@needs_node
def test_router_carries_arguments_and_anchor(tmp_path):
    r = run_js(tmp_path, """
const a = resolveRoute('#/goals?goal=g%201&anchor=steps');
R.a = [a.id, a.params];
R.b = resolveRoute('#/projects/p1/t2').params;
R.c = resolveRoute('#/office?bad key=1&goal=ok&goal=twice&anchor=a%20b').params;
R.d = resolveRoute('#/nowhere?goal=1').params;
R.e = resolveRoute('#/goals?goal=%E0%A4%A').params;
R.f = resolveRoute('#/goals?' + Array.from({length: 9}, (_, i) => 'k' + i + '=1').join('&')).params.query;
R.g = [hashFor('office', { goal: 'g 1', b: '2' }, 'steps'), hashFor('home')];
R.h = [NAV_HREF.test('#/goals?goal=g%201&anchor=steps'), NAV_HREF.test('#/x"><script>'), NAV_HREF.test('javascript:alert(1)')];
R.same = [sameParams(a.params, a.params), sameParams(a.params, resolveRoute('#/goals?goal=g2').params)];
""", "import { resolveRoute, hashFor, NAV_HREF, sameParams } from 'core/router.js';")
    assert r["a"] == ["GOALS", {"path": [], "query": {"goal": "g 1"}, "anchor": "steps"}]
    assert r["b"] == {"path": ["p1", "t2"], "query": {}, "anchor": ""}
    assert r["c"] == {"path": [], "query": {"goal": "ok"}, "anchor": ""}
    assert r["d"] == {"path": [], "query": {}, "anchor": ""}
    assert r["e"] == {"path": [], "query": {}, "anchor": ""}
    assert len(r["f"]) == 6
    assert r["g"] == ["#/office?b=2&goal=g%201&anchor=steps", "#/home"]
    assert r["h"] == [True, False, False]
    assert r["same"] == [True, False]


@needs_node
def test_ctx_params_get_onchange_reveal_and_dispose(tmp_path):
    r = run_js(tmp_path, """
const noop = () => () => {};
const events = { scope: () => ({ on: noop, onResync: noop, onStatus: noop, dispose() {} }) };
const deps = { events, api: { withSignal: () => ({}) }, chat: { thread: () => ({ key: 'k' }) }, timers: { clearOwner() {} } };
let scrolled = 0;
const el = { scrollIntoView() { scrolled += 1; }, tabIndex: -1 };
const root = { querySelector: (q) => (q === '[data-anchor="steps"]' ? el : null) };
const handle = createScreenCtx({ id: 'GOALS', root, deps, params: { path: ['p'], query: { goal: 'g1' }, anchor: 'steps' } });
const p = handle.ctx.params;
R.start = [p.get('goal'), p.get('none', 'dflt'), p.path, p.anchor, p.reveal(), scrolled];
const seen = [];
p.onChange((x) => seen.push(x.get('goal')));
handle.setParams({ path: [], query: { goal: 'g2' }, anchor: 'missing' });
R.after = [p.get('goal'), seen, p.reveal(), scrolled];
handle.dispose();
handle.setParams({ path: [], query: { goal: 'g3' }, anchor: '' });
R.disposed = [p.get('goal'), seen];
""", "import { createScreenCtx } from 'core/ctx.js';")
    assert r["start"] == ["g1", "dflt", ["p"], "steps", True, 1]
    assert r["after"] == ["g2", ["g2"], False, 1]
    assert r["disposed"] == ["g2", ["g2"]]


@needs_node
def test_attention_client_toasts_with_go_only_when_told_and_navigates_safely(tmp_path):
    r = run_js(tmp_path, """
const handlers = [];
const events = { on: (t, fn) => handlers.push([t, fn]), onResync: () => {} };
const posts = [];
const api = {
  get: async () => ({ items: [], counts: { needs_you: 1, screens: { office: 1 } }, prefs: {} }),
  post: async (path, body) => { posts.push([path, body]); return { ok: true, counts: { needs_you: 0, screens: {} } }; },
};
const shown = [];
const toasts = { show: (t) => shown.push(t) };
const went = [];
const a = createAttention({ api, events, toasts, go: (h) => went.push(h) });
const fire = handlers.find(([t]) => t === 'attention')[1];
const item = { id: 7, kind: 'needs_you', title: 'Approve', detail: 'd', href: '#/goals?goal=g1', read: false };
fire({ type: 'attention', action: 'added', item, toast: false });
R.silent = shown.length;
fire({ type: 'attention', action: 'added', item, toast: true });
R.toast = [shown.length, shown[0].level, shown[0].title, shown[0].action.label];
shown[0].action.onClick();
await new Promise((r) => setTimeout(r, 10));
R.went = went;
R.posts = posts;
R.bad = [a.open({ id: 8, href: 'javascript:alert(1)' }), a.open({ id: 9, href: '#/x"><b>' }), went.length];
R.counts = a.state.counts;
""", "import { createAttention } from 'core/attention.js';")
    assert r["silent"] == 0
    assert r["toast"] == [1, "warn", "Approve", "Go"]
    assert r["went"] == ["#/goals?goal=g1"]
    assert r["posts"] == [["/api/os/attention/read", {"ids": [7]}]]
    assert r["bad"] == [False, False, 1]


@needs_node
def test_electron_main_checks_the_place_and_never_does_more_than_navigate():
    src = (_ROOT / "electron" / "main.js").read_text(encoding="utf-8")
    assert "handleAttentionEvent(payload)" in src and "_NOTIFY_HREF.test(href)" in src
    block = src[src.index("function openFromNotification"):src.index("function setDockBadge")]
    assert "location.hash" in block and "fetch" not in block and "confirm" not in block.lower()
    # a window the owner is looking at does not also pop a banner
    assert "isVisible() && mainWindow.isFocused()" in src
    # alerts a job raised are notified once, by the attention feed
    assert 'alert.link.startsWith("#/goal/")' in src
