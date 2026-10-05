"""Phase F2: the shell's "Model is driving <App>" strip (chrome/driving-strip.js).

Pure view logic and the whole strip against a hand-made DOM, in node. The strip
reaches a real browser in EVIDENCE/169_f2_*.png.
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
_STRIP = _OS / "chrome" / "driving-strip.js"
_NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(_NODE is None, reason="node not on PATH in this environment")

PRELUDE = """
import { stripView, eventFromStatus, createDrivingStrip } from '__STRIP__';
const R = {};
function fakeEl() {
  return {
    hidden: false, id: '', attrs: {}, children: [], listener: null,
    setAttribute(k, v) { this.attrs[k] = v; },
    after() {},
    replaceChildren(...c) { this.children = c; },
    addEventListener(type, fn) { if (type === 'click') this.listener = fn; },
    get text() { return this.children.map((c) => c.html || '').join(''); },
  };
}
globalThis.document = {
  createElement(tag) {
    if (tag === 'template') return { set innerHTML(v) { this.html = v; }, get content() { return { html: this.html }; } };
    return fakeEl();
  },
};
function make({ postImpl, getImpl } = {}) {
  const handlers = {};
  const toasts = [];
  const posts = [];
  const body = { dataset: {} };
  const doc = { createElement: globalThis.document.createElement, body };
  const api = {
    get: getImpl || (async () => ({ indicator: {}, kill: { engaged: false } })),
    post: async (path, payload) => { posts.push([path, payload]); if (postImpl) return postImpl(path, payload); return { ok: true }; },
  };
  const events = { on(type, fn) { handlers[type] = fn; }, onResync(fn) { handlers.resync = fn; } };
  const strip = createDrivingStrip({ after: { after() {} }, api, events, toasts: { show: (t) => toasts.push(t) }, doc });
  return { strip, handlers, toasts, posts, body };
}
""".replace("__STRIP__", _STRIP.as_uri())


def node(tmp_path, body):
    script = tmp_path / "t.mjs"
    script.write_text(PRELUDE + body + "\nconsole.log(JSON.stringify(R));\n", encoding="utf-8")
    proc = subprocess.run([_NODE, str(script)], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestStripView:
    def test_it_shows_only_while_driving_with_an_app_and_not_killed(self, tmp_path):
        out = node(tmp_path, """
R.on = stripView({ driving: true, app: 'TextEdit', active: true, action: 'click' });
R.lingering = stripView({ driving: true, app: 'TextEdit', active: false, action: 'click' });
R.off = stripView({ driving: false, app: 'TextEdit' });
R.killed = stripView({ driving: true, killed: true, app: 'TextEdit' });
R.noapp = stripView({ driving: true, app: '  ' });
R.junk = [stripView(null), stripView(undefined), stripView('x'), stripView({ app: 7, driving: true })];
""")
        assert out["on"] == {"show": True, "text": "Model is driving TextEdit", "detail": "clicking"}
        assert out["lingering"]["show"] is True and out["lingering"]["detail"] == ""
        assert out["off"]["show"] is False and out["killed"]["show"] is False and out["noapp"]["show"] is False
        assert all(j["show"] is False for j in out["junk"])

    def test_the_status_payload_becomes_an_event(self, tmp_path):
        out = node(tmp_path, """
R.live = eventFromStatus({ indicator: { driving: true, app: 'Notes', active: false }, kill: { engaged: false } });
R.killed = eventFromStatus({ indicator: { driving: true, app: 'Notes' }, kill: { engaged: true } });
R.junk = eventFromStatus(null);
""")
        assert out["live"]["driving"] is True and out["live"]["app"] == "Notes"
        assert out["killed"]["driving"] is False and out["killed"]["killed"] is True
        assert out["junk"]["driving"] is False


class TestTheStrip:
    def test_an_event_shows_the_strip_with_stop_and_the_body_flag(self, tmp_path):
        out = node(tmp_path, """
const t = make();
R.start = { hidden: t.strip.el.hidden };
t.strip.apply({ driving: false });
R.hiddenAfterOff = t.strip.el.hidden; R.flagOff = t.body.dataset.driving;
t.handlers.app_driver_indicator({ driving: true, app: 'TextEdit', active: true, action: 'type' });
R.hidden = t.strip.el.hidden; R.flag = t.body.dataset.driving; R.html = t.strip.el.text; R.role = t.strip.el.attrs.role; R.id = t.strip.el.id;
""")
        assert out["hiddenAfterOff"] is True and out["flagOff"] == "false"
        assert out["hidden"] is False and out["flag"] == "true" and out["role"] == "status" and out["id"] == "drivingstrip"
        assert "Model is driving TextEdit" in out["html"] and "typing" in out["html"] and ">STOP<" in out["html"]

    def test_an_app_name_is_escaped(self, tmp_path):
        out = node(tmp_path, """
const t = make();
t.handlers.app_driver_indicator({ driving: true, app: '<img src=x onerror=alert(1)>', active: true });
R.html = t.strip.el.text;
""")
        assert "<img" not in out["html"] and "&lt;img" in out["html"]

    def test_a_not_driving_event_hides_it_again(self, tmp_path):
        out = node(tmp_path, """
const t = make();
t.handlers.app_driver_indicator({ driving: true, app: 'Notes', active: false });
t.handlers.app_driver_indicator({ driving: false, killed: false });
R.hidden = t.strip.el.hidden; R.flag = t.body.dataset.driving; R.html = t.strip.el.text;
""")
        assert out["hidden"] is True and out["flag"] == "false" and out["html"] == ""

    def test_stop_posts_to_the_kill_route_and_hides_the_strip(self, tmp_path):
        out = node(tmp_path, """
const t = make();
t.handlers.app_driver_indicator({ driving: true, app: 'Notes', active: false });
await t.strip.el.listener({ target: { closest: (sel) => (sel === '.ds-stop' ? {} : null) } });
await new Promise((r) => setTimeout(r, 10));
R.posts = t.posts; R.hidden = t.strip.el.hidden; R.toasts = t.toasts;
""")
        assert out["posts"][0][0] == "/api/os/apps/kill" and "reason" in out["posts"][0][1]
        assert out["hidden"] is True
        assert out["toasts"][0]["level"] == "ok" and "APPS" in out["toasts"][0]["detail"]

    def test_a_failed_stop_keeps_the_strip_and_says_why(self, tmp_path):
        out = node(tmp_path, """
const t = make({ postImpl: async () => { throw new Error('Cannot reach the Dourmouse server: down'); } });
t.handlers.app_driver_indicator({ driving: true, app: 'Notes', active: false });
await t.strip.el.listener({ target: { closest: (sel) => (sel === '.ds-stop' ? {} : null) } });
await new Promise((r) => setTimeout(r, 10));
R.hidden = t.strip.el.hidden; R.toasts = t.toasts; R.html = t.strip.el.text;
""")
        assert out["hidden"] is False and "STOP<" in out["html"] and "disabled" not in out["html"]
        assert out["toasts"][0]["level"] == "error" and "down" in out["toasts"][0]["detail"]

    def test_a_click_elsewhere_in_the_strip_does_nothing(self, tmp_path):
        out = node(tmp_path, """
const t = make();
t.handlers.app_driver_indicator({ driving: true, app: 'Notes', active: false });
await t.strip.el.listener({ target: { closest: () => null } });
R.posts = t.posts;
""")
        assert out["posts"] == []

    def test_read_and_resync_take_the_servers_word_for_it(self, tmp_path):
        out = node(tmp_path, """
const t = make({ getImpl: async () => ({ indicator: { driving: true, app: 'Mail', active: false }, kill: { engaged: false } }) });
await t.strip.read();
R.afterRead = t.strip.el.hidden;
t.strip.apply({ driving: false });
await t.handlers.resync();
R.afterResync = t.strip.el.hidden;
const bad = make({ getImpl: async () => { throw new Error('offline'); } });
await bad.strip.read();
R.afterFailedRead = bad.strip.el.hidden;
""")
        assert out["afterRead"] is False and out["afterResync"] is False
        assert out["afterFailedRead"] is True  # a failed read claims nothing


class TestWiring:
    def test_boot_creates_the_strip_under_the_menu_bar_and_reads_it_once(self):
        boot = (_OS / "boot.js").read_text(encoding="utf-8")
        assert "createDrivingStrip({ after: $('menubar'), api, events, toasts })" in boot
        assert boot.count("drivingStrip.read()") == 1

    def test_the_strip_listens_for_the_event_the_server_sends(self):
        src = _STRIP.read_text(encoding="utf-8")
        server = (_ROOT / "dourmouse" / "os_api" / "apps.py").read_text(encoding="utf-8")
        assert "'app_driver_indicator'" in src and 'INDICATOR_EVENT = "app_driver_indicator"' in server

    def test_the_strip_has_no_poll_and_stop_is_the_kill_route(self):
        src = re.sub(r"/\*.*?\*/", "", _STRIP.read_text(encoding="utf-8"), flags=re.S)
        assert "setInterval" not in src and "every(" not in src and "fetch(" not in src
        assert "/api/os/apps/kill" in src and "/api/os/apps/resume" not in src

    def test_shell_css_makes_room_for_the_strip_and_keeps_it_above_content(self):
        css = (_OS / "shell.css").read_text(encoding="utf-8")
        assert "#drivingstrip" in css and 'body[data-driving="true"] #shell { top: 56px; }' in css
        assert "z-index: 940" in css  # under the menu bar (950) and its panels, over the stage

    def test_webui_makes_exactly_one_registration_call(self):
        webui = (_ROOT / "dourmouse" / "webui.py").read_text(encoding="utf-8")
        assert webui.count("_apps_api.bind_indicator_hub(events_hub)") == 1
