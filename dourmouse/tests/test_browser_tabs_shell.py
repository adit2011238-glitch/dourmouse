"""Phase B1 (Chrome parity part 1): the Electron shell's tabs, downloads, find, zoom,
print, history and bookmarks, driven for real.

electron/main.js is loaded under plain node with a small fake ``electron`` module
(BrowserView, webContents, session, shell and so on are recording stand-ins), and its
OWN functions and its OWN pane bridge HTTP server are exercised: nothing here
re-implements the logic under test. What this cannot prove is what Chromium does with
the real objects (a real page, a real download, the real find highlight); that is
checked live in an isolated copy of the app, and recorded in the B1 finding.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ELECTRON = Path(__file__).resolve().parents[2] / "electron"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

# The prelude loads main.js with a fake electron and exposes its internals as T. Each
# scenario below is appended to it and ends by printing one JSON line.
PRELUDE = r'''
// Runs electron/main.js under plain node with a fake `electron`, then exposes its
// tab, download and bridge internals so the real code paths are driven, not re-implemented.
const Module = require("module");
const EventEmitter = require("events");
const fs = require("fs");
const os = require("os");
const path = require("path");
const http = require("http");

const MAIN = process.env.T_MAIN;
const DATA = fs.mkdtempSync(path.join(os.tmpdir(), "dm-b1-"));
const DOWNLOADS = path.join(DATA, "downloads");
process.env.DOURMOUSE_USER_DATA_DIR = path.join(DATA, "ud");
process.env.DOURMOUSE_DOWNLOADS_DIR = DOWNLOADS;
process.env.DOURMOUSE_UI_PORT = "8765";

const calls = { openPath: [], showItemInFolder: [], openExternal: [], printed: 0, sent: [] };
let wcSeq = 0;

class FakeSession extends EventEmitter {
  constructor() { super(); this.ua = ""; this.permissionHandlers = 0; }
  setUserAgent(ua) { this.ua = ua; }
  setPermissionRequestHandler() { this.permissionHandlers += 1; }
  setPermissionCheckHandler() { this.permissionHandlers += 1; }
  fetch() { return Promise.reject(new Error("no network in the fake")); }
}
const PANE_SESSION = new FakeSession();

class FakeWC extends EventEmitter {
  constructor(session) {
    super();
    this.id = ++wcSeq; this.url = "about:blank"; this.title = ""; this.destroyed = false; this.zoom = 1;
    this.loads = []; this.finds = []; this.findStops = []; this.reloads = 0; this.sess = session; this.ua = "";
    this.history = []; this.windowOpenHandler = null; this.focused = false;
    this.navigationHistory = { canGoBack: () => false, canGoForward: () => false, goBack() {}, goForward() {} };
  }
  get session() { return this.sess; }
  loadURL(u) { this.url = u; this.loads.push(u); this.emit("did-navigate", {}, u); return Promise.resolve(); }
  getURL() { return this.url; }
  getTitle() { return this.title; }
  isDestroyed() { return this.destroyed; }
  close() { this.destroyed = true; }
  setUserAgent(u) { this.ua = u; }
  setWindowOpenHandler(fn) { this.windowOpenHandler = fn; }
  getZoomFactor() { return this.zoom; }
  setZoomFactor(z) { this.zoom = z; }
  isLoading() { return false; }
  isCurrentlyAudible() { return false; }
  findInPage(q, o) { this.finds.push({ q, ...o }); }
  stopFindInPage(a) { this.findStops.push(a); }
  reload() { this.reloads += 1; }
  stop() {}
  focus() { this.focused = true; }
  print() { calls.printed += 1; }
  printToPDF() { return Promise.resolve(Buffer.from("%PDF-fake")); }
  send(channel, payload) { calls.sent.push([channel, payload]); }
}
class FakeView {
  constructor(opts) { this.opts = opts; this.webContents = new FakeWC(PANE_SESSION); this.bounds = null; }
  setBounds(b) { this.bounds = b; }
  setAutoResize() {}
  setBackgroundColor() {}
}
class FakeWindow {
  constructor() { this.views = []; this.webContents = new FakeWC(new FakeSession()); this.webContents.url = "http://127.0.0.1:8765/"; }
  isDestroyed() { return false; }
  getContentBounds() { return { x: 0, y: 0, width: 1400, height: 900 }; }
  getBrowserViews() { return this.views.slice(); }
  addBrowserView(v) { if (!this.views.includes(v)) this.views.push(v); }
  removeBrowserView(v) { this.views = this.views.filter((x) => x !== v); }
}
const handlers = {};
const fakeElectron = {
  app: {
    setPath() {}, getPath: (n) => (n === "userData" ? path.join(DATA, "ud") : path.join(DATA, n)), getName: () => "Dourmouse",
    commandLine: { appendSwitch() {} }, whenReady: () => new Promise(() => {}), on() {}, isPackaged: false, quit() {},
    dock: { setIcon() {} },
  },
  BrowserWindow: FakeWindow, BrowserView: FakeView,
  ipcMain: { handle: (name, fn) => { handlers[name] = fn; }, on() {} },
  shell: {
    openPath: (p) => { calls.openPath.push(p); return Promise.resolve(""); },
    showItemInFolder: (p) => { calls.showItemInFolder.push(p); },
    openExternal: (u) => { calls.openExternal.push(u); return Promise.resolve(); },
  },
  Tray: class {}, Menu: { buildFromTemplate: () => ({}) }, nativeImage: { createFromDataURL: () => ({}), createFromBuffer: () => ({ isEmpty: () => true }) },
  Notification: class { static isSupported() { return false; } },
  session: { defaultSession: new FakeSession() }, dialog: { showErrorBox() {} },
};
const origLoad = Module._load;
Module._load = function (request, ...rest) {
  if (request === "electron") return fakeElectron;
  return origLoad.call(this, request, ...rest);
};

const hooks = `
module.exports.__t = {
  tabs, closedTabs, MAX_TABS, openTab, closeTab, activateTab, reopenClosedTab, ensurePaneView, showPane, hidePane,
  startPaneBridge, installDownloadHandler, downloadAction, downloadList, paneState, handlePaneShortcut, flushBrowserStores,
  setTabZoom, downloadsDir, listHistory, recordVisit, wireConsoleKeys,
  order: () => tabOrder.slice(), active: () => activeTabId, view: () => paneView, setWindow: (w) => { mainWindow = w; },
  visible: () => paneVisible,
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
      res.on("end", () => { let j = null; try { j = JSON.parse(buf); } catch {} resolve({ status: res.statusCode, body: j }); });
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
const R = {};
async function main(fn) {
  await new Promise((r) => server.once("listening", r));
  try { await fn(); } catch (e) { R.__error = String(e && e.stack || e); }
  console.log("RESULT:" + JSON.stringify(R));
  T.flushBrowserStores();
  server.close();
  process.exit(0);
}
'''


TABS = r'''
main(async () => {
  R.beforeShow = (await call("GET", "/status")).body;
  R.show = (await call("POST", "/show", {})).body;
  let t = (await call("GET", "/tabs")).body;
  R.first = t.tabs;
  const v1 = T.view();
  R.prefs = v1.opts.webPreferences;
  R.ua = v1.webContents.ua;
  R.attached1 = win.getBrowserViews().length;
  R.nav1 = (await call("POST", "/navigate", { url: "https://a.example/" })).status;
  R.loads1 = v1.webContents.loads.slice();
  // a second tab
  const n = (await call("POST", "/tabs/new", { url: "https://b.example/page" })).body;
  R.newTab = n;
  const v2 = T.view();
  R.activeIsNew = v2 !== v1 && T.active() === n.id;
  R.attachedOnlyActive = win.getBrowserViews().length === 1 && win.getBrowserViews()[0] === v2;
  R.prefs2 = v2.opts.webPreferences;
  await call("POST", "/navigate", { url: "https://c.example/" });
  R.navHitsActive = { second: v2.webContents.loads.slice(), firstUntouched: v1.webContents.loads.slice() };
  const sel = await call("POST", "/tabs/select", { id: 1 });
  R.select = sel.body; R.afterSelect = T.active();
  await call("POST", "/navigate", { url: "https://d.example/" });
  R.navAfterSelect = v1.webContents.loads.slice();
  R.selectMissing = (await call("POST", "/tabs/select", { id: 99 })).status;
  R.badNew = [
    (await call("POST", "/tabs/new", { url: "file:///etc/passwd" })).status,
    (await call("POST", "/tabs/new", { url: "javascript:alert(1)" })).status,
    (await call("POST", "/tabs/new", { url: "data:text/html,x" })).status,
  ];
  R.badNavigate = (await call("POST", "/navigate", { url: "file:///etc/passwd" })).status;
  await call("POST", "/reload", {});
  R.reloads = { first: v1.webContents.reloads, second: v2.webContents.reloads };
  const blankTab = (await call("POST", "/tabs/new", {})).body;
  R.newTabLoad = T.tabs.get(blankTab.id).view.webContents.loads;
  R.blankTabsReportNoAddress = (await call("GET", "/tabs")).body.tabs.find((x) => x.id === blankTab.id).url;
  await call("POST", "/tabs/close", { id: blankTab.id });
  R.closedTabsAfterBlank = T.closedTabs.length;
  await call("POST", "/tabs/select", { id: 1 });
  R.originRefused = (await call("GET", "/tabs", undefined, { Origin: "https://evil.example" })).status;
  // window.open and target=_blank
  const opener = v1.webContents;
  const r1 = opener.windowOpenHandler({ url: "https://pop.example/", disposition: "foreground-tab" });
  R.popupDecision = r1;
  R.afterPopup = (await call("GET", "/tabs")).body.tabs.map((x) => [x.id, x.url, x.active]);
  R.badPopup = opener.windowOpenHandler({ url: "file:///etc/passwd", disposition: "foreground-tab" });
  R.afterBadPopup = T.tabs.size;
  const bg = opener.windowOpenHandler({ url: "https://bg.example/", disposition: "background-tab" });
  R.backgroundKeepsActive = T.active();
  // pop-up flood: five per ten seconds per tab
  let opened = 0;
  const before = T.tabs.size;
  for (let i = 0; i < 12; i += 1) opener.windowOpenHandler({ url: "https://flood" + i + ".example/", disposition: "foreground-tab" });
  R.floodOpened = T.tabs.size - before;
  // close: the neighbour takes over, the pane is never empty
  const ids = T.order();
  const activeBefore = T.active();
  const closed = (await call("POST", "/tabs/close", { id: activeBefore })).body;
  R.closeActive = { ok: closed.ok, newActive: T.active(), wasInOrder: ids.includes(activeBefore), stillThere: T.order().includes(activeBefore) };
  R.closedCount = T.closedTabs.length;
  R.reopen = (await call("POST", "/tabs/reopen", {})).body;
  R.reopenedUrl = T.tabs.get(R.reopen.id).view.webContents.url;
  // close everything
  for (const id of T.order()) await call("POST", "/tabs/close", { id });
  R.afterCloseAll = { count: T.tabs.size, url: T.view().webContents.url, active: T.view() ? T.tabs.get(T.active()) !== undefined : false };
  R.reopenEmpty = [];
  // exhaust the reopen list
  let st = 200; let guard = 0;
  while (st === 200 && guard++ < 40) st = (await call("POST", "/tabs/reopen", {})).status;
  R.reopenEmpty = st;
  // limit of tabs
  let limitStatus = 200; guard = 0;
  while (limitStatus === 200 && guard++ < 60) limitStatus = (await call("POST", "/tabs/new", { url: "https://many" + guard + ".example/" })).status;
  R.limit = { status: limitStatus, count: T.tabs.size, max: T.MAX_TABS };
  R.hide = (await call("POST", "/hide", {})).body;
  R.attachedAfterHide = win.getBrowserViews().length;
});
'''

DOWNLOADS = r'''
class FakeItem extends EventEmitter {
  constructor(name, total) { super(); this.name = name; this.total = total; this.recv = 0; this.savePath = ""; this.paused = false; this.cancelled = false; }
  getFilename() { return this.name; } setSavePath(p) { this.savePath = p; } getURL() { return "https://files.example/" + this.name; }
  getMimeType() { return "application/octet-stream"; } getTotalBytes() { return this.total; } getReceivedBytes() { return this.recv; }
  isPaused() { return this.paused; } pause() { this.paused = true; } resume() { this.paused = false; } canResume() { return true; }
  cancel() { this.cancelled = true; this.emit("done", {}, "cancelled"); }
}
function start(name, total, source) {
  const item = new FakeItem(name, total);
  let prevented = false;
  PANE_SESSION.emit("will-download", { preventDefault() { prevented = true; } }, item, source || T.view().webContents);
  return { item, prevented };
}
function finish(item, bytes) {
  fs.writeFileSync(item.savePath, Buffer.alloc(bytes, 65));
  item.recv = bytes;
  item.emit("done", {}, "completed");
}
main(async () => {
  T.ensurePaneView();
  T.installDownloadHandler(PANE_SESSION);
  const d1 = start("../../evil.sh", 10);
  R.savePath = path.relative(DOWNLOADS, d1.item.savePath);
  d1.item.recv = 5; d1.item.emit("updated", {}, "progressing");
  R.midway = (await call("GET", "/downloads")).body.downloads.map((d) => ({ name: d.filename, state: d.state, percent: d.percent, openable: d.openable }));
  finish(d1.item, 10);
  await wait(300);
  const list = (await call("GET", "/downloads")).body.downloads;
  R.done = list.map((d) => ({ name: d.filename, state: d.state, openable: d.openable, quarantined: d.quarantined }));
  R.files = fs.readdirSync(DOWNLOADS).sort();
  R.noAutoOpen = calls.openPath.length === 0 && calls.showItemInFolder.length === 0;
  const idSh = list[0].id;
  R.openScript = await T.downloadAction(idSh, "open");
  R.openPathAfterScript = calls.openPath.length;
  R.reveal = await T.downloadAction(idSh, "reveal");
  R.revealCalls = calls.showItemInFolder.length;
  // a harmless file is opened only on an explicit action
  const d2 = start("notes.txt", 3); finish(d2.item, 3); await wait(300);
  const idTxt = T.downloadList()[0].id;
  R.openCalledBeforeClick = calls.openPath.length;
  R.openTxt = await T.downloadAction(idTxt, "open");
  R.openCalledAfterClick = calls.openPath.length;
  // the same name again never overwrites
  const d3 = start("notes.txt", 3); finish(d3.item, 3); await wait(300);
  R.names = fs.readdirSync(DOWNLOADS).sort();
  // cancel in flight
  const d4 = start("big.bin", 1000);
  const idBig = T.downloadList()[0].id;
  R.cancel = await T.downloadAction(idBig, "cancel");
  await wait(100);
  R.cancelled = T.downloadList()[0].state;
  R.bridgeCannotOpen = (await call("POST", "/downloads/open", { id: idTxt })).status;
  // too many in a minute from one tab
  const results = [];
  const src = T.view().webContents;
  for (let i = 0; i < 12; i += 1) results.push(start("f" + i + ".txt", 1, src).prevented);
  R.refused = results.filter(Boolean).length;
  // remove and clear
  R.remove = await T.downloadAction(idTxt, "remove");
  R.removeUnknown = await T.downloadAction("nope", "remove");
  R.badAction = await T.downloadAction(idSh, "explode");
  // console IPC: only the console window may open or reveal
  const evtOther = { sender: { getURL: () => "https://evil.example/" } };
  R.ipcOpenFromPage = await handlers["pane:download-action"](evtOther, idSh, "reveal");
  R.ipcRevealFromConsole = await handlers["pane:download-action"]({ sender: win.webContents }, idSh, "reveal");
  R.ipcCancelFromAnyone = (await handlers["pane:download-action"](evtOther, "nope", "cancel")).error;
});
'''

MISC = r'''
const darwin = process.platform === "darwin";
const key = (k, extra) => ({ type: "keyDown", key: k, meta: darwin, control: !darwin, shift: false, alt: false, ...(extra || {}) });
main(async () => {
  T.ensurePaneView();
  const tab1 = T.tabs.get(T.active());
  const wc1 = tab1.view.webContents;
  wc1.title = "Alpha";
  wc1.loadURL("https://alpha.example/one");
  wc1.loadURL("https://alpha.example/one"); // a reload inside the dedupe window is one visit
  wc1.loadURL("https://alpha.example/two");
  wc1.loadURL("about:blank"); // never recorded
  R.history = (await call("GET", "/history")).body.history.map((h) => [h.url, h.title]);
  R.historySearch = (await call("GET", "/history?q=two")).body.history.map((h) => h.url);
  R.historyLimit = (await call("GET", "/history?limit=1")).body.history.length;
  const added = (await call("POST", "/history/add", { url: "https://added.example/x", title: "Added" })).body;
  R.historyAddBad = (await call("POST", "/history/add", { url: "javascript:1" })).status;
  R.historyRemove = (await call("POST", "/history/remove", { id: added.entry.id })).body.removed;
  R.historyRemoveNeedsKey = (await call("POST", "/history/remove", {})).status;
  const b1 = (await call("POST", "/bookmarks/add", { url: "https://bm.example/", title: "BM" })).body;
  const b2 = (await call("POST", "/bookmarks/add", { url: "https://bm.example/", title: "BM again" })).body;
  R.bookmarkDup = { first: b1.added, second: b2.added, listed: (await call("GET", "/bookmarks")).body.bookmarks.length };
  R.bookmarkBad = [(await call("POST", "/bookmarks/add", { url: "file:///x" })).status, (await call("POST", "/bookmarks/add", { url: "data:text/html,1" })).status];
  R.bookmarkRemove = (await call("POST", "/bookmarks/remove", { id: b1.bookmark.id })).body.removed;
  await call("POST", "/bookmarks/add", { url: "https://keep.example/", title: "Keep" });
  T.flushBrowserStores();
  const dir = path.join(DATA, "ud", "browser");
  R.files = fs.readdirSync(dir).sort();
  R.onDisk = { history: JSON.parse(fs.readFileSync(path.join(dir, "history.json"), "utf8")).length, bookmarks: JSON.parse(fs.readFileSync(path.join(dir, "bookmarks.json"), "utf8")).map((b) => b.url) };
  R.historyClear = (await call("POST", "/history/clear", {})).body.removed > 0;
  R.afterClear = (await call("GET", "/history")).body.history.length;
  wc1.loadURL("https://zoom.example/a");
  R.zoomIn = await handlers["pane:zoom"]({}, "in");
  R.zoomIn2 = await handlers["pane:zoom"]({}, "in");
  R.zoomBad = await handlers["pane:zoom"]({}, "sideways");
  const second = (await call("POST", "/tabs/new", { url: "https://zoom.example/b" })).body;
  const wc2 = T.tabs.get(second.id).view.webContents;
  R.zoomRemembered = wc2.zoom;
  const other = (await call("POST", "/tabs/new", { url: "https://other.example/" })).body;
  R.zoomOtherSite = T.tabs.get(other.id).view.webContents.zoom;
  R.zoomStateOnActive = (await handlers["pane:state"]()).zoom;
  await call("POST", "/tabs/select", { id: second.id });
  R.zoomReset = await handlers["pane:zoom"]({}, "reset");
  T.flushBrowserStores();
  R.zoomFile = JSON.parse(fs.readFileSync(path.join(dir, "zoom.json"), "utf8"));
  const activeWc = T.view().webContents;
  R.findBlank = await handlers["pane:find"]({}, "", {});
  await handlers["pane:find"]({}, "needle", { forward: true, findNext: false });
  await handlers["pane:find"]({}, "needle", { forward: true, findNext: true });
  await handlers["pane:find"]({}, "needle", { forward: false, findNext: true });
  await handlers["pane:find"]({}, "other word", { forward: true, findNext: false });
  R.finds = activeWc.finds;
  activeWc.emit("found-in-page", {}, { activeMatchOrdinal: 2, matches: 7 });
  R.findState = (await handlers["pane:state"]()).find;
  await handlers["pane:find-stop"]();
  R.findStops = activeWc.findStops;
  R.findAfterStop = (await handlers["pane:state"]()).find;
  await handlers["pane:find"]({}, "x", {});
  await call("POST", "/tabs/select", { id: tab1.id });
  R.findClearedOnSwitch = activeWc.findStops.length;
  const printed = await handlers["pane:print"]({}, undefined);
  R.print = { ok: printed.ok, dialogs: calls.printed };
  const pdf = await handlers["pane:print"]({}, { pdf: true });
  R.pdf = { ok: pdf.ok, inDownloads: pdf.path ? path.relative(DOWNLOADS, pdf.path) : null, bytes: pdf.path ? fs.statSync(pdf.path).size : 0, openedByItself: calls.openPath.length };
  const before = T.tabs.size;
  const act = T.tabs.get(T.active());
  R.keys = {};
  R.keys.newTab = T.handlePaneShortcut(act, key("t")) && T.tabs.size === before + 1;
  const newId = T.active();
  R.keys.closeTab = T.handlePaneShortcut(T.tabs.get(newId), key("w")) && !T.tabs.has(newId);
  R.keys.reopen = T.handlePaneShortcut(T.tabs.get(T.active()), key("t", { shift: true }));
  R.keys.sizeAfterReopen = [before, T.tabs.size];
  R.keys.pageOwnsCopy = ["c", "v", "x", "a", "z"].map((k) => T.handlePaneShortcut(T.tabs.get(T.active()), key(k)));
  R.keys.noMod = T.handlePaneShortcut(T.tabs.get(T.active()), { type: "keyDown", key: "t", meta: false, control: false, shift: false, alt: false });
  const order = T.order();
  T.handlePaneShortcut(T.tabs.get(T.active()), key("1"));
  R.keys.cmd1 = T.active() === order[0];
  T.handlePaneShortcut(T.tabs.get(T.active()), key("9"));
  R.keys.cmd9 = T.active() === order[order.length - 1];
  T.handlePaneShortcut(T.tabs.get(T.active()), { type: "keyDown", key: "Tab", control: true, meta: false, shift: false, alt: false });
  R.keys.ctrlTabWraps = T.active() === order[0];
  const z = T.view().webContents.zoom;
  T.handlePaneShortcut(T.tabs.get(T.active()), key("="));
  R.keys.zoomKey = T.view().webContents.zoom > z;
  T.handlePaneShortcut(T.tabs.get(T.active()), key("0"));
  R.keys.zoomResetKey = T.view().webContents.zoom;
  T.handlePaneShortcut(T.tabs.get(T.active()), key("f"));
  R.keys.findAsksConsole = calls.sent.filter((s) => s[0] === "pane:command").map((s) => s[1]);
  R.ipcNew = (await handlers["pane:tab-new"]({}, undefined)).ok;
  R.ipcNewBad = (await handlers["pane:tab-new"]({}, "file:///etc/hosts"));
  R.ipcSelectMissing = await handlers["pane:tab-select"]({}, 12345);
  R.ipcNavigateBad = await handlers["pane:navigate"]({}, "javascript:alert(1)");
  R.ipcNavigateGood = await handlers["pane:navigate"]({}, "https://nav.example/");
  R.ipcNavigatedActive = T.view().webContents.url;
  R.stateKeys = Object.keys(await handlers["pane:state"]()).sort();
});
'''

CONSOLE_KEYS = r'''
const darwin = process.platform === "darwin";
main(async () => {
  T.ensurePaneView();
  await call("POST", "/tabs/new", { url: "https://two.example/" });
  T.wireConsoleKeys(win);
  const press = (extra) => {
    let prevented = false;
    win.webContents.emit("before-input-event", { preventDefault() { prevented = true; } }, { type: "keyDown", key: "w", meta: darwin, control: !darwin, shift: false, alt: false, ...(extra || {}) });
    return prevented;
  };
  R.beforeScreen = [press(), T.tabs.size];
  const page = { sender: { getURL: () => "https://evil.example/" } };
  await handlers["pane:screen"](page, true);
  R.fromAPage = [press(), T.tabs.size];
  await handlers["pane:screen"]({ sender: win.webContents }, true);
  R.shift = [press({ shift: true }), T.tabs.size];
  R.alt = [press({ alt: true }), T.tabs.size];
  R.keyUp = [press({ type: "keyUp" }), T.tabs.size];
  R.other = [press({ key: "x" }), T.tabs.size];
  R.closes = [press(), T.tabs.size];
  await handlers["pane:screen"]({ sender: win.webContents }, false);
  R.afterLeaving = [press(), T.tabs.size];
});
'''

CRASH = r'''
main(async () => {
  T.ensurePaneView();
  const tab = T.tabs.get(T.active());
  const wc = tab.view.webContents;
  await call("POST", "/navigate", { url: "https://page.example/" });
  const loadsBefore = wc.loads.length;
  wc.emit("render-process-gone", {}, { reason: "crashed" });
  R.errorShown = (await call("GET", "/tabs")).body.tabs[0].error;
  await wait(500);
  R.recovered = wc.loads.slice(loadsBefore);
  // a crash before any page was asked for comes back as a blank tab
  wc.emit("render-process-gone", {}, { reason: "oom" });
  await wait(500);
  R.second = wc.loads.slice(loadsBefore + 1);
  // the budget is two a minute: the third is left showing the error
  wc.emit("render-process-gone", {}, { reason: "crashed" });
  await wait(500);
  R.third = wc.loads.slice(loadsBefore + 2);
  R.stillError = (await call("GET", "/tabs")).body.tabs[0].error;
  // killed and clean exits are somebody's decision
  const t2 = (await call("POST", "/tabs/new", { url: "https://other.example/" })).body;
  const w2 = T.tabs.get(t2.id).view.webContents;
  const n2 = w2.loads.length;
  w2.emit("render-process-gone", {}, { reason: "killed" });
  w2.emit("render-process-gone", {}, { reason: "clean-exit" });
  await wait(500);
  R.notRecovered = w2.loads.length - n2;
  // a tab closed while it was waiting to recover is left alone
  const t3 = (await call("POST", "/tabs/new", { url: "https://third.example/" })).body;
  const w3 = T.tabs.get(t3.id).view.webContents;
  const n3 = w3.loads.length;
  w3.emit("render-process-gone", {}, { reason: "crashed" });
  await call("POST", "/tabs/close", { id: t3.id });
  await wait(500);
  R.closedMeanwhile = w3.loads.length - n3;
});
'''


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run(tmp_path: Path, scenario: str) -> dict:
    script = tmp_path / "scenario.js"
    script.write_text(PRELUDE + scenario, encoding="utf-8")
    env = {**os.environ, "T_MAIN": str(ELECTRON / "main.js"), "DOURMOUSE_ELECTRON_PANE_PORT": str(_free_port())}
    proc = subprocess.run([str(NODE), str(script)], capture_output=True, text=True, timeout=90, env=env, check=False)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:")]
    assert lines, f"the scenario printed no result\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
    data = json.loads(lines[-1][len("RESULT:") :])
    assert "__error" not in data, data["__error"]
    return data


@pytest.fixture(scope="module")
def tabs(tmp_path_factory):
    return _run(tmp_path_factory.mktemp("tabs"), TABS)


@pytest.fixture(scope="module")
def dl(tmp_path_factory):
    return _run(tmp_path_factory.mktemp("dl"), DOWNLOADS)


@pytest.fixture(scope="module")
def misc(tmp_path_factory):
    return _run(tmp_path_factory.mktemp("misc"), MISC)


class TestTabs:
    def test_the_pane_starts_with_one_blank_tab_and_none_before_it_is_used(self, tabs):
        assert tabs["beforeShow"]["tabCount"] == 0
        assert tabs["show"] == {"ok": True}
        (first,) = tabs["first"]
        assert first["id"] == 1 and first["active"] is True and first["url"] == ""

    def test_every_tab_is_built_the_same_safe_way(self, tabs):
        want = {"contextIsolation": True, "partition": "persist:dourmouse-browser"}
        assert tabs["prefs"] == want and tabs["prefs2"] == want  # the shared profile, no preload, isolation on
        assert "Electron" not in tabs["ua"] and "Chrome/" in tabs["ua"]

    def test_the_old_bridge_routes_drive_the_active_tab_only(self, tabs):
        assert tabs["nav1"] == 200
        assert tabs["activeIsNew"] is True
        assert tabs["navHitsActive"]["second"][-1] == "https://c.example/"
        assert "https://c.example/" not in tabs["navHitsActive"]["firstUntouched"]
        assert tabs["afterSelect"] == 1
        assert tabs["navAfterSelect"][-1] == "https://d.example/"
        assert tabs["reloads"] == {"first": 1, "second": 0}

    def test_only_the_active_tabs_view_is_on_the_window(self, tabs):
        assert tabs["attached1"] == 1
        assert tabs["attachedOnlyActive"] is True
        assert tabs["attachedAfterHide"] == 0

    def test_a_new_tab_is_not_the_agents_about_blank_anchor(self, tabs):
        # browser_agent.py attaches to the one page whose address is about:blank
        load = tabs["newTabLoad"][0]
        assert load.startswith("data:text/html") and load.endswith("#dm-newtab")
        assert tabs["blankTabsReportNoAddress"] == ""
        assert tabs["closedTabsAfterBlank"] == 0  # an empty tab is not worth reopening

    def test_only_real_web_addresses_open(self, tabs):
        assert tabs["badNew"] == [400, 400, 400]
        assert tabs["badNavigate"] == 400
        assert tabs["selectMissing"] == 404

    def test_a_browser_page_cannot_use_the_bridge(self, tabs):
        assert tabs["originRefused"] == 403

    def test_window_open_makes_a_tab_next_to_its_opener_and_only_for_web_addresses(self, tabs):
        assert tabs["popupDecision"] == {"action": "deny"}  # the engine never makes its own window
        urls = [row[1] for row in tabs["afterPopup"]]
        assert urls[:2] == ["https://d.example/", "https://pop.example/"]
        assert tabs["afterPopup"][1][2] is True  # a foreground pop-up takes focus
        assert tabs["badPopup"] == {"action": "deny"} and tabs["afterBadPopup"] == 3
        assert tabs["backgroundKeepsActive"] == tabs["afterPopup"][1][0]  # a background tab does not steal focus

    def test_a_page_that_opens_tabs_in_a_loop_is_cut_off(self, tabs):
        assert tabs["floodOpened"] == 3  # five per ten seconds in all, two were already used

    def test_closing_the_active_tab_hands_over_and_it_can_be_reopened(self, tabs):
        close = tabs["closeActive"]
        assert close["ok"] is True and close["wasInOrder"] and not close["stillThere"]
        assert close["newActive"] != 0
        assert tabs["closedCount"] == 1
        assert tabs["reopen"]["ok"] is True
        assert tabs["reopenedUrl"] == "https://flood2.example/"

    def test_the_pane_is_never_left_empty_and_the_fresh_tab_is_the_agents_anchor(self, tabs):
        assert tabs["afterCloseAll"] == {"count": 1, "url": "about:blank", "active": True}
        assert tabs["reopenEmpty"] == 404

    def test_the_number_of_tabs_is_bounded(self, tabs):
        assert tabs["limit"] == {"status": 409, "count": 30, "max": 30}


@pytest.fixture(scope="module")
def console_keys(tmp_path_factory):
    return _run(tmp_path_factory.mktemp("console_keys"), CONSOLE_KEYS)


class TestCmdWOnTheConsole:
    def test_cmd_w_closes_the_tab_only_while_the_browser_screen_is_showing_and_says_so_from_the_console(self, console_keys):
        assert console_keys["beforeScreen"] == [False, 2]  # another screen: the window's own Cmd+W
        assert console_keys["fromAPage"] == [False, 2]  # a web page cannot switch this on
        assert console_keys["closes"] == [True, 1]
        assert console_keys["afterLeaving"] == [False, 1]

    def test_it_claims_nothing_but_the_plain_chord(self, console_keys):
        for name in ("shift", "alt", "keyUp", "other"):
            assert console_keys[name] == [False, 2], name


@pytest.fixture(scope="module")
def crash(tmp_path_factory):
    return _run(tmp_path_factory.mktemp("crash"), CRASH)


class TestACrashedTab:
    def test_the_error_is_shown_and_a_fresh_process_is_started_on_the_same_address(self, crash):
        assert crash["errorShown"]["description"] == "The page process ended (crashed)"
        assert crash["recovered"] == ["https://page.example/"]
        assert crash["second"] == ["https://page.example/"]

    def test_a_tab_that_keeps_crashing_stops_being_restarted_and_keeps_its_error(self, crash):
        assert crash["third"] == []
        assert crash["stillError"]["code"] == -1

    def test_a_renderer_that_was_killed_or_exited_cleanly_is_left_alone(self, crash):
        assert crash["notRecovered"] == 0

    def test_a_tab_closed_while_waiting_to_recover_is_not_touched(self, crash):
        assert crash["closedMeanwhile"] == 0


class TestDownloads:
    def test_a_hostile_file_name_is_saved_inside_downloads_as_a_partial_file_first(self, dl):
        assert dl["savePath"] == "evil.sh.crdownload"
        assert dl["midway"] == [{"name": "evil.sh", "state": "progressing", "percent": 50, "openable": False}]

    def test_it_finishes_under_its_real_name_and_nothing_opens_by_itself(self, dl):
        assert dl["files"] == ["evil.sh"]
        (done,) = dl["done"]
        assert done["state"] == "completed" and done["openable"] is False
        assert done["quarantined"] in (True, False)  # the flag is written on macOS; elsewhere it says it was not
        assert dl["noAutoOpen"] is True

    def test_a_file_that_runs_code_is_never_opened_only_shown(self, dl):
        assert dl["openScript"]["ok"] is False and "run code" in dl["openScript"]["error"]
        assert dl["openPathAfterScript"] == 0
        assert dl["reveal"] == {"ok": True} and dl["revealCalls"] == 1

    def test_a_harmless_file_opens_only_on_an_explicit_request(self, dl):
        assert dl["openCalledBeforeClick"] == 0
        assert dl["openTxt"] == {"ok": True}
        assert dl["openCalledAfterClick"] == 1

    def test_a_second_download_of_the_same_name_never_overwrites(self, dl):
        assert dl["names"] == ["evil.sh", "notes (1).txt", "notes.txt"]

    def test_a_running_download_can_be_cancelled(self, dl):
        assert dl["cancel"] == {"ok": True} and dl["cancelled"] == "cancelled"

    def test_the_bridge_can_list_and_cancel_but_has_no_way_to_open(self, dl):
        assert dl["bridgeCannotOpen"] == 404

    def test_a_page_that_starts_downloads_in_a_loop_is_refused(self, dl):
        assert dl["refused"] == 6  # ten a minute per tab, four were already used

    def test_remove_and_bad_requests_are_answered_plainly(self, dl):
        assert dl["remove"] == {"ok": True}
        assert dl["removeUnknown"]["ok"] is False
        assert dl["badAction"] == {"ok": False, "error": "unknown action"}

    def test_only_the_console_window_may_open_or_reveal(self, dl):
        assert dl["ipcOpenFromPage"] == {"ok": False, "error": "only the console may do that"}
        assert dl["ipcRevealFromConsole"] == {"ok": True}
        assert dl["ipcCancelFromAnyone"] == "no such download"  # cancel is allowed, this id just does not exist


class TestRecordsFindZoomPrint:
    def test_history_records_visits_once_and_never_blank_pages(self, misc):
        assert misc["history"] == [["https://alpha.example/two", "Alpha"], ["https://alpha.example/one", "Alpha"]]
        assert misc["historySearch"] == ["https://alpha.example/two"]
        assert misc["historyLimit"] == 1

    def test_history_add_remove_and_clear(self, misc):
        assert misc["historyAddBad"] == 400
        assert misc["historyRemove"] == 1
        assert misc["historyRemoveNeedsKey"] == 400
        assert misc["historyClear"] is True and misc["afterClear"] == 0

    def test_bookmarks_dedupe_by_address_and_refuse_non_web_addresses(self, misc):
        assert misc["bookmarkDup"] == {"first": True, "second": False, "listed": 1}
        assert misc["bookmarkBad"] == [400, 400]
        assert misc["bookmarkRemove"] == 1

    def test_the_records_are_json_files_in_the_user_data_folder(self, misc):
        assert misc["files"] == ["bookmarks.json", "history.json", "zoom.json"]
        assert misc["onDisk"] == {"history": 2, "bookmarks": ["https://keep.example/"]}

    def test_zoom_steps_like_chrome_is_remembered_per_site_and_resets(self, misc):
        assert misc["zoomIn"] == {"ok": True, "zoom": 1.1} and misc["zoomIn2"] == {"ok": True, "zoom": 1.25}
        assert misc["zoomBad"]["ok"] is False
        assert misc["zoomRemembered"] == 1.25  # another tab on the same site
        assert misc["zoomOtherSite"] == 1  # a different site
        assert misc["zoomReset"] == {"ok": True, "zoom": 1}
        assert misc["zoomFile"] == {}  # back to 100 percent forgets the site

    def test_find_starts_a_session_for_a_new_word_and_steps_through_the_same_word(self, misc):
        steps = [(f["q"], f["forward"], f["findNext"]) for f in misc["finds"]]
        # Electron's findNext TRUE means "begin a new search"; stepping uses FALSE
        assert steps[:4] == [("needle", True, True), ("needle", True, False), ("needle", False, False), ("other word", True, True)]
        assert misc["findBlank"] == {"ok": True}
        assert misc["findState"] == {"text": "other word", "active": 2, "matches": 7}

    def test_find_clears_when_stopped_and_when_the_tab_changes(self, misc):
        assert misc["findStops"][0] == "clearSelection"
        assert misc["findAfterStop"] is None
        assert misc["findClearedOnSwitch"] >= 3

    def test_print_opens_the_dialog_and_pdf_lands_in_downloads_without_opening(self, misc):
        assert misc["print"] == {"ok": True, "dialogs": 1}
        assert misc["pdf"] == {"ok": True, "inDownloads": "Alpha.pdf", "bytes": 9, "openedByItself": 0}

    def test_chrome_shortcuts_work_while_the_page_has_the_keyboard_and_leave_the_pages_own_keys_alone(self, misc):
        keys = misc["keys"]
        assert keys["newTab"] and keys["closeTab"] and keys["reopen"]
        assert keys["sizeAfterReopen"][0] == keys["sizeAfterReopen"][1]
        assert keys["pageOwnsCopy"] == [False] * 5
        assert keys["noMod"] is False
        assert keys["cmd1"] and keys["cmd9"] and keys["ctrlTabWraps"] and keys["zoomKey"]
        assert keys["zoomResetKey"] == 1
        assert keys["findAsksConsole"] == ["find"]

    def test_the_console_ipc_refuses_what_the_bridge_refuses(self, misc):
        assert misc["ipcNew"] is True
        assert misc["ipcNewBad"] == {"ok": False, "error": "url must be a real http(s) URL"}
        assert misc["ipcSelectMissing"] == {"ok": False}
        assert misc["ipcNavigateBad"]["ok"] is False
        assert misc["ipcNavigateGood"] == {"ok": True} and misc["ipcNavigatedActive"] == "https://nav.example/"

    def test_the_state_the_console_gets_names_everything_the_strip_needs(self, misc):
        for field in ("tabs", "activeTab", "zoom", "find", "closedTabs", "blockedPopups", "url", "title", "canGoBack", "canGoForward"):
            assert field in misc["stateKeys"]


@pytest.mark.skipif(sys.platform == "win32", reason="the harness paths assume a posix shell")
def test_the_harness_really_ran_main_js(tabs):
    assert tabs["limit"]["max"] == 30  # read from the module itself, not restated here
