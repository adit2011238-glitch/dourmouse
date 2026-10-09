"""Phase I2, desktop shell side of crash recovery (finding #171).

electron/main.js is loaded under plain node with a small fake ``electron`` and a fake
``child_process.spawn``, and its OWN supervision functions are driven: nothing here
re-implements the logic under test. The server it talks to is a real HTTP server
(``/workspace`` answers 200 only while the fake child is "up"), so the readiness wait,
the restart pauses and the give-up rule run on real timers (shortened through the
``SUPERVISOR`` object, which exists for exactly this).

What this cannot prove is what a real Electron does with a real crashed renderer or a
real Python server; that is checked live in an isolated copy of the app and recorded in
the finding.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

ELECTRON = Path(__file__).resolve().parents[2] / "electron"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

PRELUDE = r'''
const Module = require("module");
const EventEmitter = require("events");
const fs = require("fs");
const os = require("os");
const path = require("path");
const http = require("http");

const MAIN = process.env.T_MAIN;
const DATA = fs.mkdtempSync(path.join(os.tmpdir(), "dm-i2-"));
process.env.DOURMOUSE_USER_DATA_DIR = path.join(DATA, "ud");
const PORT = parseInt(process.env.DOURMOUSE_UI_PORT, 10);

const rec = { cookies: [], dialogs: [], quits: 0, spawns: [], executed: [], revealed: [], copied: [], eventStreams: 0, eventStreamsClosed: 0 };
let dialogAnswers = [];
let up = false;
let executeAnswers = [];
let wcSeq = 0;

class FakeSession extends EventEmitter {
  constructor() { super(); this.cookies = { set: (c) => { rec.cookies.push(c); return Promise.resolve(); } }; }
  setUserAgent() {} setPermissionRequestHandler() {} setPermissionCheckHandler() {} fetch() { return Promise.reject(new Error("no")); }
}
const SESSION = new FakeSession();
class FakeWC extends EventEmitter {
  constructor() { super(); this.id = ++wcSeq; this.url = "http://127.0.0.1:" + PORT + "/#/home"; this.reloads = 0; this.loads = []; this.crashes = 0; this.destroyed = false; }
  getURL() { return this.url; }
  isDestroyed() { return this.destroyed; }
  reload() { this.reloads += 1; setImmediate(() => this.emit("did-finish-load")); }
  loadURL(u) { this.url = u; this.loads.push(u); return Promise.resolve(); }
  forcefullyCrashRenderer() { this.crashes += 1; }
  setWindowOpenHandler() {}
  isLoading() { return false; }
  executeJavaScript(code) { rec.executed.push(code); return Promise.resolve(executeAnswers.length ? executeAnswers.shift() : true); }
  get session() { return SESSION; }
}
class FakeWindow extends EventEmitter {
  constructor() { super(); this.webContents = new FakeWC(); this.shown = 0; }
  show() { this.shown += 1; }
  loadURL(u) { return this.webContents.loadURL(u); }
  isDestroyed() { return false; }
  getContentBounds() { return { x: 0, y: 0, width: 1400, height: 900 }; }
}
const appHandlers = {};
const ipcHandlers = {};
const fakeElectron = {
  app: {
    setPath() {}, getPath: (n) => (n === "userData" ? path.join(DATA, "ud") : path.join(DATA, n)), getName: () => "Dourmouse",
    commandLine: { appendSwitch() {} }, whenReady: () => new Promise(() => {}), on: (n, fn) => { appHandlers[n] = fn; },
    isPackaged: false, quit() { rec.quits += 1; }, dock: { setIcon() {} }, setName() {},
  },
  BrowserWindow: FakeWindow, BrowserView: class {},
  ipcMain: { handle: (n, fn) => { ipcHandlers[n] = fn; }, on() {} },
  shell: { openPath: () => Promise.resolve(""), showItemInFolder: (p) => { rec.revealed.push(p); }, openExternal: () => Promise.resolve() },
  Tray: class {}, Menu: { buildFromTemplate: () => ({}) }, nativeImage: { createFromDataURL: () => ({}), createFromBuffer: () => ({ isEmpty: () => true }) },
  Notification: class { static isSupported() { return false; } },
  session: { defaultSession: SESSION },
  clipboard: { writeText: (t) => { rec.copied.push(t); } },
  dialog: {
    showErrorBox() {},
    showMessageBox: (...args) => {
      const opts = args[args.length - 1];
      rec.dialogs.push(opts);
      return Promise.resolve({ response: dialogAnswers.length ? dialogAnswers.shift() : 99 });
    },
  },
};

// A fake child process. `plan` says what each successive spawn does.
let plan = [];
class FakeChild extends EventEmitter {
  constructor(n) {
    super();
    this.pid = 40000 + n; this.killed = false; this.killSignals = [];
    this.stdin = Object.assign(new EventEmitter(), { written: "", end: (d) => { this.stdin.written += d || ""; } });
    this.stdout = new EventEmitter(); this.stderr = new EventEmitter();
  }
  kill(sig) { this.killed = true; this.killSignals.push(sig || "SIGTERM"); setImmediate(() => this.emit("exit", null, sig || "SIGTERM")); return true; }
}
const children = [];
function fakeSpawn(cmd, args, opts) {
  const child = new FakeChild(children.length + 1);
  children.push(child);
  const step = plan.length ? plan.shift() : { ready: true };
  rec.spawns.push({ cmd, args, env: opts.env, stdio: opts.stdio });
  setTimeout(() => {
    if (step.ready) up = true;
    if (step.output) child.stdout.emit("data", Buffer.from(step.output));
    if (step.exitAfter !== undefined) setTimeout(() => { up = false; child.emit("exit", step.code === undefined ? 1 : step.code, step.signal || null); }, step.exitAfter);
  }, 5);
  if (step.spawnError) setImmediate(() => child.emit("error", new Error(step.spawnError)));
  return child;
}

// The real server stand-in.
const server = http.createServer((req, res) => {
  if (req.url === "/workspace") { res.statusCode = up ? 200 : 503; return res.end("ok"); }
  if (req.url === "/api/security/owner-gate") { res.setHeader("content-type", "application/json"); return res.end(JSON.stringify({ enforced: false })); }
  if (req.url === "/api/state") { res.setHeader("content-type", "application/json"); return res.end(JSON.stringify({ alerts: [] })); }
  if (req.url === "/api/events") {
    rec.eventStreams += 1;
    res.setHeader("content-type", "text/event-stream"); res.write(": hi\n\n");
    req.on("close", () => { rec.eventStreamsClosed += 1; });
    return;
  }
  res.statusCode = 404; res.end("");
});

const realChild = require("child_process");
const origLoad = Module._load;
Module._load = function (request, ...rest) {
  if (request === "electron") return fakeElectron;
  if (request === "child_process") return { ...realChild, spawn: fakeSpawn };
  return origLoad.call(this, request, ...rest);
};

const hooks = `
module.exports.__t = {
  SUPERVISOR, ensureServer, spawnServerProcess, stopServer, recoverConsole, wireConsoleRecovery, showConsoleNotice,
  startAlertNotifications, serverLogPath, planServerRestart, loadMapOnce,
  setMap: (w) => { mapWindow = w; },
  setWindow: (w) => { mainWindow = w; },
  state: () => ({ supervised: serverSupervised, gaveUp: serverGaveUp, quitting: serverQuitting, restartTimes: serverRestartTimes.length, secret: OWNER_SECRET, proc: serverProcess }),
};`;
const src = fs.readFileSync(MAIN, "utf8").replace(/^#!.*\n/, "");
const mod = new Module(MAIN, null);
mod.filename = MAIN;
mod.paths = Module._nodeModulePaths(path.dirname(MAIN));
mod._compile(src + hooks, MAIN);
const T = mod.exports.__t;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(fn, ms) { const end = Date.now() + ms; while (Date.now() < end) { if (fn()) return true; await wait(10); } return fn(); }
const R = {};
function fast() { T.SUPERVISOR.backoffMs = [20, 20, 20]; T.SUPERVISOR.readyTimeoutMs = 2000; T.SUPERVISOR.noticeRetryMs = 20; }
async function main(fn) {
  await new Promise((r) => server.listen(PORT, "127.0.0.1", r));
  try { await fn(); } catch (e) { R.__error = String(e && e.stack || e); }
  console.log("RESULT:" + JSON.stringify(R));
  process.exit(0);
}
'''

START_AND_CRASH = r'''
main(async () => {
  fast();
  const win = new FakeWindow();
  T.setWindow(win);
  plan = [{ ready: true, output: "hello from the server\n" }, { ready: true }];
  await T.ensureServer();
  const first = T.state();
  R.firstSecretOnStdin = children[0].stdin.written.trim() === first.secret;
  R.firstEnvHasNoSecret = !Object.values(rec.spawns[0].env).some((v) => String(v).includes(first.secret));
  R.firstEnvGate = rec.spawns[0].env.DOURMOUSE_OWNER_GATE;
  R.firstEnvLog = rec.spawns[0].env.DOURMOUSE_SERVER_LOG === T.serverLogPath();
  R.firstCookies = rec.cookies.map((c) => [new URL(c.url).hostname, c.value === first.secret, c.httpOnly, c.sameSite]);
  R.supervised = first.supervised;
  R.noticeBefore = rec.executed.length;
  // the server dies under the app
  const secretBefore = first.secret;
  up = false;
  children[0].emit("exit", null, "SIGKILL");
  R.restarted = await until(() => rec.spawns.length === 2 && up, 3000);
  await until(() => rec.executed.length > 0, 3000);
  const after = T.state();
  R.secondSecretOnStdin = children[1].stdin.written.trim() === after.secret;
  R.secretChanged = after.secret !== secretBefore;
  R.secondEnvHasNoSecret = !Object.values(rec.spawns[1].env).some((v) => String(v).includes(after.secret) || String(v).includes(secretBefore));
  R.cookiesAfter = rec.cookies.slice(2).map((c) => [new URL(c.url).hostname, c.value === after.secret]);
  R.noticeCount = rec.executed.length;
  R.noticeHasTitle = rec.executed[0].includes("The server stopped and was restarted");
  R.noticeUsesShellToasts = rec.executed[0].includes("window.__dmShell") && rec.executed[0].includes("toasts.show");
  R.noticeNoSecret = !rec.executed.some((c) => c.includes(after.secret) || c.includes(secretBefore));
  R.dialogs = rec.dialogs.length;
  const log = fs.readFileSync(T.serverLogPath(), "utf8");
  R.logHasServerOutput = log.includes("hello from the server");
  R.logHasExit = log.includes("server process exited (signal SIGKILL)");
  R.logHasRestart = log.includes("restarting the server in 20 ms (try 1 of 3)");
  R.logHasNoSecret = !log.includes(secretBefore) && !log.includes(after.secret);
  R.logMode = (fs.statSync(T.serverLogPath()).mode & 0o777).toString(8);
});
'''

GIVE_UP = r'''
main(async () => {
  fast();
  const win = new FakeWindow();
  T.setWindow(win);
  plan = [{ ready: true }, { ready: false, exitAfter: 10 }, { ready: false, exitAfter: 10 }, { ready: false, exitAfter: 10 }, { ready: true }];
  await T.ensureServer();
  up = false;
  children[0].emit("exit", 1, null);
  await until(() => rec.dialogs.length > 0, 5000);
  await wait(300);
  R.spawns = rec.spawns.length;
  R.dialogs = rec.dialogs.length;
  const d = rec.dialogs[0] || {};
  R.dialogHasLogPath = (d.detail || "").includes(T.serverLogPath());
  R.dialogButtons = d.buttons;
  R.gaveUp = T.state().gaveUp;
  R.noToast = rec.executed.length;
  // choosing "Try again" starts it once more and resets the count
  dialogAnswers = [];
}) ;
'''

TRY_AGAIN = r'''
main(async () => {
  fast();
  T.setWindow(new FakeWindow());
  plan = [{ ready: true }, { ready: false, exitAfter: 10 }, { ready: false, exitAfter: 10 }, { ready: false, exitAfter: 10 }, { ready: true }];
  await T.ensureServer();
  dialogAnswers = [1];
  up = false;
  children[0].emit("exit", 1, null);
  await until(() => rec.spawns.length === 5 && up, 5000);
  R.spawnsAfterTryAgain = rec.spawns.length;
  R.stillOneDialog = rec.dialogs.length;
  R.gaveUpAfter = T.state().gaveUp;
});
'''

QUIT_CHOICE = r'''
main(async () => {
  fast();
  T.setWindow(new FakeWindow());
  plan = [{ ready: true }, { ready: false, exitAfter: 10 }, { ready: false, exitAfter: 10 }, { ready: false, exitAfter: 10 }];
  dialogAnswers = [2];
  await T.ensureServer();
  up = false;
  children[0].emit("exit", 1, null);
  await until(() => rec.quits > 0, 5000);
  R.quits = rec.quits;
  R.spawns = rec.spawns.length;
});
'''

LOG_CHOICE = r'''
main(async () => {
  fast();
  T.setWindow(new FakeWindow());
  plan = [{ ready: true }, { ready: false, exitAfter: 10 }, { ready: false, exitAfter: 10 }, { ready: false, exitAfter: 10 }];
  dialogAnswers = [0];
  await T.ensureServer();
  up = false;
  children[0].emit("exit", 1, null);
  await until(() => rec.copied.length > 0, 5000);
  R.copied = rec.copied; R.revealed = rec.revealed;
  R.logPath = T.serverLogPath();
  R.quits = rec.quits;
});
'''

SPACED_OUT = r'''
main(async () => {
  fast();
  T.SUPERVISOR.windowMs = 300;
  T.setWindow(new FakeWindow());
  plan = [{ ready: true }];
  await T.ensureServer();
  for (let i = 0; i < 6; i += 1) {
    const before = rec.spawns.length;
    up = false;
    children[children.length - 1].emit("exit", 1, null);
    await until(() => rec.spawns.length === before + 1 && up, 3000);
    await wait(450); // longer than the window: the earlier crashes no longer count
  }
  R.spawns = rec.spawns.length;
  R.dialogs = rec.dialogs.length;
  R.gaveUp = T.state().gaveUp;
});
'''

QUITTING = r'''
main(async () => {
  fast();
  T.SUPERVISOR.backoffMs = [400, 400, 400];
  T.setWindow(new FakeWindow());
  plan = [{ ready: true }];
  await T.ensureServer();
  up = false;
  children[0].emit("exit", 1, null);          // a crash: a restart is planned in 400 ms
  await wait(50);
  appHandlers["before-quit"]();                // the owner quits before it happens
  await wait(700);
  R.spawns = rec.spawns.length;
  R.quitting = T.state().quitting;
  // and an exit caused by our own stop is never a crash
  R.dialogs = rec.dialogs.length;
});
'''

STOP_IS_NOT_A_CRASH = r'''
main(async () => {
  fast();
  T.setWindow(new FakeWindow());
  plan = [{ ready: true }];
  await T.ensureServer();
  T.stopServer();
  await wait(300);
  R.spawns = rec.spawns.length;
  R.killSignals = children[0].killSignals;
  R.dialogs = rec.dialogs.length;
});
'''

START_FAILS = r'''
main(async () => {
  fast();
  T.setWindow(new FakeWindow());
  plan = [{ ready: false, exitAfter: 10, code: 3, output: "Traceback: boom\n" }];
  const t0 = Date.now();
  try { await T.ensureServer(); R.threw = false; } catch (e) { R.threw = true; R.message = String(e.message); }
  R.seconds = (Date.now() - t0) / 1000;
  R.supervised = T.state().supervised;
  await wait(300);
  R.spawns = rec.spawns.length; // a server that never started is not restarted behind the error dialog
});
'''

SPAWN_ERROR = r'''
main(async () => {
  fast();
  T.setWindow(new FakeWindow());
  plan = [{ ready: false, spawnError: "spawn ENOENT" }];
  try { await T.ensureServer(); R.threw = false; } catch (e) { R.threw = true; R.message = String(e.message); }
});
'''

HUNG_RESTART = r'''
main(async () => {
  fast();
  T.SUPERVISOR.readyTimeoutMs = 300;
  T.setWindow(new FakeWindow());
  plan = [{ ready: true }, { ready: false }, { ready: true }];   // the first restart never answers
  await T.ensureServer();
  up = false;
  children[0].emit("exit", 1, null);
  await until(() => rec.spawns.length === 3 && up, 5000);
  R.spawns = rec.spawns.length;
  R.hungChildKilled = children[1].killSignals;
  R.dialogs = rec.dialogs.length;
});
'''

NOTICE_RETRY = r'''
main(async () => {
  fast();
  T.setWindow(new FakeWindow());
  executeAnswers = [false, false, true];
  T.showConsoleNotice("warn", "t", "d");
  await until(() => rec.executed.length >= 3, 3000);
  await wait(100);
  R.calls = rec.executed.length;
  // a window that is gone is not an error
  T.setWindow(null);
  T.showConsoleNotice("warn", "t", "d");
  R.afterNullWindow = rec.executed.length;
});
'''

EVENT_STREAM = r'''
main(async () => {
  fast();
  const win = new FakeWindow();
  T.setWindow(win);
  plan = [{ ready: true }, { ready: true }];
  await T.ensureServer();
  T.startAlertNotifications();
  await until(() => rec.eventStreams === 1, 2000);
  up = false;
  children[0].emit("exit", 1, null);
  await until(() => rec.spawns.length === 2 && up, 3000);
  await until(() => rec.eventStreams === 2, 3000);
  R.streams = rec.eventStreams;
  await until(() => rec.eventStreamsClosed === 1, 3000);
  R.oldClosed = rec.eventStreamsClosed;
});
'''

CONSOLE_CRASH = r'''
main(async () => {
  fast();
  const win = new FakeWindow();
  T.setWindow(win);
  T.wireConsoleRecovery(win);
  const wc = win.webContents;
  wc.emit("render-process-gone", {}, { reason: "killed", exitCode: 9 });
  wc.emit("render-process-gone", {}, { reason: "clean-exit", exitCode: 0 });
  await wait(400);
  R.ignoredReasons = wc.reloads;
  wc.emit("render-process-gone", {}, { reason: "crashed", exitCode: 11 });
  await until(() => wc.reloads === 1, 2000);
  R.firstCrashReloads = wc.reloads;
  await wait(100);
  wc.emit("render-process-gone", {}, { reason: "oom", exitCode: 0 });
  await wait(600);
  R.secondCrashReloads = wc.reloads;
  R.dialogsAfterSecond = rec.dialogs.length;
  R.dialogHasLog = (rec.dialogs[0] && rec.dialogs[0].detail || "").includes(T.serverLogPath());
  wc.emit("render-process-gone", {}, { reason: "crashed", exitCode: 11 });
  await wait(300);
  R.dialogsAfterThird = rec.dialogs.length;
  const log = fs.readFileSync(T.serverLogPath(), "utf8");
  R.logHasCrash = log.includes("console renderer gone") && log.includes("the console window crashed; reloading it once");
  R.logHasGiveUp = log.includes("again within");
});
'''

CONSOLE_HANG = r'''
main(async () => {
  fast();
  T.SUPERVISOR.hangGraceMs = 150;
  const win = new FakeWindow();
  T.setWindow(win);
  T.wireConsoleRecovery(win);
  const wc = win.webContents;
  // it comes back by itself: nothing is done
  win.emit("unresponsive");
  await wait(50);
  win.emit("responsive");
  await wait(300);
  R.recoveredByItself = { crashes: wc.crashes, reloads: wc.reloads };
  // it does not: it is ended and reloaded once
  win.emit("unresponsive");
  await until(() => wc.reloads === 1, 2000);
  R.stuck = { crashes: wc.crashes, reloads: wc.reloads };
  // the crash event that follows our own forced end does not reload a second time
  wc.emit("render-process-gone", {}, { reason: "crashed", exitCode: 1 });
  await wait(400);
  R.afterForcedCrashEvent = wc.reloads;
  const log = fs.readFileSync(T.serverLogPath(), "utf8");
  R.logHasHang = log.includes("the console window is not responding") && log.includes("responding again");
});
'''

MAP_WINDOW = r'''
main(async () => {
  const map = new FakeWindow();
  T.setMap(map);
  R.loadedBeforeOpen = map.webContents.loads.length;
  R.firstOpen = await ipcHandlers["bridge:open_map"]();
  R.afterFirstOpen = map.webContents.loads.slice();
  await ipcHandlers["bridge:open_map"]();
  R.afterSecondOpen = map.webContents.loads.length;
  R.shown = map.shown;
  T.setMap(null);
  R.noWindow = await ipcHandlers["bridge:open_map"]();
});
'''

CONSOLE_OFFSITE = r'''
main(async () => {
  fast();
  const win = new FakeWindow();
  win.webContents.url = "https://evil.example/";   // the window somehow shows another site when it crashes
  T.setWindow(win);
  T.wireConsoleRecovery(win);
  win.webContents.emit("render-process-gone", {}, { reason: "crashed", exitCode: 11 });
  await until(() => win.webContents.loads.length > 0, 2000);
  R.reloads = win.webContents.reloads;
  R.loaded = win.webContents.loads;
});
'''


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run(scenario: str) -> dict:
    port = _free_port()
    assert port not in (8765, 9333, 9334)
    env = {
        **os.environ,
        "T_MAIN": str(ELECTRON / "main.js"),
        "DOURMOUSE_UI_PORT": str(port),
        "DOURMOUSE_ELECTRON_CDP_PORT": str(_free_port()),
        "DOURMOUSE_ELECTRON_PANE_PORT": str(_free_port()),
        "DOURMOUSE_ELECTRON_REUSE_SERVER": "0",
    }
    proc = subprocess.run([str(NODE), "-e", PRELUDE + scenario], capture_output=True, text=True, env=env, timeout=90, check=False)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:")]
    assert lines, f"no result\nSTDOUT:\n{proc.stdout[-3000:]}\nSTDERR:\n{proc.stderr[-3000:]}"
    result = json.loads(lines[-1][len("RESULT:"):])
    assert "__error" not in result, result["__error"]
    return result


class TestServerRestart:
    def test_a_server_that_dies_is_started_again_with_a_fresh_secret_and_cookie(self):
        r = _run(START_AND_CRASH)
        assert r["supervised"] is True
        assert r["firstSecretOnStdin"] and r["firstEnvHasNoSecret"], "the secret goes on stdin only, never env"
        assert r["firstEnvGate"] == "stdin" and r["firstEnvLog"]
        assert r["firstCookies"] == [["127.0.0.1", True, True, "strict"], ["localhost", True, True, "strict"]]
        assert r["restarted"] is True
        assert r["secondSecretOnStdin"] and r["secretChanged"], "a restart has a fresh secret"
        assert r["secondEnvHasNoSecret"]
        assert r["cookiesAfter"] == [["127.0.0.1", True], ["localhost", True]], "the cookie matches the new secret"
        assert r["dialogs"] == 0

    def test_the_console_gets_one_small_toast_through_its_own_toast_stack(self):
        r = _run(START_AND_CRASH)
        assert r["noticeBefore"] == 0
        assert r["noticeCount"] == 1
        assert r["noticeHasTitle"] and r["noticeUsesShellToasts"]
        assert r["noticeNoSecret"]

    def test_the_log_records_the_exit_and_the_restart_and_never_the_secret(self):
        r = _run(START_AND_CRASH)
        assert r["logHasServerOutput"] and r["logHasExit"] and r["logHasRestart"]
        assert r["logHasNoSecret"]
        assert r["logMode"] == "600"

    def test_the_old_event_stream_is_replaced_so_notifications_keep_working(self):
        r = _run(EVENT_STREAM)
        assert r["streams"] == 2 and r["oldClosed"] == 1

    def test_a_restart_that_never_answers_is_ended_and_counted_then_tried_again(self):
        r = _run(HUNG_RESTART)
        assert r["spawns"] == 3
        assert r["hungChildKilled"] == ["SIGKILL"]
        assert r["dialogs"] == 0


class TestGivingUp:
    def test_after_the_third_failed_restart_there_is_one_dialog_with_the_log_path(self):
        r = _run(GIVE_UP)
        assert r["spawns"] == 4, "the original and three restarts, no more"
        assert r["dialogs"] == 1
        assert r["dialogHasLogPath"]
        assert r["dialogButtons"] == ["Copy the log path", "Try again", "Quit Dourmouse"]
        assert r["gaveUp"] is True
        assert r["noToast"] == 0, "no 'was restarted' toast for restarts that failed"

    def test_try_again_resets_the_count_and_starts_the_server(self):
        r = _run(TRY_AGAIN)
        assert r["spawnsAfterTryAgain"] == 5
        assert r["stillOneDialog"] == 1
        assert r["gaveUpAfter"] is False

    def test_quit_quits(self):
        r = _run(QUIT_CHOICE)
        assert r["quits"] == 1 and r["spawns"] == 4

    def test_copy_the_log_path_puts_exactly_the_log_path_on_the_clipboard_and_keeps_the_app_open(self):
        r = _run(LOG_CHOICE)
        assert r["copied"] == [r["logPath"]] and r["revealed"] == []
        assert r["quits"] == 0

    def test_crashes_spread_over_more_than_two_minutes_never_exhaust_the_tries(self):
        r = _run(SPACED_OUT)
        assert r["spawns"] == 7 and r["dialogs"] == 0 and r["gaveUp"] is False


class TestQuittingAndStartingUp:
    def test_quitting_cancels_a_planned_restart(self):
        r = _run(QUITTING)
        assert r["spawns"] == 1 and r["quitting"] is True and r["dialogs"] == 0

    def test_an_exit_caused_by_our_own_stop_is_not_a_crash(self):
        r = _run(STOP_IS_NOT_A_CRASH)
        assert r["spawns"] == 1 and r["killSignals"] == ["SIGTERM"] and r["dialogs"] == 0

    def test_a_server_that_dies_while_starting_fails_fast_naming_the_log(self):
        r = _run(START_FAILS)
        assert r["threw"] is True
        assert "exit code 3" in r["message"] and "server.log" in r["message"]
        assert r["seconds"] < 10, "it must not wait out the 60 second deadline for a process that already ended"
        assert r["supervised"] is False and r["spawns"] == 1

    def test_a_server_that_cannot_be_spawned_says_why(self):
        r = _run(SPAWN_ERROR)
        assert r["threw"] is True and "spawn ENOENT" in r["message"]


class TestMapWindowIsNotLoadedUntilShown:
    def test_the_hidden_map_page_loads_only_when_first_shown_and_only_once(self):
        r = _run(MAP_WINDOW)
        assert r["loadedBeforeOpen"] == 0
        assert r["firstOpen"] is True and len(r["afterFirstOpen"]) == 1 and r["afterFirstOpen"][0].endswith("/map")
        assert r["afterSecondOpen"] == 1 and r["shown"] == 2
        assert r["noWindow"] is False

    def test_main_js_never_loads_the_map_page_at_launch_except_for_the_smoke_test(self):
        src = (ELECTRON / "main.js").read_text(encoding="utf-8")
        assert src.count("/map`") == 1, "the one load of the map page is inside loadMapOnce"
        launch = src.split("mapWindow = new BrowserWindow({")[1].split("atlasWindow = new BrowserWindow")[0]
        assert "loadMapOnce()" in launch and 'DOURMOUSE_ELECTRON_VERIFY === "1"' in launch
        assert 'mapWindow.loadURL("data:text/html,' in launch, "a never navigated window is a DevTools target without a page"
        assert "/map" not in launch.replace("DOURMOUSE_ELECTRON_VERIFY", "")


class TestConsoleNotice:
    def test_a_page_that_is_not_the_shell_yet_is_retried_then_shows_it_once(self):
        r = _run(NOTICE_RETRY)
        assert r["calls"] == 3
        assert r["afterNullWindow"] == 3


class TestConsoleWindowRecovery:
    def test_a_crashed_console_renderer_is_reloaded_once_and_a_second_crash_asks_instead_of_looping(self):
        r = _run(CONSOLE_CRASH)
        assert r["ignoredReasons"] == 0, "killed and clean-exit are somebody's decision, not a crash"
        assert r["firstCrashReloads"] == 1
        assert r["secondCrashReloads"] == 1, "no second automatic reload inside the window"
        assert r["dialogsAfterSecond"] == 1 and r["dialogHasLog"]
        assert r["logHasCrash"] and r["logHasGiveUp"]

    def test_a_hung_console_is_ended_and_reloaded_once_but_left_alone_if_it_wakes_up(self):
        r = _run(CONSOLE_HANG)
        assert r["recoveredByItself"] == {"crashes": 0, "reloads": 0}
        assert r["stuck"] == {"crashes": 1, "reloads": 1}
        assert r["afterForcedCrashEvent"] == 1
        assert r["logHasHang"]

    def test_a_window_that_shows_another_site_is_brought_back_to_the_app_not_reloaded(self):
        r = _run(CONSOLE_OFFSITE)
        assert r["reloads"] == 0 and len(r["loaded"]) == 1 and r["loaded"][0].startswith("http://127.0.0.1:")


class TestBuildConfigListsWhatTheShellLoads:
    """Build preparation (I2): a self-contained build must carry every file main.js loads at run time.
    No build is run here; this reads package.json and main.js."""

    PKG = json.loads((ELECTRON / "package.json").read_text(encoding="utf-8"))
    MAIN = (ELECTRON / "main.js").read_text(encoding="utf-8")

    def test_every_module_main_js_requires_is_in_the_files_list(self):
        import re

        listed = set(self.PKG["build"]["files"])
        for name in re.findall(r'require\("\./(\w+)"\)', self.MAIN):
            assert f"{name}.js" in listed, name
        assert "preload.js" in listed and "main.js" in listed

    def test_every_file_main_js_reads_from_its_own_folder_is_packaged(self):
        listed = set(self.PKG["build"]["files"])
        assert "content/**" in listed and (ELECTRON / "content" / "frame-forms.js").exists()
        assert "resources/icon.png" in listed and (ELECTRON / "resources" / "icon.png").exists()

    def test_the_python_package_the_ui_and_the_runtime_are_extra_resources_without_tests_or_user_data(self):
        by_to = {r["to"]: r for r in self.PKG["build"]["extraResources"]}
        assert set(by_to) == {"dourmouse", "ui", ".venv"}
        py_filter = by_to["dourmouse"]["filter"]
        for excluded in ("!tests/**", "!workspace/**", "!**/__pycache__/**", "!**/*.pyc"):
            assert excluded in py_filter
        assert (ELECTRON.parent / "dourmouse" / "browser_scripts" / "element_ids.js").exists(), "browser_scripts rides inside dourmouse/"
        assert "**/*" in py_filter, "so dourmouse/browser_scripts and os_api and the rest are included"

    def test_no_dependency_was_changed(self):
        # finding #172: electron is the castLabs ECS build pinned by commit (owner approved 2026-10-09)
        assert self.PKG["devDependencies"] == {
            "electron": "github:castlabs/electron-releases#9b90904e4fe174122b99eca5b7b967aa79681cc6",
            "electron-builder": "^26.15.3",
            "@electron/notarize": "^2.5.0",
        }
        assert "dependencies" not in self.PKG
