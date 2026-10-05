"""Shared harness for the Phase B2 shell tests (permissions and passwords).

electron/main.js is loaded under plain node with a fake ``electron`` module, so its OWN
permission handlers, IPC handlers and pane bridge are driven for real. The fakes are
recording stand-ins: a session that keeps the handlers main.js installs, web contents that
record what is sent to them, a safeStorage whose cipher is visible in the files, a menu and a
dialog that record what they were asked to show, and a small HTTP server standing in for the
Dourmouse server's ``/api/vision/status`` (the privacy kill switch).

What this cannot prove is what Chromium and the real Keychain do. That was checked live in an
isolated copy of the app and is recorded in the B2 finding.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
from pathlib import Path

ELECTRON = Path(__file__).resolve().parents[2] / "electron"
NODE = shutil.which("node")

PRELUDE = r'''
const Module = require("module");
const EventEmitter = require("events");
const fs = require("fs");
const os = require("os");
const path = require("path");
const http = require("http");

const MAIN = process.env.T_MAIN;
const UI_PORT = parseInt(process.env.T_UI_PORT, 10);
const DATA = fs.mkdtempSync(path.join(os.tmpdir(), "dm-b2-"));
const USERDATA = path.join(DATA, "ud");
process.env.DOURMOUSE_USER_DATA_DIR = USERDATA;
process.env.DOURMOUSE_DOWNLOADS_DIR = path.join(DATA, "downloads");
process.env.DOURMOUSE_UI_PORT = String(UI_PORT);
Object.defineProperty(process, "platform", { value: process.env.T_PLATFORM || "darwin" });

const calls = { sent: [], menus: [], dialogs: [], asked: [], openExternal: [] };
const kill = { fail: false, mic_enabled: true, camera_enabled: true, hits: 0 };
const tcc = { microphone: "granted", camera: "granted", askResult: true };
const safe = { available: true, backend: "keychain", calls: 0 };
let dialogAnswer = 0;
let wcSeq = 0;

// the stand-in for the Dourmouse server's privacy status route
const visionServer = http.createServer((req, res) => {
  if (req.url.startsWith("/api/vision/status")) {
    kill.hits += 1;
    if (kill.fail) { res.writeHead(500); res.end("{}"); return; }
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ kill_switch: { mic_enabled: kill.mic_enabled, camera_enabled: kill.camera_enabled } }));
    return;
  }
  res.writeHead(404); res.end("{}");
});

class FakeSession extends EventEmitter {
  constructor() { super(); this.ua = ""; this.req = null; this.chk = null; this.display = null; this.device = null; this.preloads = []; }
  setUserAgent(ua) { this.ua = ua; }
  setPermissionRequestHandler(fn) { this.req = fn; }
  setPermissionCheckHandler(fn) { this.chk = fn; }
  setDisplayMediaRequestHandler(fn) { this.display = fn; }
  setDevicePermissionHandler(fn) { this.device = fn; }
  registerPreloadScript(s) { this.preloads.push(s); return "id" + this.preloads.length; }
  fetch() { return Promise.reject(new Error("no network in the fake")); }
}
const PANE_SESSION = new FakeSession();
const APP_SESSION = new FakeSession();

class FakeWC extends EventEmitter {
  constructor(session) {
    super();
    this.id = ++wcSeq; this.url = "about:blank"; this.title = ""; this.destroyed = false; this.zoom = 1;
    this.loads = []; this.sess = session; this.ua = ""; this.sentToThis = [];
    this.navigationHistory = { canGoBack: () => false, canGoForward: () => false, goBack() {}, goForward() {} };
  }
  get session() { return this.sess; }
  loadURL(u) { this.url = u; this.loads.push(u); this.emit("did-navigate", {}, u); return Promise.resolve(); }
  getURL() { return this.url; }
  getTitle() { return this.title; }
  isDestroyed() { return this.destroyed; }
  close() { this.destroyed = true; }
  setUserAgent(u) { this.ua = u; }
  setWindowOpenHandler() {}
  getZoomFactor() { return this.zoom; }
  setZoomFactor(z) { this.zoom = z; }
  isLoading() { return false; }
  isCurrentlyAudible() { return false; }
  findInPage() {} stopFindInPage() {} reload() {} stop() {} focus() {} print() {}
  printToPDF() { return Promise.resolve(Buffer.from("%PDF")); }
  send(channel, payload) { calls.sent.push({ wc: this.id, channel, payload }); this.sentToThis.push({ channel, payload }); }
}
class FakeView {
  constructor(opts) { this.opts = opts; this.webContents = new FakeWC(PANE_SESSION); this.bounds = null; }
  setBounds(b) { this.bounds = b; }
  setAutoResize() {}
  setBackgroundColor() {}
}
class FakeWindow {
  constructor() { this.views = []; this.webContents = new FakeWC(APP_SESSION); this.webContents.url = "http://127.0.0.1:" + UI_PORT + "/"; }
  isDestroyed() { return false; }
  getContentBounds() { return { x: 0, y: 0, width: 1400, height: 900 }; }
  getBrowserViews() { return this.views.slice(); }
  addBrowserView(v) { if (!this.views.includes(v)) this.views.push(v); }
  removeBrowserView(v) { this.views = this.views.filter((x) => x !== v); }
}
const handlers = {};
const listeners = {};
const fakeElectron = {
  app: {
    setPath() {}, setName() {}, getPath: (n) => (n === "userData" ? USERDATA : path.join(DATA, n)), getName: () => "Dourmouse",
    commandLine: { appendSwitch() {} }, whenReady: () => new Promise(() => {}), on() {}, isPackaged: false, quit() {}, dock: { setIcon() {} },
  },
  BrowserWindow: FakeWindow, BrowserView: FakeView,
  ipcMain: { handle: (name, fn) => { handlers[name] = fn; }, on: (name, fn) => { listeners[name] = fn; } },
  shell: { openPath: () => Promise.resolve(""), showItemInFolder() {}, openExternal: (u) => { calls.openExternal.push(u); return Promise.resolve(); } },
  Tray: class {},
  Menu: { buildFromTemplate: (items) => ({ items, popup: (o) => { calls.menus.push({ items, window: Boolean(o && o.window) }); } }) },
  nativeImage: { createFromDataURL: () => ({}), createFromBuffer: () => ({ isEmpty: () => true }) },
  Notification: class { static isSupported() { return false; } },
  session: { defaultSession: APP_SESSION, fromPartition: () => PANE_SESSION },
  dialog: { showErrorBox() {}, showMessageBox: async (_w, opts) => { calls.dialogs.push(opts); return { response: dialogAnswer }; } },
  safeStorage: {
    isEncryptionAvailable: () => { safe.calls += 1; return safe.available; },
    getSelectedStorageBackend: () => safe.backend,
    encryptString: (t) => {
      safe.calls += 1;
      return Buffer.from("FAKEKC1:" + Buffer.from(String(t), "utf8").toString("base64").split("").reverse().join(""), "utf8");
    },
    decryptString: (b) => {
      safe.calls += 1;
      const s = Buffer.from(b).toString("utf8");
      if (!s.startsWith("FAKEKC1:")) throw new Error("not ours");
      return Buffer.from(s.slice(8).split("").reverse().join(""), "base64").toString("utf8");
    },
  },
  systemPreferences: {
    getMediaAccessStatus: (n) => tcc[n],
    askForMediaAccess: async (n) => { calls.asked.push(n); if (tcc.askResult) tcc[n] = "granted"; return tcc.askResult; },
  },
};
const origLoad = Module._load;
Module._load = function (request, ...rest) {
  if (request === "electron") return fakeElectron;
  return origLoad.call(this, request, ...rest);
};

const hooks = `
module.exports.__t = {
  tabs, openTab, closeTab, activateTab, ensurePaneView, showPane, startPaneBridge, privacyState, promptQueue, tempGrants,
  pendingSaves, vault, addressBook, siteTable, flushBrowserStores, paneState, installPermissionPolicy, resetCipher: () => { encryptionAvailable = null; },
  setWindow: (w) => { mainWindow = w; },
  order: () => tabOrder.slice(), active: () => activeTabId, setScreen: (b) => { browserScreenActive = b; },
};`;
const src = fs.readFileSync(MAIN, "utf8").replace(/^#!.*\n/, "");
const mod = new Module(MAIN, null);
mod.filename = MAIN;
mod.paths = Module._nodeModulePaths(path.dirname(MAIN));
mod._compile(src + hooks, MAIN);
const T = mod.exports.__t;
const PANE_PORT = parseInt(process.env.DOURMOUSE_ELECTRON_PANE_PORT, 10);

function call(method, route, body, headers) {
  return new Promise((resolve, reject) => {
    const data = body === undefined ? null : JSON.stringify(body);
    const req = http.request({ host: "127.0.0.1", port: PANE_PORT, path: route, method, headers: { ...(data ? { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(data) } : {}), ...(headers || {}) } }, (res) => {
      let buf = "";
      res.on("data", (c) => (buf += c));
      res.on("end", () => { let j = null; try { j = JSON.parse(buf); } catch {} resolve({ status: res.statusCode, body: j, text: buf }); });
    });
    req.on("error", reject);
    if (data) req.write(data);
    req.end();
  });
}
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const win = new FakeWindow();
T.setWindow(win);
const server = T.startPaneBridge();

// who is calling an IPC handler
const consoleEvt = () => ({ sender: win.webContents, senderFrame: { parent: null, url: win.webContents.url } });
const consoleSubframeEvt = () => ({ sender: win.webContents, senderFrame: { parent: {}, url: win.webContents.url } });
const pageEvt = (wc, url) => ({ sender: wc, senderFrame: { parent: null, url: url === undefined ? wc.getURL() : url } });
const frameEvt = (wc, url) => ({ sender: wc, senderFrame: { parent: {}, url: url === undefined ? wc.getURL() : url } });
const strangerEvt = () => ({ sender: new FakeWC(PANE_SESSION), senderFrame: { parent: null, url: "https://stranger.example/" } });
// a permission request as Electron would deliver it; resolves with what the page is told
const ask = (wc, permission, details) => new Promise((resolve) => PANE_SESSION.req(wc, permission, resolve, details || {}));
// "has this settled yet?" for a request that may wait for the owner
function watch(promise) { const st = { done: false, value: undefined }; promise.then((v) => { st.done = true; st.value = v; }); return st; }
const check = (wc, permission, origin, details) => PANE_SESSION.chk(wc, permission, origin === undefined ? wc.getURL() : origin, details || {});
const readJson = (name) => { try { return JSON.parse(fs.readFileSync(path.join(USERDATA, "browser", name), "utf8")); } catch { return null; } };
const readRaw = (name) => { try { return fs.readFileSync(path.join(USERDATA, "browser", name), "utf8"); } catch { return null; } };

const R = {};
async function main(fn) {
  await new Promise((r) => visionServer.listen(UI_PORT, "127.0.0.1", r));
  await new Promise((r) => (server.listening ? r() : server.once("listening", r)));
  try { await fn(); } catch (e) { R.__error = String((e && e.stack) || e); }
  console.log("RESULT:" + JSON.stringify(R));
  T.flushBrowserStores();
  server.close();
  visionServer.close();
  process.exit(0);
}
'''


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_scenario(tmp_path: Path, scenario: str, *, platform: str = "darwin") -> dict:
    script = tmp_path / "scenario.js"
    script.write_text(PRELUDE + scenario, encoding="utf-8")
    env = {
        **os.environ,
        "T_MAIN": str(ELECTRON / "main.js"),
        "T_UI_PORT": str(free_port()),
        "T_PLATFORM": platform,
        "DOURMOUSE_ELECTRON_PANE_PORT": str(free_port()),
    }
    proc = subprocess.run([str(NODE), str(script)], capture_output=True, text=True, timeout=90, env=env, check=False)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:")]
    assert lines, f"the scenario printed no result\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
    data = json.loads(lines[-1][len("RESULT:"):])
    assert "__error" not in data, data["__error"]
    return data
