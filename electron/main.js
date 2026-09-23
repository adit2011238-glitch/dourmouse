// Dourmouse native shell (Electron). Replaces dourmouse/desktop.py's
// pywebview-based launch(); the Python backend it points at
// (dourmouse/webui.py's HTTP+SSE server) is completely unmodified.
//
// STATUS, corrected 2026-09-23 (OS-2, finding #078). This header used to say
// Stages C, D and E were "not yet done" -- stale, and contradicted by
// package.json's own description in the same directory. All five stages are
// real and present in this file: A/B (shell, windowing, IPC bridge), C (real
// Tray, Notification and nativeImage branding), D (the embedded CDP-driven
// BrowserView plus the pane-bridge server below), and E (electron-builder
// config with notarization in package.json). electron/verify-result.json
// records a real verification run with "errors": []. The same
// documentation-reality drift this repo has hit before -- docs/UI_SOURCE_MAP.md
// found "four skins" where there were eight and "nine screens" where there
// were fourteen -- so treat any in-file comment here as directional until
// re-checked against the code.
//
// This IS now the default shell for a real launch: dourmouse/desktop.py's
// __main__ block hands off to it when electron/node_modules is present
// (DOURMOUSE_SHELL=pywebview opts back out). It is not an unconditional
// default because node_modules is gitignored, so a fresh clone genuinely has
// no Electron and must still start -- run `npm install` here to enable it.

const { app, BrowserWindow, BrowserView, ipcMain, shell, Tray, Menu, nativeImage, Notification } = require("electron");
const { spawn } = require("child_process");
const http = require("http");
const fs = require("fs");
const path = require("path");

// Stage D: real CDP access to this process's own Chromium, the load-
// bearing fact proven live in the migration spike (chromium.connect_over_cdp
// against this exact switch enumerated every real open page, including an
// embedded BrowserView's, and drove a real Playwright navigation on it).
// Must be set before app.whenReady(). Configurable so a second instance
// (dev + a packaged build running side by side) never collides.
const CDP_PORT = parseInt(process.env.DOURMOUSE_ELECTRON_CDP_PORT || "9333", 10);
app.commandLine.appendSwitch("remote-debugging-port", String(CDP_PORT));

// Stage E: when packaged, electron-builder's extraResources (see
// package.json's "build" config) lay dourmouse/, ui/, and .venv/ out as
// siblings directly under Contents/Resources — the EXACT SAME relative
// layout this repo already has (<project_root>/dourmouse,
// <project_root>/ui, <project_root>/.venv), so every existing Python
// path-resolution helper that does the equivalent of
// Path(__file__).resolve().parent.parent (dourmouse/browser_agent.py's
// own _PROJECT_ROOT, for one real example) keeps working unmodified —
// only THIS constant needs the packaged/dev branch, nothing in Python.
const PROJECT_ROOT = app.isPackaged ? process.resourcesPath : path.join(__dirname, "..");
const VENV_PYTHON = path.join(PROJECT_ROOT, ".venv", "bin", "python");
// Same env var + default dourmouse/webui.py's own _DEFAULT_PORT already
// reads at import time (confirmed by reading it before writing this --
// `_DEFAULT_PORT = int(os.environ.get("DOURMOUSE_UI_PORT", "8765"))`), and
// the same one dourmouse/desktop.py's _pick_port() reads -- so no backend
// change was needed to make the port controllable from here.
const PORT = parseInt(process.env.DOURMOUSE_UI_PORT || "8765", 10);
const BASE_URL = `http://127.0.0.1:${PORT}`;
// When true, an already-running dev server on PORT is reused instead of
// spawning a new one -- matches how the feasibility spike iterated, and
// is genuinely useful for fast local development. The real packaged app
// (Stage E) always spawns its own bundled server; this is a dev
// convenience only.
const REUSE_EXISTING_SERVER = process.env.DOURMOUSE_ELECTRON_REUSE_SERVER === "1";

let serverProcess = null;
let mainWindow = null;
let mapWindow = null;
let atlasWindow = null;
// task_id -> BrowserWindow, the exact same dedupe-by-id convention as
// dourmouse/desktop.py's DesktopBridge._agent_windows / open_task_window:
// one window per id, reused/focused on repeat calls, recreated if closed.
const taskWindows = new Map();

const PRELOAD = path.join(__dirname, "preload.js");

function log(...args) {
  console.log("[dourmouse-electron]", ...args);
}

// --------------------------------------------------------------------- //
// Server lifecycle
// --------------------------------------------------------------------- //

function pingServer(url) {
  return new Promise((resolve) => {
    const req = http.get(url, (res) => {
      res.resume();
      resolve(res.statusCode >= 200 && res.statusCode < 500);
    });
    req.on("error", () => resolve(false));
    req.setTimeout(2000, () => {
      req.destroy();
      resolve(false);
    });
  });
}

async function waitForServer(url, timeoutMs = 30000, intervalMs = 300) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await pingServer(url)) return true;
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  return false;
}

async function ensureServer() {
  if (await pingServer(`${BASE_URL}/workspace`)) {
    log(`reusing server already answering on ${BASE_URL}`);
    return;
  }
  if (REUSE_EXISTING_SERVER) {
    throw new Error(
      `DOURMOUSE_ELECTRON_REUSE_SERVER=1 but nothing is answering at ${BASE_URL} -- start one first.`
    );
  }
  if (!fs.existsSync(VENV_PYTHON)) {
    throw new Error(`No venv python at ${VENV_PYTHON} -- run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-desktop.txt`);
  }
  log(`spawning ${VENV_PYTHON} -m dourmouse.webui (port ${PORT})`);
  serverProcess = spawn(VENV_PYTHON, ["-m", "dourmouse.webui"], {
    cwd: PROJECT_ROOT,
    env: {
      ...process.env,
      DOURMOUSE_UI_PORT: String(PORT),
      // Stage D: how dourmouse/browser_agent.py discovers this shell's
      // real CDP port and the tiny pane-bridge server below. Absent
      // entirely under the old pywebview shell or a plain headless
      // server -- browser_agent.py's own _electron_pane_configured()
      // treats missing/unset as "no pane available" and falls back to
      // its existing launch()-its-own-Chrome behavior unchanged.
      DOURMOUSE_ELECTRON_CDP_PORT: String(CDP_PORT),
      DOURMOUSE_ELECTRON_PANE_PORT: String(PANE_BRIDGE_PORT),
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  serverProcess.stdout.on("data", (d) => process.stdout.write(`[server] ${d}`));
  serverProcess.stderr.on("data", (d) => process.stderr.write(`[server:err] ${d}`));
  serverProcess.on("exit", (code) => log(`server process exited (${code})`));
  const ok = await waitForServer(`${BASE_URL}/workspace`);
  if (!ok) {
    throw new Error(`server did not answer at ${BASE_URL}/workspace within the startup deadline`);
  }
}

function stopServer() {
  if (serverProcess && !serverProcess.killed) {
    log("stopping spawned server process");
    try {
      serverProcess.kill();
    } catch {
      /* best-effort teardown, matches dourmouse/desktop.py's own finally block */
    }
  }
}

// --------------------------------------------------------------------- //
// Vision helpers (Stage C) -- dourmouse/desktop.py's v13.11 folded
// overlay/wakeword/tray into ONE process specifically because they shared
// pywebview's own AppKit event loop. Electron owns its own separate main
// process (Node, not that same loop), so that specific consolidation
// trick doesn't carry over -- a real, disclosed tradeoff (see the plan
// doc), not a silent regression: back to small separate Python helper
// processes for this shell, same as the pre-v13.11 shape. Each one
// already has its OWN real, tested, standalone `python -m dourmouse.X`
// entrypoint -- zero new or modified Python code needed for any of them.
// Best-effort per helper, matching desktop.py's own discipline: one
// failing (missing optional dependency, DOURMOUSE_WAKEWORD off, a port
// already taken) never blocks the app or any other helper.
// --------------------------------------------------------------------- //

const helperProcesses = [];

function spawnPythonHelper(moduleName, extraEnv) {
  if (!fs.existsSync(VENV_PYTHON)) return null;
  const proc = spawn(VENV_PYTHON, ["-m", moduleName], {
    cwd: PROJECT_ROOT,
    env: { ...process.env, ...(extraEnv || {}) },
    stdio: ["ignore", "pipe", "pipe"],
  });
  const tag = moduleName.split(".").pop();
  proc.stdout.on("data", (d) => process.stdout.write(`[${tag}] ${d}`));
  proc.stderr.on("data", (d) => process.stderr.write(`[${tag}:err] ${d}`));
  proc.on("exit", (code) => log(`${tag} helper exited (${code}) -- non-fatal, matches desktop.py's best-effort discipline`));
  proc.on("error", (exc) => log(`${tag} helper failed to start (non-fatal): ${exc.message}`));
  helperProcesses.push(proc);
  return proc;
}

function startVisionHelpers() {
  const visionAutostart = process.env.DOURMOUSE_VISION_AUTOSTART !== "0"; // same default as desktop.py
  if (!visionAutostart) {
    log("DOURMOUSE_VISION_AUTOSTART=0 -- skipping vision helpers");
    return;
  }
  // dourmouse.vision_bridge: the kill-switch's real reach into an
  // in-progress browser vision session. Its own default state_reader
  // (dourmouse.tray.load_state) reads the SAME shared state file the
  // Electron tray below reads/writes via the existing HTTP endpoint --
  // no coordination needed, they already agree via one file on disk.
  spawnPythonHelper("dourmouse.vision_bridge");
  // dourmouse.wakeword: a genuine no-op (prints "NOT CONFIGURED", exits 1,
  // caught by the 'exit' handler above as non-fatal) unless the user has
  // explicitly set DOURMOUSE_WAKEWORD=1 -- safe to always spawn.
  spawnPythonHelper("dourmouse.wakeword");
  // dourmouse.overlay: genuinely already supports running fully
  // standalone (its own pywebview window, its own polling loop against
  // the main server's real /api/activity) -- reused unchanged rather than
  // reimplemented in Electron. Having pywebview installed/used for just
  // this one small always-on-top utility window is harmless even though
  // the MAIN shell is Electron now; they're independent OS processes.
  spawnPythonHelper("dourmouse.overlay");
}

function stopVisionHelpers() {
  for (const proc of helperProcesses) {
    if (!proc.killed) {
      try {
        proc.kill();
      } catch {
        /* best-effort teardown */
      }
    }
  }
  helperProcesses.length = 0;
}

// --------------------------------------------------------------------- //
// Native tray (Stage C) -- replaces dourmouse/tray.py's TrayApp (pystray)
// for this shell. The kill-switch STATE logic in tray.py (KillSwitchState/
// load_state/save_state/mic_allowed/camera_allowed) is pure, already-real,
// already-tested file-based logic with zero shell coupling -- this tray
// just calls the EXISTING /api/vision/kill-switch POST endpoint (confirmed
// by reading webui.py's _handle_vision_kill_switch_post before writing
// this) and reads /api/vision/status's real kill_switch field for state,
// same effect as clicking the equivalent control in the web UI. No Python
// changes needed.
// --------------------------------------------------------------------- //

// Same bitmap design as tray.py's _build_icon_image (same colors, same
// dot layout) so the visual language matches across every surface this
// app has a tray-like control on -- built as an inline SVG data URL
// (crisp at any menu-bar scale) instead of needing a canvas/Pillow
// equivalent in Node.
const _TRAY_COLOR_ON = "#3adc64";
const _TRAY_COLOR_OFF = "#c83c34";
const _TRAY_COLOR_BG = "#18181c";

function buildTrayIcon(micEnabled, cameraEnabled) {
  const micColor = micEnabled ? _TRAY_COLOR_ON : _TRAY_COLOR_OFF;
  const camColor = cameraEnabled ? _TRAY_COLOR_ON : _TRAY_COLOR_OFF;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64">
    <rect x="4" y="4" width="56" height="56" rx="14" fill="${_TRAY_COLOR_BG}"/>
    <circle cx="21" cy="32" r="9" fill="${micColor}"/>
    <circle cx="43" cy="32" r="9" fill="${camColor}"/>
  </svg>`;
  const dataUrl = `data:image/svg+xml;base64,${Buffer.from(svg).toString("base64")}`;
  const img = nativeImage.createFromDataURL(dataUrl);
  // macOS menu-bar icons render best around 18-22px -- resize down from
  // the crisp 64px source rather than drawing a second small SVG.
  return img.resize({ width: 20, height: 20 });
}

let tray = null;

async function fetchJson(url, options) {
  return new Promise((resolve, reject) => {
    const req = http.request(url, options || {}, (res) => {
      let body = "";
      res.on("data", (chunk) => (body += chunk));
      res.on("end", () => {
        try {
          resolve(JSON.parse(body));
        } catch (exc) {
          reject(exc);
        }
      });
    });
    req.on("error", reject);
    if (options && options.body) req.write(options.body);
    req.end();
  });
}

async function fetchKillSwitchState() {
  const status = await fetchJson(`${BASE_URL}/api/vision/status`, { method: "GET" });
  return status.kill_switch || { mic_enabled: true, camera_enabled: true };
}

async function postKillSwitch(action, enabled) {
  const body = JSON.stringify(enabled === undefined ? { action } : { action, enabled });
  const result = await fetchJson(`${BASE_URL}/api/vision/kill-switch`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) },
    body,
  });
  return result.kill_switch;
}

async function refreshTray() {
  if (!tray) return;
  let state;
  try {
    state = await fetchKillSwitchState();
  } catch (exc) {
    log("tray: could not read kill-switch state (non-fatal):", exc.message || exc);
    return;
  }
  tray.setImage(buildTrayIcon(state.mic_enabled, state.camera_enabled));
  const micLabel = state.mic_enabled ? "mic on" : "MIC KILLED";
  const camLabel = state.camera_enabled ? "cam on" : "CAM KILLED";
  tray.setToolTip(`DourMouse — ${micLabel} / ${camLabel}`);
  const menu = Menu.buildFromTemplate([
    {
      label: "Kill camera + mic NOW",
      click: async () => {
        await postKillSwitch("kill_all");
        refreshTray();
      },
    },
    { type: "separator" },
    {
      label: "Mic enabled",
      type: "checkbox",
      checked: state.mic_enabled,
      click: async () => {
        await postKillSwitch("set_mic", !state.mic_enabled);
        refreshTray();
      },
    },
    {
      label: "Camera enabled",
      type: "checkbox",
      checked: state.camera_enabled,
      click: async () => {
        await postKillSwitch("set_camera", !state.camera_enabled);
        refreshTray();
      },
    },
    { type: "separator" },
    { label: "Quit", click: () => app.quit() },
  ]);
  tray.setContextMenu(menu);
}

async function createTray() {
  tray = new Tray(buildTrayIcon(true, true));
  await refreshTray();
}

// --------------------------------------------------------------------- //
// Embedded browser pane (Stage D) -- the actual new capability. The pane
// is a real BrowserView docked to the right side of the main window,
// backed by this SAME process's own Chromium (the CDP_PORT switch set
// above). dourmouse/browser_agent.py connects to that exact same session
// via Playwright's connect_over_cdp (proven live in the migration spike),
// so the human watching this pane and the agent's existing browser_open/
// browser_click/... tools are driving the literal same page -- no
// mirroring, no second browser process.
//
// This tiny local HTTP server is the cross-process signal browser_agent.py
// needs (show/hide requests originate from a Python tool call in a
// DIFFERENT OS process) -- the same "a tiny second local server for
// cross-process signaling" pattern dourmouse/vision_bridge.py's own
// docstring already establishes and justifies in this codebase, just
// implemented in Node since this side of the bridge lives in Electron's
// main process.
// --------------------------------------------------------------------- //

const PANE_BRIDGE_PORT = parseInt(process.env.DOURMOUSE_ELECTRON_PANE_PORT || "9334", 10);
const PANE_WIDTH_FRACTION = 0.45; // right ~45% of the main window, adjustable later

let paneView = null;
let paneVisible = false;

function paneBounds() {
  if (!mainWindow || mainWindow.isDestroyed()) return { x: 0, y: 0, width: 0, height: 0 };
  const { width, height } = mainWindow.getContentBounds();
  const paneWidth = Math.round(width * PANE_WIDTH_FRACTION);
  return { x: width - paneWidth, y: 0, width: paneWidth, height };
}

function ensurePaneView() {
  if (paneView) return paneView;
  paneView = new BrowserView({
    webPreferences: { contextIsolation: true },
  });
  // Deliberately about:blank, and deliberately never navigated away from
  // here -- dourmouse/browser_agent.py's connect_over_cdp discovery finds
  // "the" pane by looking for the one still-blank about:blank page at
  // attach time (none of this app's OWN windows ever load about:blank),
  // then caches that Page object for the rest of the process's life.
  // Real, disclosed limitation: this is a one-time-at-discovery match,
  // not an ongoing identity check -- see browser_agent.py's own comment
  // on _ensure_browser_via_electron_pane for the full reasoning.
  paneView.webContents.loadURL("about:blank");
  return paneView;
}

function showPane() {
  if (!mainWindow || mainWindow.isDestroyed()) return false;
  const view = ensurePaneView();
  mainWindow.addBrowserView(view);
  view.setBounds(paneBounds());
  view.setAutoResize({ width: false, height: true }); // width recalculated explicitly on resize below
  paneVisible = true;
  return true;
}

function hidePane() {
  if (!mainWindow || mainWindow.isDestroyed() || !paneView) {
    paneVisible = false;
    return true;
  }
  try {
    mainWindow.removeBrowserView(paneView);
  } catch {
    /* already removed -- fine */
  }
  paneVisible = false;
  return true;
}

function startPaneBridge() {
  const server = http.createServer((req, res) => {
    const respond = (status, body) => {
      res.writeHead(status, { "Content-Type": "application/json" });
      res.end(JSON.stringify(body));
    };
    // Localhost-only by construction (bound to 127.0.0.1 below, matching
    // this whole app's existing 127.0.0.1-only posture) -- no auth needed
    // for the same reason webui.py's own loopback-only endpoints don't.
    if (req.method === "GET" && req.url === "/status") {
      respond(200, { active: paneVisible, cdpEndpoint: `http://127.0.0.1:${CDP_PORT}` });
    } else if (req.method === "POST" && req.url === "/show") {
      respond(200, { ok: showPane() });
    } else if (req.method === "POST" && req.url === "/hide") {
      respond(200, { ok: hidePane() });
    } else if (req.method === "POST" && req.url.startsWith("/navigate")) {
      // 2026-09-23 (finding #081). THE fix for the browser pane's proxy
      // errors, and it is an architectural one rather than a patch.
      //
      // The iframe pane cannot load a site that sends X-Frame-Options or
      // CSP frame-ancestors, which is most of the real web, so a rewriting
      // proxy existed as the fallback. That proxy is where the errors come
      // from: it mangles relative URLs, it carries no cookies so a logged-in
      // site looks logged out, and it has to rewrite responses it cannot
      // always parse.
      //
      // None of that is necessary here. Those headers restrict FRAMING. A
      // BrowserView is a real top-level browsing context, not a frame, so
      // they do not apply to it at all. Proven with a controlled experiment
      // rather than assumed: the same local page serving
      // "X-Frame-Options: DENY" plus "frame-ancestors 'none'" came back
      // BLANK (ERR_BLOCKED_BY_RESPONSE) in an iframe and LOADED in a
      // BrowserView, same Chromium, same process.
      //
      // So: no proxy, no rewriting, real cookies, real sessions.
      let body = "";
      req.on("data", (c) => { body += c; if (body.length > 8192) req.destroy(); });
      req.on("end", () => {
        let url;
        try { url = JSON.parse(body || "{}").url; } catch { url = null; }
        // Only real http(s). Refusing file:// and data:// here matters: this
        // bridge is reachable from any local process, and a BrowserView
        // pointed at file:// would read the user's disk.
        if (typeof url !== "string" || !/^https?:\/\//i.test(url)) {
          respond(400, { ok: false, error: "url must be a real http(s) URL" });
          return;
        }
        try {
          const view = ensurePaneView();
          if (!paneVisible) showPane();
          view.webContents.loadURL(url);
          respond(200, { ok: true, url });
        } catch (err) {
          respond(500, { ok: false, error: String(err && err.message || err) });
        }
      });
    } else if (req.method === "POST" && /^\/(back|forward|reload)$/.test(req.url)) {
      // Real history, which the iframe pane could never have: its own code
      // comments note cross-origin history "isn't reachable from the parent
      // page". A BrowserView owns its history outright.
      if (!paneView) { respond(409, { ok: false, error: "pane not open" }); return; }
      const wc = paneView.webContents;
      const nav = wc.navigationHistory;
      try {
        if (req.url === "/back") {
          if (nav && typeof nav.canGoBack === "function" ? nav.canGoBack() : wc.canGoBack())
            (nav && nav.goBack ? nav.goBack() : wc.goBack());
        } else if (req.url === "/forward") {
          if (nav && typeof nav.canGoForward === "function" ? nav.canGoForward() : wc.canGoForward())
            (nav && nav.goForward ? nav.goForward() : wc.goForward());
        } else {
          wc.reload();
        }
        respond(200, { ok: true });
      } catch (err) {
        respond(500, { ok: false, error: String(err && err.message || err) });
      }
    } else {
      respond(404, { ok: false, error: "not found" });
    }
  });
  server.listen(PANE_BRIDGE_PORT, "127.0.0.1", () => {
    log(`pane bridge listening on 127.0.0.1:${PANE_BRIDGE_PORT}`);
  });
  server.on("error", (exc) => log("pane bridge failed to start (non-fatal):", exc.message));
  return server;
}

// --------------------------------------------------------------------- //
// Native notifications (Stage C) -- replaces desktop.py's DesktopNotifier.
// The original reads the server's in-memory event hub directly (an
// in-process Python object reference, server.events_broadcast) -- not
// reachable from a separate Electron process. This is a genuinely new
// small piece (not a port of existing code): a plain HTTP GET against the
// existing, already-real /api/events SSE endpoint (the SAME stream
// ui/*.html's startNewsStream() already consumes from a browser context),
// replicating DesktopNotifier's own alert-diffing logic (seen-set, primed
// flag, bounded size) in JS. No Python changes.
// --------------------------------------------------------------------- //

const _NOTIFY_MAX_SEEN = 200;
let _notifySeen = new Set();
let _notifyPrimed = false;
let _notifyRefreshing = false;

async function refreshAlerts() {
  if (_notifyRefreshing) return;
  _notifyRefreshing = true;
  try {
    const state = await fetchJson(`${BASE_URL}/api/state`, { method: "GET" });
    const alerts = state.alerts || [];
    const fresh = [];
    for (const alert of alerts) {
      const aid = alert && alert.id;
      if (aid === undefined || aid === null) continue;
      if (!_notifyPrimed) {
        _notifySeen.add(aid);
        continue;
      }
      if (_notifySeen.has(aid)) continue;
      _notifySeen.add(aid);
      fresh.push(alert);
    }
    _notifyPrimed = true;
    if (_notifySeen.size > _NOTIFY_MAX_SEEN) {
      _notifySeen = new Set([..._notifySeen].slice(-_NOTIFY_MAX_SEEN));
    }
    for (const alert of fresh) {
      if (Notification.isSupported()) {
        new Notification({
          title: (alert.title || "DourMouse alert").toString().slice(0, 80),
          body: (alert.detail || "").toString().slice(0, 200),
        }).show();
      }
    }
  } catch (exc) {
    log("notifications: /api/state refresh failed (non-fatal):", exc.message || exc);
  } finally {
    _notifyRefreshing = false;
  }
}

function startAlertNotifications() {
  if (process.env.DOURMOUSE_DESKTOP_NOTIFICATIONS === "0") return;
  // Raw SSE consumption (no npm dependency needed): /api/events is a
  // plain text/event-stream of "data: {...}\n\n" lines, same framing this
  // whole project's own test suite already parses this way (http.client
  // + readline() + a "data: " prefix check).
  const req = http.get(`${BASE_URL}/api/events`, (res) => {
    let buffer = "";
    res.on("data", (chunk) => {
      buffer += chunk.toString("utf8");
      let idx;
      while ((idx = buffer.indexOf("\n")) !== -1) {
        const line = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 1);
        if (!line.startsWith("data: ")) continue;
        try {
          const payload = JSON.parse(line.slice(6));
          if (payload.type === "state_change" && payload.section === "alerts") {
            refreshAlerts();
          }
        } catch {
          /* a malformed line is skipped, never crashes the listener */
        }
      }
    });
    res.on("end", () => log("notifications: /api/events stream ended"));
  });
  req.on("error", (exc) => log("notifications: /api/events connection failed (non-fatal):", exc.message));
}

// --------------------------------------------------------------------- //
// Window-geometry persistence (Stage A) -- window_state()/set_window_state()
// in dourmouse/desktop.py's DesktopBridge are called ONLY from Python
// itself (main_window.events.closed += _persist_geometry), never from
// ui/*.html -- confirmed by grepping every ui/*.html and dourmouse/*.py
// for a real caller before deciding this. So this is plain main-process
// logic here too, not an IPC-exposed renderer API.
// --------------------------------------------------------------------- //

const _MIN_DIMENSION = 200;

function windowStatePath() {
  return path.join(app.getPath("userData"), "window-state.json");
}

function readWindowState() {
  try {
    const raw = JSON.parse(fs.readFileSync(windowStatePath(), "utf8"));
    return raw && typeof raw === "object" ? raw : {};
  } catch {
    return {};
  }
}

function writeWindowState(state) {
  try {
    fs.mkdirSync(path.dirname(windowStatePath()), { recursive: true });
    fs.writeFileSync(windowStatePath(), JSON.stringify(state));
  } catch {
    /* window-state memory is best-effort, matches desktop.py's own guard */
  }
}

function persistMainWindowGeometry() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  const bounds = mainWindow.getBounds();
  if (bounds.width < _MIN_DIMENSION || bounds.height < _MIN_DIMENSION) return;
  writeWindowState({ ...bounds, maximized: mainWindow.isMaximized() });
}

// --------------------------------------------------------------------- //
// IPC bridge (Stage B) -- window.pywebview.api.* via preload.js.
// Only the 3 real, live call sites found by grepping ui/*.html:
// open_agent (index.html), open_all_hands (index.html, all_hands.html),
// open_external (setup.html, login.html). open_map has no real caller
// anywhere (a docstring mention only) but is cheap and the map window
// already exists hidden, so it's wired too, just not advertised as a
// "ported" feature the way the 3 real ones are.
// --------------------------------------------------------------------- //

function openTaskWindow(taskId, routePath, { title, width = 980, height = 760 } = {}) {
  taskId = (taskId || "").trim();
  routePath = (routePath || "").trim();
  if (!taskId || !routePath || !(routePath.startsWith("/") || routePath.startsWith("#"))) {
    return false;
  }
  if (!routePath.startsWith("/")) routePath = "/" + routePath; // "#/world" -> "/#/world"
  const existing = taskWindows.get(taskId);
  if (existing && !existing.isDestroyed()) {
    existing.show();
    existing.focus();
    return true;
  }
  const win = new BrowserWindow({
    width,
    height,
    minWidth: 720,
    minHeight: 540,
    title: (title || taskId.toUpperCase()).slice(0, 80),
    webPreferences: { preload: PRELOAD, contextIsolation: true },
  });
  win.loadURL(`${BASE_URL}${routePath}`);
  taskWindows.set(taskId, win);
  win.on("closed", () => taskWindows.delete(taskId));
  return true;
}

ipcMain.handle("bridge:open_agent", (_evt, name) => {
  name = (name || "").toString().trim();
  if (!name) return false;
  return openTaskWindow(name, `/agent/${name}`, { title: `AGENT // ${name.toUpperCase()}` });
});

ipcMain.handle("bridge:open_all_hands", (_evt, runId, goal) => {
  runId = (runId || "").toString().trim();
  if (!runId) return false;
  const title = `ALL HANDS // ${(goal || runId).toString().trim().slice(0, 28).toUpperCase()}`;
  return openTaskWindow(`allhands:${runId}`, `/all-hands?run=${encodeURIComponent(runId)}`, { title });
});

ipcMain.handle("bridge:open_external", async (_evt, url) => {
  // Mirrors desktop.py's open_external contract exactly: only http(s),
  // never raises in a way that could crash the window, honest False
  // otherwise. Real, disclosed gap vs. desktop.py's _open_in_chrome:
  // Electron has no first-party "open in a SPECIFIC browser" API, so this
  // opens the OS DEFAULT browser (shell.openExternal), matching
  // desktop.py's own webbrowser.open() FALLBACK path -- not its
  // Chrome-preferred primary path. Worth revisiting only if the default
  // browser ever turns out not to be Chrome for a real user and that
  // causes a real Google-sign-in problem (Chrome itself isn't required by
  // Google, only "not an embedded webview" is).
  if (typeof url !== "string" || !/^https?:\/\//i.test(url)) return false;
  try {
    await shell.openExternal(url);
    return true;
  } catch {
    return false;
  }
});

ipcMain.handle("bridge:open_map", () => {
  if (mapWindow && !mapWindow.isDestroyed()) {
    mapWindow.show();
    return true;
  }
  return false;
});

// --------------------------------------------------------------------- //
// App lifecycle
// --------------------------------------------------------------------- //

app.whenReady().then(async () => {
  try {
    await ensureServer();
  } catch (exc) {
    log("FATAL: could not bring up the server:", exc.message || exc);
    app.quit();
    return;
  }

  const geometry = readWindowState();
  mainWindow = new BrowserWindow({
    width: Number(geometry.width) || 1440,
    height: Number(geometry.height) || 900,
    x: Number.isFinite(geometry.x) ? geometry.x : undefined,
    y: Number.isFinite(geometry.y) ? geometry.y : undefined,
    minWidth: 1024,
    minHeight: 680,
    title: "DOURMOUSE // CENTRAL AGENT DISPATCH",
    webPreferences: { preload: PRELOAD, contextIsolation: true },
  });
  mainWindow.loadURL(`${BASE_URL}/workspace`);
  if (geometry.maximized) mainWindow.maximize();
  mainWindow.on("close", persistMainWindowGeometry);
  mainWindow.on("resize", () => {
    if (paneView && paneVisible) paneView.setBounds(paneBounds());
  });

  // Created hidden up front, same as dourmouse/desktop.py's map_window --
  // pywebview's own "create before start(), reveal on demand" rule doesn't
  // apply to Electron (BrowserWindow can be created any time), kept anyway
  // for behavioral parity during the port.
  mapWindow = new BrowserWindow({
    width: 1280,
    height: 860,
    show: false,
    title: "AGENT ORCHESTRATION MAP",
    webPreferences: { preload: PRELOAD, contextIsolation: true },
  });
  mapWindow.loadURL(`${BASE_URL}/map`);

  const openAtlasAtLaunch = process.env.DOURMOUSE_OPEN_ATLAS_LAB === "1";
  atlasWindow = new BrowserWindow({
    width: 1100,
    height: 760,
    show: openAtlasAtLaunch,
    title: "ATLAS // STRATEGY LAB",
    webPreferences: { preload: PRELOAD, contextIsolation: true },
  });
  atlasWindow.loadURL(`${BASE_URL}/atlas-lab`);

  // Stage A/B end here; Stage C native integrations start here.
  startVisionHelpers();
  await createTray();
  startAlertNotifications();
  // Stage D: the embedded browser pane's cross-process bridge.
  startPaneBridge();

  log(`Dourmouse (Electron shell) online at ${BASE_URL}`);

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      mainWindow = new BrowserWindow({
        width: 1440,
        height: 900,
        minWidth: 1024,
        minHeight: 680,
        webPreferences: { preload: PRELOAD, contextIsolation: true },
      });
      mainWindow.loadURL(`${BASE_URL}/workspace`);
      mainWindow.on("close", persistMainWindowGeometry);
    }
  });
});

// --------------------------------------------------------------------- //
// Smoke-test mode (DOURMOUSE_ELECTRON_VERIFY=1) -- drives the real
// window.pywebview.api.* shim from a real page context (index.html, which
// has real open_agent/open_all_hands call sites) the same way a human
// clicking the UI would, and writes a real result file. Not test
// scaffolding removed after use -- kept as a genuine smoke-test path for
// verifying this shell after future changes, matching this project's own
// "live-reproduced, not just unit-tested" convention for anything
// OS/process-level.
// --------------------------------------------------------------------- //

if (process.env.DOURMOUSE_ELECTRON_VERIFY === "1") {
  const verifyResultPath = path.join(__dirname, "verify-result.json");
  const verifyResult = {
    at: new Date().toISOString(),
    mainWindowLoaded: false,
    mapWindowLoaded: false,
    atlasWindowLoaded: false,
    openAgentOpenedRealWindow: false,
    openAllHandsOpenedRealWindow: false,
    openExternalReturnedTrue: false,
    geometryPersisted: false,
    trayCreated: false,
    killSwitchRoundTrip: null,
    errors: [],
  };
  app.whenReady().then(() => {
    (async () => {
      // Real bug caught by testing against a genuinely FRESH server spawn
      // (not the already-warm reused-server case every earlier run had
      // used): a fixed delay here raced ensureServer()'s real, variable
      // boot time and fired before mainWindow/tray even existed yet. Poll
      // for real readiness instead of guessing a timeout -- the same
      // "wait for the actual condition" principle ensureServer()'s own
      // waitForServer() already uses, applied to this harness too.
      const objectsReady = () => mainWindow && mapWindow && atlasWindow && tray;
      const deadline = Date.now() + 20000;
      while (Date.now() < deadline && !objectsReady()) {
        await new Promise((r) => setTimeout(r, 200));
      }
      // Object existence isn't the same as "finished loading" -- a real,
      // fairly heavy page (workspace.html's actual JS/CSS/SSE) can still
      // report isLoading()===true well after its window object exists,
      // especially on a genuinely cold V8/Electron start with no warm
      // caches (exactly the case a fresh server spawn also is). Wait for
      // the actual per-window condition too, not just object presence.
      const fullyLoaded = () =>
        objectsReady() &&
        !mainWindow.webContents.isLoading() &&
        !mapWindow.webContents.isLoading() &&
        !atlasWindow.webContents.isLoading();
      while (Date.now() < deadline && !fullyLoaded()) {
        await new Promise((r) => setTimeout(r, 200));
      }
      try {
        if (mainWindow) verifyResult.mainWindowLoaded = !mainWindow.webContents.isLoading();
        if (mapWindow) verifyResult.mapWindowLoaded = !mapWindow.webContents.isLoading();
        if (atlasWindow) verifyResult.atlasWindowLoaded = !atlasWindow.webContents.isLoading();

        // Drive the bridge from a REAL index.html page context, not a
        // direct main-process function call -- proves the renderer ->
        // preload -> ipcMain round trip, the actual thing being verified.
        const bridgeTestWin = new BrowserWindow({
          width: 800,
          height: 600,
          show: false,
          webPreferences: { preload: PRELOAD, contextIsolation: true },
        });
        await bridgeTestWin.loadURL(`${BASE_URL}/index.html`);
        const beforeAgentWindows = taskWindows.size;
        await bridgeTestWin.webContents.executeJavaScript(
          "window.pywebview.api.open_agent('mail')"
        );
        await new Promise((r) => setTimeout(r, 500));
        verifyResult.openAgentOpenedRealWindow = taskWindows.size > beforeAgentWindows && taskWindows.has("mail");

        const beforeHandsWindows = taskWindows.size;
        await bridgeTestWin.webContents.executeJavaScript(
          "window.pywebview.api.open_all_hands('verify-run-1', 'smoke test')"
        );
        await new Promise((r) => setTimeout(r, 500));
        verifyResult.openAllHandsOpenedRealWindow =
          taskWindows.size > beforeHandsWindows && taskWindows.has("allhands:verify-run-1");

        const externalOk = await bridgeTestWin.webContents.executeJavaScript(
          "window.pywebview.api.open_external('https://example.com/electron-shell-verify')"
        );
        verifyResult.openExternalReturnedTrue = externalOk === true;

        // Geometry persistence: force a resize + close, confirm the file
        // really changed to reflect it.
        mainWindow.setBounds({ width: 1300, height: 820, x: 50, y: 50 });
        persistMainWindowGeometry();
        const persisted = readWindowState();
        verifyResult.geometryPersisted = persisted.width === 1300 && persisted.height === 820;

        bridgeTestWin.close();

        // Stage C: real tray + real kill-switch HTTP round trip, toggling
        // and then restoring the mic flag so this smoke test leaves no
        // lasting change to the real shared privacy_state.json.
        verifyResult.trayCreated = tray !== null;
        try {
          const before = await fetchKillSwitchState();
          const toggled = await postKillSwitch("set_mic", !before.mic_enabled);
          const restored = await postKillSwitch("set_mic", before.mic_enabled);
          verifyResult.killSwitchRoundTrip = {
            before: before.mic_enabled,
            afterToggle: toggled.mic_enabled,
            afterRestore: restored.mic_enabled,
            ok: toggled.mic_enabled === !before.mic_enabled && restored.mic_enabled === before.mic_enabled,
          };
          await refreshTray();
        } catch (exc) {
          verifyResult.errors.push(`kill-switch round trip failed: ${exc}`);
        }
      } catch (exc) {
        verifyResult.errors.push(String(exc));
      }
      fs.writeFileSync(verifyResultPath, JSON.stringify(verifyResult, null, 2));
      console.log("VERIFY RESULT:", JSON.stringify(verifyResult, null, 2));
      setTimeout(() => app.quit(), 500);
    })();
  });
}

app.on("window-all-closed", () => {
  // A tray-resident app should keep running after every window closes
  // (macOS convention this app already follows via _brand_native_app's
  // Dock presence today) -- do NOT quit here just because windows closed.
  // Real quit happens via the tray's own "Quit" item or Cmd+Q -> before-quit.
});

app.on("before-quit", () => {
  stopVisionHelpers();
  // Only tear down a server THIS process spawned -- never kill a dev
  // server the user is reusing (REUSE_EXISTING_SERVER / already-running).
  if (!REUSE_EXISTING_SERVER) stopServer();
});
