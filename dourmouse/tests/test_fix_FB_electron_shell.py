"""Fix agent FB: the Electron shell findings A-1, A-2, A-6, A-7, A-8, A-9, A-10, A-11, A-12 and A-4.

electron/main.js is loaded under plain node with a fake ``electron`` (the B3 harness, patched
here with a window that records what it is asked, a tray that keeps its menu, an error box that
keeps what it shows, and hooks into main.js). The functions under test are main.js's OWN. What this
cannot prove is what Electron and macOS do with them; that was looked at live in an isolated copy of
the app (recorded in the FB results).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from dourmouse.tests import b3_harness
from dourmouse.tests.b2_harness import ELECTRON, NODE, free_port
from dourmouse.tests.test_browser_import_policy import make_chrome_profile

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

REPO = ELECTRON.parent
MAIN_CODE = (ELECTRON / "main.js").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# the harness
# --------------------------------------------------------------------------- #


def _patch(src: str, old: str, new: str) -> str:
    assert old in src, f"FB harness: the B3 harness no longer contains {old[:70]!r}"
    return src.replace(old, new, 1)


def _prelude() -> str:
    src = b3_harness.PRELUDE
    src = _patch(src, "class FakeWindow {", "const allWindows = [];\nconst appEvents = {};\nconst trayInstances = [];\nclass FakeWindow extends EventEmitter {")
    src = _patch(src, "constructor() { this.views = [];", "constructor(opts) { super(); this.opts = opts || {}; this.destroyed = false; this.minimized = false; this.loads = []; this.acts = []; allWindows.push(this); this.views = [];")
    src = _patch(src, "  isDestroyed() { return false; }\n", r'''  isDestroyed() { return this.destroyed; }
  loadURL(u) { this.loads.push(u); this.webContents.url = u; return Promise.resolve(); }
  maximize() { this.acts.push("maximize"); }
  isMaximized() { return false; }
  getBounds() { return { x: 0, y: 0, width: 1400, height: 900 }; }
  isMinimized() { return this.minimized; }
  restore() { this.minimized = false; this.acts.push("restore"); }
  show() { this.acts.push("show"); }
  focus() { this.acts.push("focus"); }
  destroy() { this.destroyed = true; this.emit("closed"); }
  hide() { this.acts.push("hide"); this.hidden = true; }
''')
    src = _patch(src, "const handlers = {};", "FakeWindow.getAllWindows = () => allWindows.filter((w) => !w.destroyed);\nconst handlers = {};")
    src = _patch(src, "Tray: class {},", "Tray: class { constructor() { trayInstances.push(this); this.menu = null; this.tip = ''; } setImage() {} setToolTip(t) { this.tip = t; } setContextMenu(m) { this.menu = m; } },")
    src = _patch(src, "nativeImage: { createFromDataURL: () => ({}),", "nativeImage: { createFromDataURL: () => ({ resize() { return {}; } }),")
    src = _patch(src, "commandLine: { appendSwitch() {} }, whenReady: () => new Promise(() => {}), on() {},", "commandLine: { appendSwitch() {} }, whenReady: () => new Promise(() => {}), on(ev, fn) { appEvents[ev] = fn; },")
    src = _patch(src, "showErrorBox() {},", "showErrorBox(t, m) { calls.errors.push([t, m]); },")
    src = _patch(src, "const calls = { sent: [], menus: [], dialogs: [], asked: [], openExternal: [], opens: [] };", "const calls = { sent: [], menus: [], dialogs: [], asked: [], openExternal: [], opens: [], errors: [] };")
    hooks = r'''  closeAllTabs, extensionsDir, browserDataDir, tabPrefs: () => TAB_WEB_PREFERENCES, drmReport,
  fn: (n) => { try { return eval(n); } catch (_e) { return undefined; } },
  getMainWindow: () => mainWindow, taskWindows, makeStore, serverPidFile, setServerProcess: (p) => { serverProcess = p; },
  extSessionList, paneSession, base_url: BASE_URL, port: PORT,'''
    src = _patch(src, "  closeAllTabs, extensionsDir, browserDataDir, tabPrefs: () => TAB_WEB_PREFERENCES, drmReport,", hooks)
    return src


PRELUDE = _prelude()


def run_scenario(tmp_path: Path, scenario: str, *, main: Path | None = None, extra_env: dict | None = None, data: Path | None = None) -> dict:
    script = tmp_path / "scenario.js"
    script.write_text(PRELUDE + scenario, encoding="utf-8")
    env = {
        **os.environ,
        "T_MAIN": str(main or (ELECTRON / "main.js")),
        "T_UI_PORT": str(free_port()),
        "T_PLATFORM": "darwin",
        "DOURMOUSE_ELECTRON_PANE_PORT": str(free_port()),
        **({"T_DATA": str(data)} if data else {}),
        **(extra_env or {}),
    }
    proc = subprocess.run([str(NODE), str(script)], capture_output=True, text=True, timeout=120, env=env, check=False)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:")]
    assert lines, f"the scenario printed no result\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
    out = json.loads(lines[-1][len("RESULT:"):])
    assert "__error" not in out, out["__error"]
    return out


# --------------------------------------------------------------------------- #
# A-1: the Dock "activate" recreates the console, wired like the first one
# --------------------------------------------------------------------------- #


def test_activate_no_longer_counts_hidden_windows():
    assert "BrowserWindow.getAllWindows().length === 0" not in MAIN_CODE
    assert 'app.on("activate", onActivate)' in MAIN_CODE
    assert MAIN_CODE.count("title: \"DOURMOUSE // CENTRAL AGENT DISPATCH\"") == 1, "one place builds the console window"


A1 = r'''
const fsx = require("fs");
main(async () => {
  fsx.mkdirSync(USERDATA, { recursive: true });
  fsx.writeFileSync(path.join(USERDATA, "window-state.json"), JSON.stringify({ x: 40, y: 50, width: 1111, height: 777, maximized: true }));
  T.ensurePaneView();
  T.showPane(); // the pane is open
  const view = T.tabs.get(T.active()).view;
  // the hidden map and ATLAS windows, alive for the whole run
  new FakeWindow({ show: false }); new FakeWindow({ show: false });
  const createMainWindow = T.fn("createMainWindow"), onActivate = T.fn("onActivate");
  R.have = [typeof createMainWindow, typeof onActivate];
  const w1 = createMainWindow();
  R.first = { opts: { w: w1.opts.width, h: w1.opts.height, x: w1.opts.x, y: w1.opts.y, title: w1.opts.title }, loads: w1.loads.slice(), acts: w1.acts.slice(), isMain: T.getMainWindow() === w1, paneOnIt: w1.views.includes(view) };
  // the owner presses the red button: the console is hidden, not destroyed, and the pane stays on it
  const evt = { prevented: false, preventDefault() { this.prevented = true; } };
  w1.emit("close", evt);
  R.close = { prevented: evt.prevented, hidden: w1.acts.includes("hide"), destroyed: w1.destroyed, viewStillOnIt: w1.views.includes(view), liveWindows: allWindows.filter((w) => !w.destroyed).length };
  // the Dock icon: the same console comes back, nothing new is built
  const made = allWindows.length;
  onActivate();
  R.activate = { made: allWindows.length - made, same: T.getMainWindow() === w1, acts: w1.acts.slice(-2) };
  // a minimized console is restored too
  w1.minimized = true; onActivate();
  R.minimized = { acts: w1.acts.slice(-3), made: allWindows.length - made };
  // the pane follows the console when it is resized
  w1.getContentBounds = () => ({ x: 0, y: 0, width: 2000, height: 1200 });
  w1.emit("resize");
  R.afterResize = view.bounds;
  // a real quit is not blocked
  appEvents["before-quit"]();
  const evt2 = { prevented: false, preventDefault() { this.prevented = true; } };
  w1.emit("close", evt2);
  R.quit = { prevented: evt2.prevented };
});
'''


def test_the_console_is_hidden_not_destroyed_and_the_dock_brings_it_back(tmp_path):
    out = run_scenario(tmp_path, A1)
    assert out["have"] == ["function", "function"]
    first = out["first"]
    assert first["isMain"] and first["opts"] == {"w": 1111, "h": 777, "x": 40, "y": 50, "title": "DOURMOUSE // CENTRAL AGENT DISPATCH"}
    assert "maximize" in first["acts"] and first["paneOnIt"] and first["loads"][0].startswith("http://127.0.0.1:")
    close = out["close"]
    assert close["prevented"] is True and close["hidden"] is True and close["destroyed"] is False
    assert close["viewStillOnIt"] is True, "the pane's views must stay on the console: destroying it destroyed them"
    assert close["liveWindows"] >= 2, "the hidden map and ATLAS windows are alive, which is why 'no windows' was never true"
    assert out["activate"] == {"made": 0, "same": True, "acts": ["show", "focus"]}
    assert "restore" in out["minimized"]["acts"] and out["minimized"]["made"] == 0
    assert out["afterResize"]["width"] > 0 and out["afterResize"]["x"] + out["afterResize"]["width"] <= 2000
    assert out["quit"]["prevented"] is False, "quitting must still close the console"


A1B = r'''
main(async () => {
  T.ensurePaneView();
  T.showPane();
  const oldView = T.tabs.get(T.active()).view;
  const createMainWindow = T.fn("createMainWindow"), onActivate = T.fn("onActivate");
  const w1 = createMainWindow();
  // the console was destroyed anyway (a crash, or by script) and the views died with it
  w1.destroy();
  const realAdd = FakeWindow.prototype.addBrowserView;
  let thrown = 0;
  FakeWindow.prototype.addBrowserView = function (v) { if (v === oldView && !thrown) { thrown += 1; throw new Error("Can't add a destroyed child view to a parent view"); } return realAdd.call(this, v); };
  const made = allWindows.length;
  let err = null;
  try { onActivate(); } catch (e) { err = String(e.message || e); }
  const w2 = T.getMainWindow();
  R.out = { err, thrown, made: allWindows.length - made, w2New: w2 !== w1 && !w2.destroyed, tabs: T.tabs.size, freshView: [...T.tabs.values()][0].view !== oldView, onNewWindow: w2.views.includes([...T.tabs.values()][0].view) };
});
'''


def test_a_console_rebuilt_over_dead_pane_views_opens_a_fresh_pane_instead_of_stopping_the_app(tmp_path):
    out = run_scenario(tmp_path, A1B)["out"]
    assert out["err"] is None and out["thrown"] == 1
    assert out["made"] == 1 and out["w2New"]
    assert out["tabs"] == 1 and out["freshView"] and out["onNewWindow"]


# --------------------------------------------------------------------------- #
# A-2 and A-10: the bridge opens STUDY and PROJECT windows, and builds agent URLs safely
# --------------------------------------------------------------------------- #

A2 = r'''
main(async () => {
  const evt = consoleEvt();
  const opened = () => [...T.taskWindows.entries()].map(([id, w]) => [id, w.loads[0] || "", w.opts.title]);
  R.study = await handlers["bridge:open_study"]?.(evt);
  R.studyAgain = await handlers["bridge:open_study"]?.(evt);
  R.project = await handlers["bridge:open_project"]?.(evt, "project-0123456789abcdef", "My Notes & Things");
  R.projectBad = [];
  for (const bad of ["", "../x", "a b", "a/b", "p?x=1", "p#h", "x".repeat(65), null, 5]) R.projectBad.push(await handlers["bridge:open_project"]?.(evt, bad, "n"));
  R.agent = await handlers["bridge:open_agent"](evt, "agent_smith");
  R.agentBad = [];
  for (const bad of ["../api/x?y=1", "a#b", "a/b", "a?b", "mail%2f..", "x".repeat(41), "", "  ", "a b"]) R.agentBad.push(await handlers["bridge:open_agent"](evt, bad));
  R.windows = opened();
  R.count = T.taskWindows.size;
  R.base = T.base_url;
});
'''


def test_study_and_project_open_their_own_windows_and_agent_urls_are_safe(tmp_path):
    out = run_scenario(tmp_path, A2)
    base = out["base"]
    assert out["study"] is True and out["studyAgain"] is True
    assert out["project"] is True
    assert out["projectBad"] == [False] * 9
    assert out["agent"] is True
    assert out["agentBad"] == [False] * 9
    by_id = {w[0]: w for w in out["windows"]}
    assert by_id["study"][1] == f"{base}/study" and by_id["study"][2] == "STUDY"
    assert by_id["project:project-0123456789abcdef"][1] == f"{base}/?project=project-0123456789abcdef"
    assert by_id["project:project-0123456789abcdef"][2].startswith("PROJECT // MY NOTES")
    assert by_id["agent_smith"][1] == f"{base}/agent/agent_smith"
    assert out["count"] == 3, "the second STUDY click reuses the window; no refused call opened anything"


def test_the_preload_exposes_what_the_console_feature_detects(tmp_path):
    script = tmp_path / "pre.js"
    script.write_text(
        r'''
const Module = require("module");
const exposed = {}; const invoked = [];
const fake = { contextBridge: { exposeInMainWorld: (n, v) => { exposed[n] = v; } }, ipcRenderer: { invoke: (...a) => { invoked.push(a); return Promise.resolve(true); }, on() {}, removeListener() {} } };
const orig = Module._load;
Module._load = function (r, ...rest) { return r === "electron" ? fake : orig.call(this, r, ...rest); };
require(process.argv[2]);
const api = exposed.pywebview.api;
Promise.all([api.open_study(), api.open_project("project-abc", "Name"), api.open_agent("mail")]).then(() => {
  console.log(JSON.stringify({ names: Object.keys(api).sort(), invoked }));
});
''',
        encoding="utf-8",
    )
    proc = subprocess.run([NODE, str(script), str(ELECTRON / "preload.js")], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    assert got["names"] == ["open_agent", "open_all_hands", "open_external", "open_project", "open_study"]
    assert ["bridge:open_study"] in got["invoked"]
    assert ["bridge:open_project", "project-abc", "Name"] in got["invoked"]
    console = (REPO / "ui" / "console.html").read_text(encoding="utf-8")
    assert "window.pywebview.api.open_study" in console and "window.pywebview.api.open_project" in console


# --------------------------------------------------------------------------- #
# A-12: a corrupt store whose rename fails still starts empty
# --------------------------------------------------------------------------- #

A12 = r'''
const fsx = require("fs");
main(async () => {
  const dir = path.join(USERDATA, "browser");
  fsx.mkdirSync(dir, { recursive: true });
  fsx.writeFileSync(path.join(dir, "perm-test.json"), "{ not json");
  fsx.writeFileSync(path.join(dir, "ok-test.json"), "{ not json either");
  const realRename = fsx.renameSync;
  fsx.renameSync = (a, b) => { if (String(a).endsWith("perm-test.json")) throw Object.assign(new Error("EROFS: read-only file system"), { code: "EROFS" }); return realRename(a, b); };
  try {
    R.threw = null;
    try { R.value = T.makeStore("perm-test.json", { a: 1 }).get(); } catch (e) { R.threw = String(e.message || e); }
    R.stillThere = fsx.existsSync(path.join(dir, "perm-test.json"));
    // the normal case is unchanged: the corrupt file is kept beside it
    R.okValue = T.makeStore("ok-test.json", []).get();
    R.aside = fsx.readdirSync(dir).filter((f) => f.startsWith("ok-test.json.corrupt-")).length;
  } finally { fsx.renameSync = realRename; }
});
'''


def test_a_corrupt_store_on_a_volume_that_refuses_the_rename_still_starts_empty(tmp_path):
    out = run_scenario(tmp_path, A12)
    assert out["threw"] is None
    assert out["value"] == {"a": 1}
    assert out["stillThere"] is True
    assert out["okValue"] == [] and out["aside"] == 1


# --------------------------------------------------------------------------- #
# A-8: the tray's privacy switch tells the owner when the server could not do it
# --------------------------------------------------------------------------- #

A8 = r'''
main(async () => {
  let mode = "ok";
  visionServer.removeAllListeners("request");
  visionServer.on("request", (req, res) => {
    if (req.url.startsWith("/api/vision/status")) { res.writeHead(200, { "Content-Type": "application/json" }); res.end(JSON.stringify({ kill_switch: { mic_enabled: true, camera_enabled: true } })); return; }
    if (req.url.startsWith("/api/vision/kill-switch")) {
      req.resume();
      if (mode === "hang") return; // never answers
      if (mode === "500") { res.writeHead(500, { "Content-Type": "application/json" }); res.end(JSON.stringify({ ok: false, error: "boom inside the server" })); return; }
      if (mode === "403") { res.writeHead(403, { "Content-Type": "application/json" }); res.end(JSON.stringify({ ok: false, error: "owner only" })); return; }
      if (mode === "html") { res.writeHead(200, { "Content-Type": "text/html" }); res.end("<html>restarting</html>"); return; }
      res.writeHead(200, { "Content-Type": "application/json" }); res.end(JSON.stringify({ ok: true, kill_switch: { mic_enabled: false, camera_enabled: false } })); return;
    }
    res.writeHead(404); res.end("{}");
  });
  await T.fn("createTray")();
  const item = (label) => trayInstances[0].menu.items.find((i) => i.label === label);
  const out = {};
  for (const m of ["ok", "500", "403", "html"]) {
    mode = m; calls.errors.length = 0;
    await item("Kill camera + mic NOW").click();
    out[m] = calls.errors.map((e) => e[1]);
  }
  mode = "500"; calls.errors.length = 0;
  await item("Mic enabled").click();
  out.mic500 = calls.errors.map((e) => e[1]);
  mode = "hang"; calls.errors.length = 0;
  const started = Date.now();
  await item("Kill camera + mic NOW").click();
  out.hang = calls.errors.map((e) => e[1]); out.hangMs = Date.now() - started;
  R.out = out;
});
'''


def test_the_tray_kill_switch_reports_a_failure_instead_of_doing_nothing(tmp_path):
    out = run_scenario(tmp_path, A8)["out"]
    assert out["ok"] == []
    assert len(out["500"]) == 1 and "did NOT go through" in out["500"][0] and "HTTP 500" in out["500"][0] and "boom inside the server" in out["500"][0]
    assert len(out["403"]) == 1 and "owner only" in out["403"][0]
    assert len(out["html"]) == 1 and "did NOT go through" in out["html"][0]
    assert len(out["mic500"]) == 1 and "microphone" in out["mic500"][0]
    assert len(out["hang"]) == 1 and "did not answer" in out["hang"][0]
    assert 4000 <= out["hangMs"] < 15000, "a server that never answers is given up on after a few seconds"


# --------------------------------------------------------------------------- #
# A-9: an import confirmed after a profile switch is not written into the other profile
# --------------------------------------------------------------------------- #

A9 = r'''
const fsx = require("fs");
const CHROME = process.env.T_CHROME, CSVFILE = process.env.T_CSV;
main(async () => {
  T.ensurePaneView();
  await handlers["profile:create"](consoleEvt(), "work");
  const count = async (profile) => {
    const s = T.storesFor(profile);
    return { bookmarks: s.bookmarks.get().length, history: s.history.get().length, passwords: s.password.get().entries ? s.password.get().entries.length : 0 };
  };
  // bookmarks and history: the owner switches profile while the confirmation is open
  openPaths.push(CHROME); dialogAnswer = 0;
  let release; hold.box = new Promise((r) => { release = r; });
  const pending = handlers["import:chrome"](consoleEvt(), {});
  await wait(300);
  R.dialogOpen = calls.dialogs.length;
  await handlers["profile:switch"](consoleEvt(), "work");
  release(); hold.box = null;
  R.chromeResult = await pending;
  R.afterChrome = { default: await count("default"), work: await count("work") };
  // and the normal case still imports into the profile that was active when it started
  await handlers["profile:switch"](consoleEvt(), "default");
  openPaths.push(CHROME);
  R.normal = await handlers["import:chrome"](consoleEvt(), {});
  R.afterNormal = { default: await count("default"), work: await count("work") };
  // passwords: same
  await handlers["profile:switch"](consoleEvt(), "default");
  openPaths.push(CSVFILE); dialogAnswer = 0;
  hold.box = new Promise((r) => { release = r; });
  const pendingPw = handlers["import:passwords"](consoleEvt());
  await wait(300);
  await handlers["profile:switch"](consoleEvt(), "work");
  release(); hold.box = null;
  R.pwResult = await pendingPw;
  R.afterPw = { default: await count("default"), work: await count("work") };
});
'''


def test_an_import_confirmed_after_a_profile_switch_changes_nothing(tmp_path):
    chrome = make_chrome_profile(tmp_path / "chrome")
    csv = tmp_path / "pw.csv"
    csv.write_text("name,url,username,password,note\r\nshop,https://shop.example/login,alice,Secret-Pw-1!,\r\n", encoding="utf-8")
    out = run_scenario(tmp_path, A9, extra_env={"T_CHROME": str(chrome), "T_CSV": str(csv)})
    assert out["dialogOpen"] == 1
    assert out["chromeResult"]["ok"] is False and "changed from" in out["chromeResult"]["error"]
    assert out["afterChrome"]["default"]["bookmarks"] == 0 and out["afterChrome"]["work"]["bookmarks"] == 0
    assert out["afterChrome"]["work"]["history"] == 0 and out["afterChrome"]["default"]["history"] == 0
    assert out["normal"]["ok"] is True and out["normal"]["profile"] == "default"
    assert out["afterNormal"]["default"]["bookmarks"] > 0 and out["afterNormal"]["work"]["bookmarks"] == 0
    assert out["pwResult"]["ok"] is False and "changed from" in out["pwResult"]["error"]
    assert out["afterPw"]["default"]["passwords"] == 0 and out["afterPw"]["work"]["passwords"] == 0


# --------------------------------------------------------------------------- #
# A-6: an extension added in one profile is loaded in every profile already started
# --------------------------------------------------------------------------- #

A6 = r'''
const fsx = require("fs");
const writeExt = (dir, manifest, extra) => { fsx.mkdirSync(dir, { recursive: true }); fsx.writeFileSync(path.join(dir, "manifest.json"), JSON.stringify(manifest)); for (const [f, c] of Object.entries(extra || {})) fsx.writeFileSync(path.join(dir, f), c); };
main(async () => {
  T.ensurePaneView();                                   // profile "default" is started
  const defaultSes = T.paneSession();
  await handlers["profile:create"](consoleEvt(), "work");
  await handlers["profile:switch"](consoleEvt(), "work"); // profile "work" is started too, and active
  const workSes = T.paneSession();
  R.distinct = defaultSes !== workSes;
  R.started = T.extSessionList.length;
  const SRC = path.join(DATA, "src-ext");
  writeExt(SRC, { manifest_version: 3, name: "Page Tagger", version: "1.2", permissions: ["storage"] }, { "tag.js": "1" });
  openPaths.push(SRC); dialogAnswer = 0;
  const added = await handlers["ext:add"](consoleEvt());
  R.added = added.ok;
  const id = added.extension && added.extension.id;
  R.loadedIn = extCalls.loaded.map((l) => l.partition).sort();
  R.statusDefault = (T.statusMap(defaultSes).get(id) || {}).loaded === true;
  R.statusWork = (T.statusMap(workSes).get(id) || {}).loaded === true;
  // disable unloads everywhere (unchanged), enable loads everywhere again
  await handlers["ext:disable"](consoleEvt(), id);
  R.afterDisable = { default: (T.statusMap(defaultSes).get(id) || {}).loaded === true, work: (T.statusMap(workSes).get(id) || {}).loaded === true };
  extCalls.loaded.length = 0; dialogAnswer = 0;
  const enabled = await handlers["ext:enable"](consoleEvt(), id);
  R.enabled = enabled.ok;
  R.reloadedIn = extCalls.loaded.map((l) => l.partition).sort();
  R.afterEnable = { default: (T.statusMap(defaultSes).get(id) || {}).loaded === true, work: (T.statusMap(workSes).get(id) || {}).loaded === true };
});
'''


def test_an_extension_added_or_enabled_in_one_profile_reaches_every_started_profile(tmp_path):
    out = run_scenario(tmp_path, A6)
    assert out["distinct"] is True and out["started"] >= 2
    assert out["added"] is True
    assert len(out["loadedIn"]) == 2 and len(set(out["loadedIn"])) == 2, out["loadedIn"]
    assert out["statusDefault"] is True and out["statusWork"] is True
    assert out["afterDisable"] == {"default": False, "work": False}
    assert out["enabled"] is True and len(set(out["reloadedIn"])) == 2
    assert out["afterEnable"] == {"default": True, "work": True}


# --------------------------------------------------------------------------- #
# A-7: the stale-server cleanup only ends a process that really is our server
# --------------------------------------------------------------------------- #

A7 = r'''
const { spawn, execFileSync } = require("child_process");
const fsx = require("fs");
const alive = (pid) => { try { process.kill(pid, 0); return true; } catch { return false; } };
const waitUp = async (port) => { for (let i = 0; i < 50; i += 1) { const ok = await new Promise((r) => { const q = http.get({ host: "127.0.0.1", port, path: "/workspace" }, (res) => { res.resume(); r(true); }); q.on("error", () => r(false)); }); if (ok) return true; await wait(100); } return false; };
const standIn = (extraArg) => spawn(process.execPath, ["-e", `require('http').createServer((q,r)=>r.end('ok')).listen(${T.port},'127.0.0.1')`, extraArg], { stdio: "ignore" });
main(async () => {
  visionServer.close(); // free the port for the stand-in servers
  await wait(100);
  const stopStale = T.fn("stopStaleServer"), isOurs = T.fn("pidIsOurServer"), removePid = T.fn("removeServerPidFile");
  R.have = [typeof stopStale, typeof isOurs, typeof removePid];
  const pidFile = T.serverPidFile();
  fsx.mkdirSync(path.dirname(pidFile), { recursive: true });
  const kids = [];
  try {
    // 1. a stale pid that has been reused by an unrelated process: it must be left alone
    const innocent = spawn("sleep", ["60"], { stdio: "ignore" }); kids.push(innocent);
    fsx.writeFileSync(pidFile, String(innocent.pid));
    R.unrelated = { err: null };
    try { await stopStale(); } catch (e) { R.unrelated.err = String(e.message || e); }
    R.unrelated.alive = alive(innocent.pid);
    // 2. a process on our port whose command line is not the Dourmouse server: also left alone
    const other = standIn("some-other-program"); kids.push(other);
    R.otherUp = await waitUp(T.port);
    fsx.writeFileSync(pidFile, String(other.pid));
    R.notOurs = { err: null };
    try { await stopStale(); } catch (e) { R.notOurs.err = String(e.message || e); }
    R.notOurs.alive = alive(other.pid);
    R.notOurs.ours = isOurs ? await isOurs(other.pid) : null;
    other.kill(); await wait(300);
    // 3. the real thing: it listens on our port and runs dourmouse.webui
    const real = standIn("dourmouse.webui"); kids.push(real);
    R.realUp = await waitUp(T.port);
    fsx.writeFileSync(pidFile, String(real.pid));
    R.real = { ours: isOurs ? await isOurs(real.pid) : null, err: null };
    try { await stopStale(); } catch (e) { R.real.err = String(e.message || e); }
    await wait(300);
    R.real.alive = alive(real.pid);
    R.real.pidFileLeft = fsx.existsSync(pidFile);
    // 4. the pid file is cleaned up for the right process only
    fsx.writeFileSync(pidFile, "4242");
    if (removePid) { removePid(1111); R.keptForOther = fsx.existsSync(pidFile); removePid(4242); R.removedForOwn = !fsx.existsSync(pidFile); }
    // 5. a server process that exits takes its pid file with it; a newer server's file stays
    fsx.writeFileSync(pidFile, "777");
    const onExit = T.fn("onServerExit");
    onExit({ pid: 777 }, 0, null, null);
    R.exitRemoved = !fsx.existsSync(pidFile);
    fsx.writeFileSync(pidFile, "888");
    onExit({ pid: 777 }, 0, null, null);
    R.exitKeptNewer = fsx.existsSync(pidFile);
    // 6. quitting removes the file of the server this app spawned
    fsx.writeFileSync(pidFile, "999");
    T.setServerProcess({ pid: 999, killed: false, kill() {}, _dmExpected: false });
    appEvents["before-quit"]();
    R.quitRemoved = !fsx.existsSync(pidFile);
  } finally {
    for (const k of kids) { try { k.kill("SIGKILL"); } catch {} }
  }
});
'''


@pytest.mark.skipif(not Path("/usr/sbin/lsof").exists(), reason="lsof is not available")
def test_stale_server_cleanup_never_ends_a_process_that_is_not_our_server(tmp_path):
    out = run_scenario(tmp_path, A7)
    assert out["have"] == ["function", "function", "function"]
    assert out["unrelated"]["err"] and "leftover Dourmouse server" in out["unrelated"]["err"]
    assert out["unrelated"]["alive"] is True, "an unrelated process whose pid was in the file was terminated"
    assert out["otherUp"] is True
    assert out["notOurs"]["ours"] is False
    assert out["notOurs"]["err"] and out["notOurs"]["alive"] is True
    assert out["realUp"] is True and out["real"]["ours"] is True
    assert out["real"]["err"] is None and out["real"]["alive"] is False, out["real"]
    assert out["real"]["pidFileLeft"] is False
    assert out["keptForOther"] is True and out["removedForOwn"] is True
    assert out["exitRemoved"] is True and out["exitKeptNewer"] is True
    assert out["quitRemoved"] is True


# --------------------------------------------------------------------------- #
# A-11: the downloads shelf refuses to open documents that carry script
# --------------------------------------------------------------------------- #


def test_the_downloads_shelf_does_not_open_documents_that_carry_script(tmp_path):
    script = tmp_path / "p.js"
    script.write_text(
        "const p = require(process.argv[2]);\n"
        "const bad = process.argv.slice(3, 3 + Number(process.env.NBAD));\n"
        "const good = process.argv.slice(3 + Number(process.env.NBAD));\n"
        "console.log(JSON.stringify({ bad: bad.map(p.isOpenableDownload), good: good.map(p.isOpenableDownload) }));\n",
        encoding="utf-8",
    )
    bad = ["invoice.html", "a.HTM", "b.xhtml", "logo.svg", "x.SVG", "page.webarchive", "old.mht", "a.mhtml", "run.jnlp", "m.docm", "s.xlsm", "p.pptm",
           "t.dotm", "x.xlam", "e.hta", "h.chm", "view.xml", "style.xsl", "evil.sh", "tool.command", "Page.HTML"]
    good = ["x.pdf", "x.png", "x.txt", "x.zip", "x.docx", "archive.tar.gz", "X.PDF", "photo.jpg", "sheet.xlsx", "deck.pptx", "song.mp3", "clip.mp4"]
    proc = subprocess.run([NODE, str(script), str(ELECTRON / "policy.js"), *bad, *good], capture_output=True, text=True, timeout=30, check=False,
                          env={**os.environ, "NBAD": str(len(bad))})
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    assert got["bad"] == [False] * len(bad), dict(zip(bad, got["bad"], strict=True))
    assert got["good"] == [True] * len(good), dict(zip(good, got["good"], strict=True))


# --------------------------------------------------------------------------- #
# A-4: the DRM install script says what went wrong when the tag lookup fails
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("node") is None or shutil.which("npm") is None, reason="needs bash, node and npm")
def test_install_drm_script_reports_a_failed_tag_lookup(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(REPO / "scripts" / "install_drm_electron.sh", repo / "scripts" / "install_drm_electron.sh")
    el = repo / "electron"
    (el / "node_modules" / "electron").mkdir(parents=True)
    (el / "node_modules" / "electron" / "package.json").write_text('{"name":"electron","version":"44.5.1"}', encoding="utf-8")
    (el / "package.json").write_text('{"name":"x"}', encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git = bin_dir / "git"
    git.write_text("#!/bin/sh\nexit 128\n", encoding="utf-8")  # the network is down
    git.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "CASTLABS_TAG"}
    env.update(HOME=str(home), PATH=f"{bin_dir}:{os.environ['PATH']}")
    proc = subprocess.run(["bash", str(repo / "scripts" / "install_drm_electron.sh"), "--yes"], capture_output=True, text=True, timeout=60, env=env, check=False)
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
    assert "No castLabs tag was found for Electron 44" in proc.stderr
    assert "CASTLABS_TAG" in proc.stderr
    assert "using tag" not in proc.stdout, "it must not go on to install anything"


# --------------------------------------------------------------------------- #
# H-FB-1 (same class as A-9): removing a profile, confirmed after the owner switched to it
# --------------------------------------------------------------------------- #

HFB1 = r'''
const fsx = require("fs");
main(async () => {
  T.ensurePaneView();
  await handlers["profile:create"](consoleEvt(), "work");
  await handlers["profile:switch"](consoleEvt(), "work");
  await handlers["profile:switch"](consoleEvt(), "default");
  const dir = path.join(USERDATA, "browser", "profiles", "work");
  fsx.mkdirSync(dir, { recursive: true }); fsx.writeFileSync(path.join(dir, "bookmarks.json"), "[]");
  dialogAnswer = 0;
  let release; hold.box = new Promise((r) => { release = r; });
  const pending = handlers["profile:remove"](consoleEvt(), "work");
  await wait(300);
  await handlers["profile:switch"](consoleEvt(), "work");    // the owner (or a script) switches to the profile being removed
  release(); hold.box = null;
  R.removed = await pending;
  T.flushBrowserStores();
  R.active = T.activeProfile();
  R.registry = T.profileRegistry();
  R.saved = readJson("profiles.json");
  R.dirStillThere = fsx.existsSync(dir);
});
'''


def test_removing_a_profile_that_became_active_during_the_confirmation_is_refused(tmp_path):
    out = run_scenario(tmp_path, HFB1)
    assert out["removed"]["ok"] is False and "nothing was removed" in out["removed"]["error"]
    assert out["active"] == "work" and out["registry"]["active"] == "work" and "work" in out["registry"]["names"]
    assert out["saved"]["active"] == "work" and "work" in out["saved"]["names"], "the registry on disk still agrees with the running app"
    assert out["dirStillThere"] is True
