"""FIX-R R-5: hiding the console (the red button) turns the microphone and camera off.

Two layers, both run under plain node:
* preload.js's capture tracker, loaded with a fake electron and fake media devices: it records the
  streams and speech recognizers a page opens and window.__dmStopMedia() stops what is live;
* electron/main.js's own close handler (the harness of the FB shell tests): it asks the page to stop
  capture before hiding, and says so the first time something was really stopped.
What this cannot show is macOS's own mic indicator; the live check in an isolated copy is in the results.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from dourmouse.tests.b2_harness import ELECTRON, NODE
from dourmouse.tests.test_fix_FB_electron_shell import PRELUDE, run_scenario

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

PRELOAD_TEST = r'''
const Module = require("module");
const path = require("path");
const log = [];
const fakeElectron = {
  contextBridge: { exposeInMainWorld() {}, executeInMainWorld({ func }) { func(); } },
  ipcRenderer: { invoke() {}, on() {}, send() {} },
};
const origLoad = Module._load;
Module._load = function (request, ...rest) { return request === "electron" ? fakeElectron : origLoad.call(this, request, ...rest); };

class Track {
  constructor(kind) { this.kind = kind; this.readyState = "live"; this.stops = 0; this.l = {}; }
  addEventListener(ev, fn) { (this.l[ev] = this.l[ev] || []).push(fn); }
  stop() { this.readyState = "ended"; this.stops += 1; log.push("track-stop:" + this.kind); }
  end() { this.readyState = "ended"; (this.l.ended || []).forEach((f) => f()); }
}
const opened = [];
Object.defineProperty(global, "navigator", { configurable: true, value: { mediaDevices: {
  getUserMedia: async (c) => { const ts = []; if (c.audio) ts.push(new Track("audio")); if (c.video) ts.push(new Track("video")); opened.push(ts); return { getTracks: () => ts }; },
  getDisplayMedia: async () => { const ts = [new Track("screen")]; opened.push(ts); return { getTracks: () => ts }; },
} } });
class Rec { constructor() { this.l = {}; } addEventListener(ev, fn) { (this.l[ev] = this.l[ev] || []).push(fn); } start() { log.push("rec-start"); } abort() { log.push("rec-abort"); } }
global.window = { SpeechRecognition: Rec, webkitSpeechRecognition: undefined, dispatchEvent(e) { log.push("event:" + e.type); return true; } };
global.CustomEvent = class { constructor(type) { this.type = type; } };
global.document = {};
require(path.join(process.env.T_ELECTRON, "preload.js"));

(async () => {
  const out = {};
  out.installed = typeof window.__dmStopMedia;
  out.enumerable = Object.keys(window).includes("__dmStopMedia");
  await navigator.mediaDevices.getUserMedia({ audio: true });
  await navigator.mediaDevices.getUserMedia({ video: true });
  await navigator.mediaDevices.getDisplayMedia({});
  const finished = await navigator.mediaDevices.getUserMedia({ audio: true });
  finished.getTracks()[0].end(); // the page already stopped this one itself
  new window.SpeechRecognition().start();
  const before = opened.flat().filter((t) => t.readyState === "live").length;
  out.liveBefore = before;
  out.stopped = window.__dmStopMedia();
  out.liveAfter = opened.flat().filter((t) => t.readyState === "live").length;
  out.stopCounts = opened.flat().map((t) => t.stops);
  out.again = window.__dmStopMedia();
  out.log = log;
  console.log("RESULT:" + JSON.stringify(out));
})();
'''


def _run_node(tmp_path: Path, code: str) -> dict:
    script = tmp_path / "t.js"
    script.write_text(code, encoding="utf-8")
    proc = subprocess.run([str(NODE), str(script)], capture_output=True, text=True, timeout=60, check=False,
                          env={"PATH": "/usr/bin:/bin", "T_ELECTRON": str(ELECTRON)})
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:")]
    assert lines, f"no result\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
    return json.loads(lines[-1][len("RESULT:"):])


def test_the_preload_tracker_stops_every_live_capture_and_tells_the_page_first(tmp_path):
    out = _run_node(tmp_path, PRELOAD_TEST)
    assert out["installed"] == "function" and out["enumerable"] is False
    assert out["liveBefore"] == 3
    assert out["stopped"] == 4, "three live tracks and one speech recognizer"
    assert out["liveAfter"] == 0
    assert out["stopCounts"] == [1, 1, 1, 0], "the stream the page had already ended is not stopped again"
    assert out["again"] == 0
    log = out["log"]
    assert log.index("event:dourmouse:console-hiding") < log.index("track-stop:audio"), "the page is told before anything is stopped"
    assert log.count("event:dourmouse:console-hiding") == 2, "told on each call, even the second that stops nothing"
    assert "rec-abort" in log


_NOTE_PRELUDE = PRELUDE.replace(
    "Notification: class { static isSupported() { return false; } }",
    "Notification: class { static isSupported() { return true; } constructor(o) { this.o = o; (globalThis.__notes = globalThis.__notes || []).push(o); } show() {} }",
)

CLOSE_SCENARIO = r'''
main(async () => {
  T.ensurePaneView();
  const createMainWindow = T.fn("createMainWindow");
  const w = createMainWindow();
  const asked = [];
  let answer = 2;
  w.webContents.executeJavaScript = (code) => { asked.push(code); return Promise.resolve(answer); };
  const evt = () => ({ prevented: false, preventDefault() { this.prevented = true; } });
  const e1 = evt(); w.emit("close", e1);
  await new Promise((r) => setTimeout(r, 20));
  R.first = { prevented: e1.prevented, hidden: w.acts.includes("hide"), asked: asked.slice(), notes: (globalThis.__notes || []).length };
  const e2 = evt(); w.emit("close", e2);
  await new Promise((r) => setTimeout(r, 20));
  R.second = { asked: asked.length, notes: (globalThis.__notes || []).length };
  answer = 0;
  w.emit("close", evt());
  await new Promise((r) => setTimeout(r, 20));
  R.nothingToStop = { notes: (globalThis.__notes || []).length };
  // the page cannot be reached: the console is still hidden, nothing throws
  w.webContents.executeJavaScript = () => Promise.reject(new Error("render frame disposed"));
  const e4 = evt(); w.emit("close", e4);
  await new Promise((r) => setTimeout(r, 20));
  R.unreachable = { prevented: e4.prevented, hiddenTimes: w.acts.filter((a) => a === "hide").length };
  // a real quit does not run any of this
  appEvents["before-quit"]();
  asked.length = 0;
  const e5 = evt(); w.emit("close", e5);
  R.quit = { prevented: e5.prevented, asked: asked.length };
  R.note = (globalThis.__notes || [])[0] || null;
});
'''


def test_hiding_the_console_stops_capture_and_says_so_once(tmp_path):
    from dourmouse.tests import test_fix_FB_electron_shell as fb

    original = fb.PRELUDE
    fb.PRELUDE = _NOTE_PRELUDE
    try:
        out = run_scenario(tmp_path, CLOSE_SCENARIO)
    finally:
        fb.PRELUDE = original
    assert out["first"]["prevented"] is True and out["first"]["hidden"] is True
    assert out["first"]["asked"] == ["window.__dmStopMedia ? window.__dmStopMedia() : 0"]
    assert out["first"]["notes"] == 1, "the owner is told, the first time something was really stopped"
    assert out["second"] == {"asked": 2, "notes": 1}, "stopped again, told only once"
    assert out["nothingToStop"]["notes"] == 1
    assert out["unreachable"]["prevented"] is True and out["unreachable"]["hiddenTimes"] == 4
    assert out["quit"] == {"prevented": False, "asked": 0}
    assert "microphone and camera were turned off" in out["note"]["body"]


def test_a_screen_hears_the_console_hiding_and_stops_hearing_it_when_left(tmp_path):
    """ui/assets/os/core/ctx.js: ctx.onConsoleHiding is tied to the screen's own lifetime."""
    from dourmouse.tests.test_os_core import run as run_os_harness

    out = run_os_harness(tmp_path, """
const listeners = new Set();
globalThis.addEventListener = (type, fn, opts) => {
  if (type !== 'dourmouse:console-hiding') return;
  const entry = { fn, signal: opts && opts.signal };
  listeners.add(entry);
  if (entry.signal) entry.signal.addEventListener('abort', () => listeners.delete(entry));
};
globalThis.removeEventListener = (type, fn) => { for (const e of [...listeners]) if (e.fn === fn) listeners.delete(e); };
const fire = () => [...listeners].forEach((e) => e.fn({}));
const events = createEvents({ EventSourceImpl: class { close() {} }, setTimer: () => 0, clearTimer: () => {} });
const timers = createTimers({ setInterval: () => 1, clearInterval: () => {}, doc: { hidden: false } });
const thread = { key: 'VOICE', subscribe: () => () => {} };
const deps = { events, timers, keymap: { bind: () => () => {}, pushEsc: () => () => {} }, api: { withSignal: (s) => ({ signal: s }) },
  chat: { thread: () => thread }, approvals: { pending: () => null, declineAll: async () => 0, get: () => null },
  scope: { tabId: () => 't', project: () => null, leaveProject() {}, onChange: () => () => {} }, chrome: {},
  overlays: { open: () => false, onChange: () => () => {} }, host: {}, prefs: { read: () => null, write: () => true },
  toasts: { show() {} }, kit: {}, renderApproval: () => ({ off: () => {} }) };
const h = createScreenCtx({ id: 'VOICE', root: {}, deps });
let heard = 0;
h.ctx.onConsoleHiding(() => { heard += 1; });
fire();
R.whileMounted = { heard, listeners: listeners.size };
h.dispose();
fire();
R.afterLeaving = { heard, listeners: listeners.size };
""", "import { createTimers, createScreenCtx } from 'core/ctx.js';\nimport { createEvents } from 'core/events.js';")
    assert out["whileMounted"] == {"heard": 1, "listeners": 1}
    assert out["afterLeaving"] == {"heard": 1, "listeners": 0}
