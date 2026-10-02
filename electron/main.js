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

const { app, BrowserWindow, BrowserView, ipcMain, shell, Tray, Menu, nativeImage, Notification, session, dialog } = require("electron");
const { spawn, execFile } = require("child_process");
const crypto = require("crypto");
const http = require("http");
const fs = require("fs");
const path = require("path");
const policy = require("./policy");

// An isolated copy (a test run, a second profile) can keep every file the shell
// writes (window state, the browser profile, history, bookmarks, downloads list)
// out of the owner's real folder: DOURMOUSE_USER_DATA_DIR, absolute path only. Set
// before the app is ready, so it also wins over a launcher that picked a folder.
if (process.env.DOURMOUSE_USER_DATA_DIR && path.isAbsolute(process.env.DOURMOUSE_USER_DATA_DIR)) {
  app.setPath("userData", process.env.DOURMOUSE_USER_DATA_DIR);
}

// Stage D: real CDP access to this process's own Chromium, the load-
// bearing fact proven live in the migration spike (chromium.connect_over_cdp
// against this exact switch enumerated every real open page, including an
// embedded BrowserView's, and drove a real Playwright navigation on it).
// Must be set before app.whenReady(). Configurable so a second instance
// (dev + a packaged build running side by side) never collides.
const CDP_PORT = parseInt(process.env.DOURMOUSE_ELECTRON_CDP_PORT || "9333", 10);
app.commandLine.appendSwitch("remote-debugging-port", String(CDP_PORT));
// Finding S36, assessed: the DevTools port is kept because the app is driven
// through it (dourmouse/browser_agent.py). Chromium binds it to loopback only;
// the address is pinned explicitly here rather than left to the default. It
// has no authentication, so any local process running as any user can drive
// the app's pages: that is the same trust boundary as the loopback server, and
// is a known, accepted residual risk of using CDP. Browsers cannot reach it
// (Chromium rejects a non-local Host header and cross-origin websockets).
app.commandLine.appendSwitch("remote-debugging-address", "127.0.0.1");

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
// Where the main window opens. DOURMOUSE_ELECTRON_START_PATH lets the owner
// (or a test) open the OS shell ("/shell" or "/") without a rebuild; anything
// that is not a plain same-origin path falls back to the default.
// Finding #154: the OS shell at "/" is the default; /workspace and /console are still served.
const DEFAULT_START_PATH = "/";
const START_PATH = (() => {
  const raw = process.env.DOURMOUSE_ELECTRON_START_PATH || "";
  return /^\/[A-Za-z0-9_.\/#?=&%-]{0,120}$/.test(raw) && !raw.startsWith("//") ? raw : DEFAULT_START_PATH;
})();
// When true, an already-running dev server on PORT is reused instead of
// spawning a new one -- matches how the feasibility spike iterated, and
// is genuinely useful for fast local development. The real packaged app
// (Stage E) always spawns its own bundled server; this is a dev
// convenience only.
const REUSE_EXISTING_SERVER = process.env.DOURMOUSE_ELECTRON_REUSE_SERVER === "1";

let serverProcess = null;
// Finding #162 (A5): per-launch owner secret. Held only in this variable: never in
// process.env, a log or a file. The server reads it once from stdin; the app windows
// present it as a cookie on the default session (never on the pane partition).
const OWNER_SECRET = require("crypto").randomBytes(32).toString("base64url");
let ownerGateArmed = false;
let mainWindow = null;
let mapWindow = null;
let atlasWindow = null;
// task_id -> BrowserWindow, the exact same dedupe-by-id convention as
// dourmouse/desktop.py's DesktopBridge._agent_windows / open_task_window:
// one window per id, reused/focused on repeat calls, recreated if closed.
const taskWindows = new Map();

const PRELOAD = path.join(__dirname, "preload.js");

// Finding S34: deny every web permission by default, for every session. The
// policy (installPermissionPolicy, in the browser pane section below) grants the
// few things the app's own pages use, and only to the app's own origin; a page
// in the browser pane is refused outright even if its origin were ever the app's.

// Finding S35: the console windows carry the privileged preload, so they may
// only ever show the app itself. A link, redirect or window.open to anywhere
// else is stopped here and, for plain http(s), handed to the OS browser.
function lockToAppOrigin(win) {
  const wc = win.webContents;
  const sendOut = (url) => {
    if (policy.externalUrlAllowed(url)) shell.openExternal(url);
  };
  const guard = (evt, url) => {
    if (policy.navigationAllowed(url, PORT)) return;
    evt.preventDefault();
    sendOut(url);
  };
  wc.on("will-navigate", guard);
  wc.on("will-redirect", guard);
  wc.setWindowOpenHandler(({ url }) => {
    if (!policy.navigationAllowed(url, PORT)) sendOut(url);
    return { action: "deny" };
  });
}

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

async function waitForServer(url, timeoutMs = 60000, intervalMs = 300) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await pingServer(url)) return true;
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  return false;
}

function serverPidFile() {
  return path.join(app.getPath("userData"), "server.pid");
}

async function staleOwnerGate() {
  try {
    const state = await fetchJson(`${BASE_URL}/api/security/owner-gate`, { method: "GET" });
    return Boolean(state && state.enforced && !state.owner);
  } catch (_exc) {
    return false;
  }
}

async function stopStaleServer() {
  let pid = 0;
  try {
    pid = parseInt(fs.readFileSync(serverPidFile(), "utf8"), 10);
  } catch (_exc) {
    pid = 0;
  }
  if (!pid || pid === process.pid) {
    throw new Error(
      `A leftover Dourmouse server on port ${PORT} needs its owner secret and this launch does not have it. ` +
      "Quit that process (Activity Monitor, a Python process running dourmouse.webui) and open the app again."
    );
  }
  try {
    process.kill(pid, "SIGTERM");
  } catch (_exc) {
    /* already gone */
  }
  for (let i = 0; i < 40 && (await pingServer(`${BASE_URL}/workspace`)); i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  if (await pingServer(`${BASE_URL}/workspace`)) {
    throw new Error(`The leftover server on port ${PORT} did not stop. Quit it and open the app again.`);
  }
}

async function ensureServer() {
  if (await pingServer(`${BASE_URL}/workspace`)) {
    // Finding #162: a server this app spawned in an earlier run (crash, force quit) still holds
    // the OLD owner secret. Reusing it would lock the owner out of approvals and settings.
    if (!REUSE_EXISTING_SERVER && (await staleOwnerGate())) {
      log("replacing a leftover server that enforces an owner secret this launch does not have");
      await stopStaleServer();
    } else {
      log(`reusing server already answering on ${BASE_URL}`);
      return;
    }
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
      DOURMOUSE_OWNER_GATE: "stdin",
    },
    stdio: ["pipe", "pipe", "pipe"],
  });
  ownerGateArmed = true;
  try {
    fs.writeFileSync(serverPidFile(), String(serverProcess.pid));
  } catch (_exc) {
    /* the pid file only helps the next launch clean up */
  }
  serverProcess.stdin.on("error", () => {});
  serverProcess.stdin.end(`${OWNER_SECRET}\n`);
  serverProcess.stdout.on("data", (d) => process.stdout.write(`[server] ${d}`));
  serverProcess.stderr.on("data", (d) => process.stderr.write(`[server:err] ${d}`));
  serverProcess.on("exit", (code) => log(`server process exited (${code})`));
  const ok = await waitForServer(`${BASE_URL}/workspace`);
  if (!ok) {
    throw new Error(`server did not answer at ${BASE_URL}/workspace within the startup deadline`);
  }
  await armOwnerCookie();
}

// Only when this app spawned the server (a reused server has no secret and stays ungated).
async function armOwnerCookie() {
  if (!ownerGateArmed) return;
  const { session: electronSession } = require("electron");
  for (const host of ["127.0.0.1", "localhost"]) {
    await electronSession.defaultSession.cookies.set({
      url: `http://${host}:${PORT}/`,
      name: "dourmouse_owner",
      value: OWNER_SECRET,
      httpOnly: true,
      sameSite: "strict",
      path: "/",
    });
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
  // Finding #162: the tray's calls to the owner-only routes (vision kill switch) carry the
  // per-launch secret when this app spawned the server.
  if (ownerGateArmed) {
    options = { ...(options || {}), headers: { ...((options && options.headers) || {}), "X-Dourmouse-Owner": OWNER_SECRET } };
  }
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
//
// Phase B1 (Chrome parity, part 1). The pane now holds a LIST OF TABS, each its
// own BrowserView in the same persistent partition (one cookie jar, one profile).
// Exactly one tab is ever attached to the window: the ACTIVE tab. `paneView`
// keeps its old name and always points at that tab's view, so everything that
// drives "the pane" (/navigate, /back, /forward, /reload, the console's address
// bar, the bounds the console reports) drives the active tab with no change.
// Downloads, find in page, per-site zoom, printing, history and bookmarks live
// here too, because they are all properties of the browsing session.
// --------------------------------------------------------------------- //

const PANE_BRIDGE_PORT = parseInt(process.env.DOURMOUSE_ELECTRON_PANE_PORT || "9334", 10);
const PANE_WIDTH_FRACTION = 0.45; // right ~45% of the main window, adjustable later
const MAX_TABS = 30;

let paneView = null; // the ACTIVE tab's BrowserView (null until the pane is first used)
let paneVisible = false;
let browserScreenActive = false;
// Finding #116 (OS-3): the console tells us exactly where its pane's page
// area is, so the BrowserView sits inside the pane's own chrome (address
// bar, back/forward, resize handle) instead of a fixed 45% of the window.
let paneRendererBounds = null;

function paneBounds() {
  if (!mainWindow || mainWindow.isDestroyed()) return { x: 0, y: 0, width: 0, height: 0 };
  const { width, height } = mainWindow.getContentBounds();
  if (paneRendererBounds) {
    const b = paneRendererBounds;
    const x = Math.max(0, Math.min(width - 1, b.x)), y = Math.max(0, Math.min(height - 1, b.y));
    return { x, y, width: Math.max(1, Math.min(width - x, b.width)), height: Math.max(1, Math.min(height - y, b.height)) };
  }
  const paneWidth = Math.round(width * PANE_WIDTH_FRACTION);
  return { x: width - paneWidth, y: 0, width: paneWidth, height };
}

// Finding #160: the pane is the owner's everyday browser, so it must identify
// as the Chrome it is built on. Electron's default user agent adds
// "Electron/x" and the app name, and Google refuses sign-in on embedded
// webviews it detects that way ("content blocked"). This is the same Chromium
// version string the engine already runs; nothing is spoofed beyond dropping
// the two extra tokens.
function chromeUserAgent() {
  const v = process.versions.chrome || "130.0.0.0";
  const os = process.platform === "darwin" ? "Macintosh; Intel Mac OS X 10_15_7"
    : process.platform === "win32" ? "Windows NT 10.0; Win64; x64" : "X11; Linux x86_64";
  return `Mozilla/5.0 (${os}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${v} Safari/537.36`;
}
// A dedicated, persistent partition: the owner's logins and cookies survive
// restarts (like a Chrome profile) and stay apart from the app's own session.
const PANE_PARTITION = "persist:dourmouse-browser";
// Every tab is created with exactly these preferences: no preload (a web page
// never gets the console's bridge), context isolation on, the shared partition.
const TAB_WEB_PREFERENCES = { contextIsolation: true, partition: PANE_PARTITION };
// What a tab with no address shows. Loaded by this file only, never from a caller's
// URL. The fragment lets policy.isBlankUrl() tell it from a real page.
const NEW_TAB_URL = "data:text/html;charset=utf-8," + encodeURIComponent(
  '<!doctype html><meta charset="utf-8"><title>New Tab</title><body style="margin:0;background:#0b120f">'
) + "#dm-newtab";

// ------------------------------ storage ------------------------------ //
// History, bookmarks, per-site zoom and the downloads list are small JSON files
// in the Electron userData folder (next to window-state.json). Writes are atomic
// (temp file then rename) and debounced; a file that cannot be parsed is renamed
// aside, never overwritten silently.

function browserDataDir() {
  return path.join(app.getPath("userData"), "browser");
}

function makeStore(file, fallback) {
  let data = null;
  let timer = null;
  const target = () => path.join(browserDataDir(), file);
  const fresh = () => JSON.parse(JSON.stringify(fallback));
  function get() {
    if (data !== null) return data;
    let raw;
    try {
      raw = fs.readFileSync(target(), "utf8");
    } catch (exc) {
      if (exc && exc.code !== "ENOENT") log(`browser store ${file} unreadable (starting empty):`, exc.message);
      data = fresh();
      return data;
    }
    try {
      const parsed = JSON.parse(raw);
      const okShape = Array.isArray(fallback) ? Array.isArray(parsed) : parsed && typeof parsed === "object" && !Array.isArray(parsed);
      if (!okShape) throw new Error("unexpected shape");
      data = parsed;
    } catch (exc) {
      const aside = `${target()}.corrupt-${Date.now()}`;
      log(`browser store ${file} is corrupt (${exc.message}); keeping it as ${path.basename(aside)} and starting empty`);
      fs.renameSync(target(), aside);
      data = fresh();
    }
    return data;
  }
  function flush() {
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
    if (data === null) return;
    try {
      fs.mkdirSync(browserDataDir(), { recursive: true });
      const tmp = `${target()}.tmp`;
      fs.writeFileSync(tmp, JSON.stringify(data));
      fs.renameSync(tmp, target());
    } catch (exc) {
      log(`browser store ${file} could not be saved:`, exc.message || exc);
    }
  }
  function set(value) {
    data = value;
    if (!timer) timer = setTimeout(flush, 500);
  }
  return { get, set, flush };
}

const historyStore = makeStore("history.json", []);
const bookmarkStore = makeStore("bookmarks.json", []);
const zoomStore = makeStore("zoom.json", {});
const downloadStore = makeStore("downloads.json", []);

function flushBrowserStores() {
  for (const s of [historyStore, bookmarkStore, zoomStore, downloadStore]) s.flush();
}

const newId = () => `${Date.now().toString(36)}${crypto.randomBytes(4).toString("hex")}`;

// -------------------------------- tabs -------------------------------- //

const tabs = new Map(); // id -> tab
let tabOrder = []; // ids, in strip order
let activeTabId = 0;
let nextTabId = 1;
const closedTabs = []; // newest last: { url, title, index }

const activeTab = () => tabs.get(activeTabId) || null;
const liveContents = (tab) => (tab && tab.view && !tab.view.webContents.isDestroyed() ? tab.view.webContents : null);
const tabForContents = (wc) => {
  if (!wc) return null;
  for (const t of tabs.values()) if (t.view.webContents === wc) return t;
  return null;
};

// Finding S34: the pane's pages are denied every permission, whatever they ask.
// B2 owns turning that into per-site prompts; this only has to recognise a tab.
const permissionPolicyInstalled = new WeakSet();
function installPermissionPolicy(ses) {
  if (permissionPolicyInstalled.has(ses)) return;
  permissionPolicyInstalled.add(ses);
  const fromPane = (wc) => !!tabForContents(wc);
  ses.setPermissionRequestHandler((wc, permission, callback, details) => {
    const origin = (details && details.requestingUrl) || (wc && wc.getURL()) || "";
    callback(!fromPane(wc) && policy.permissionAllowed(permission, origin, PORT));
  });
  ses.setPermissionCheckHandler((wc, permission, requestingOrigin) => {
    return !fromPane(wc) && policy.permissionAllowed(permission, requestingOrigin, PORT);
  });
}

// The first tab is the one dourmouse/browser_agent.py attaches to: it finds "the"
// pane by looking for the one still-blank about:blank page at attach time, so the
// first tab is created at about:blank and nothing else is ever created there
// while other tabs exist (a new tab shows NEW_TAB_URL instead).
function ensurePaneView() {
  if (paneView && !paneView.webContents.isDestroyed()) return paneView;
  paneView = new BrowserView({ webPreferences: TAB_WEB_PREFERENCES });
  paneView.webContents.session.setUserAgent(chromeUserAgent());
  installPermissionPolicy(paneView.webContents.session);
  installDownloadHandler(paneView.webContents.session);
  const tab = registerTab(paneView, "about:blank");
  activeTabId = tab.id;
  return paneView;
}

function loadInTab(wc, url) {
  const owner = tabForContents(wc);
  if (owner && policy.paneUrlAllowed(url)) owner.requested = url; // what the tab was last asked to open, for a crash recovery
  // did-fail-load reports a failed load to the console; the promise rejection
  // carries nothing more, so it is consumed here rather than left unhandled.
  wc.loadURL(url).catch((exc) => log("tab load did not finish:", exc && exc.message ? exc.message : exc));
}

function registerTab(view, url, insertAt, openerId = 0) {
  const wc = view.webContents;
  const tab = {
    id: nextTabId++, view, openerId, favicon: "", faviconSrc: "", fail: null, find: null, blockedPopups: 0,
    popupLimit: policy.createLimiter(5, 10000), downloadLimit: policy.createLimiter(10, 60000), recoveries: policy.createLimiter(2, 60000), requested: "", lastHost: "",
  };
  tabs.set(tab.id, tab);
  if (Number.isInteger(insertAt) && insertAt >= 0 && insertAt <= tabOrder.length) tabOrder.splice(insertAt, 0, tab.id);
  else tabOrder.push(tab.id);
  wc.setUserAgent(chromeUserAgent());
  // A page that sets no background gets Chrome's white canvas, not the console behind it.
  view.setBackgroundColor("#ffffffff");
  wireTab(tab);
  loadInTab(wc, url);
  return tab;
}

function wireTab(tab) {
  const wc = tab.view.webContents;
  const alive = () => tabs.get(tab.id) === tab && !wc.isDestroyed();
  // Finding #153: the BROWSER screen shows why a page did not load. A new load
  // clears the last failure; a failed main frame records the engine's own
  // words. ERR_ABORTED (-3) is a navigation replaced by another, not a failure.
  wc.on("did-start-loading", () => { tab.fail = null; });
  wc.on("did-fail-load", (_evt, errorCode, errorDescription, validatedURL, isMainFrame) => {
    if (isMainFrame && errorCode !== -3) {
      tab.fail = { code: errorCode, description: String(errorDescription || "").slice(0, 200), url: String(validatedURL || "").slice(0, 2000) };
    }
  });
  wc.on("render-process-gone", (_evt, details) => {
    log("tab renderer gone:", JSON.stringify(details || {}));
    const reason = (details && details.reason) || "unknown";
    tab.fail = { code: -1, description: `The page process ended (${reason})`, url: alive() ? wc.getURL() : "" };
    // A renderer that died of its own accord (not one the owner or the OS killed) gets
    // one fresh process, to the address it was on or was asked for. Two tries a minute
    // at most, so a page that crashes every time shows the error instead of looping.
    // (Seen live: a pane tab created while a CDP client such as the browser agent is
    // already attached can lose its first renderer; this is how that tab recovers.)
    if (alive() && policy.isCrashReason(reason) && tab.recoveries.allow()) {
      const current = wc.getURL();
      const target = policy.paneUrlAllowed(current) ? current : tab.requested || "about:blank";
      setTimeout(() => { if (alive()) loadInTab(wc, target); }, 250);
    }
    schedulePush();
  });
  wc.on("did-navigate", () => {
    if (!alive()) return;
    tab.find = null;
    const host = policy.zoomHost(wc.getURL());
    if (host !== tab.lastHost) {
      tab.lastHost = host;
      tab.favicon = "";
      tab.faviconSrc = "";
    }
    applySiteZoom(tab);
    recordVisit(tab);
  });
  wc.on("dom-ready", () => { if (alive()) applySiteZoom(tab); });
  wc.on("page-title-updated", (_evt, title) => {
    if (!alive()) return;
    const url = wc.getURL();
    if (policy.paneUrlAllowed(url)) {
      const before = historyStore.get();
      const after = policy.updateVisitTitle(before, url, title);
      if (after !== before) historyStore.set(after);
    }
  });
  wc.on("page-favicon-updated", (_evt, favicons) => {
    const src = Array.isArray(favicons) ? favicons.find((f) => policy.paneUrlAllowed(f)) : "";
    if (!src || src === tab.faviconSrc) return;
    tab.faviconSrc = src;
    faviconData(wc.session, src).then((dataUrl) => {
      if (alive() && tab.faviconSrc === src) {
        tab.favicon = dataUrl;
        schedulePush();
      }
    });
  });
  wc.on("found-in-page", (_evt, result) => {
    if (!tab.find || !result) return;
    tab.find = { text: tab.find.text, active: result.activeMatchOrdinal || 0, matches: result.matches || 0 };
    schedulePush();
  });
  for (const ev of ["did-navigate", "did-navigate-in-page", "page-title-updated", "did-start-loading",
                    "did-stop-loading", "did-fail-load", "audio-state-changed"]) {
    wc.on(ev, schedulePush);
  }
  // target=_blank, window.open and a middle or Cmd click open a new TAB in the same
  // profile; only http(s), same rule as /navigate. A page that opens them in a loop
  // is cut off after five in ten seconds, and the strip never holds more than
  // MAX_TABS. (A pop-up that needs window.opener, some sign-in and payment flows,
  // cannot work as a tab: see the finding for B1.)
  wc.setWindowOpenHandler(({ url, disposition }) => {
    if (policy.paneUrlAllowed(url) && tabs.size < MAX_TABS && tab.popupLimit.allow()) {
      openTab(url, { background: disposition === "background-tab", afterId: tab.id, opener: tab.id });
    } else {
      tab.blockedPopups += 1;
    }
    return { action: "deny" };
  });
  wc.on("before-input-event", (event, input) => {
    if (input.type === "keyDown" && handlePaneShortcut(tab, input)) event.preventDefault();
  });
}

function openTab(url, { background = false, afterId = 0, at: wantedAt, opener = 0 } = {}) {
  ensurePaneView();
  if (tabs.size >= MAX_TABS) return null;
  const view = new BrowserView({ webPreferences: TAB_WEB_PREFERENCES });
  let at = tabOrder.length;
  if (Number.isInteger(wantedAt)) {
    at = Math.max(0, Math.min(tabOrder.length, wantedAt));
  } else if (afterId && tabOrder.includes(afterId)) {
    // Right after the tab that opened it, and after any it already opened, so a run
    // of links from one page keeps their order (Chrome's rule).
    at = tabOrder.indexOf(afterId) + 1;
    while (at < tabOrder.length && tabs.get(tabOrder[at]).openerId === opener && opener) at += 1;
  }
  const tab = registerTab(view, policy.paneUrlAllowed(url) ? url : NEW_TAB_URL, at, opener);
  if (!background) activateTab(tab.id);
  schedulePush();
  return tab;
}

function activateTab(id) {
  const tab = tabs.get(Number(id));
  if (!tab) return false;
  const previous = activeTab();
  if (previous && previous !== tab && previous.find && liveContents(previous)) stopFind(previous);
  activeTabId = tab.id;
  paneView = tab.view;
  attachActiveView();
  schedulePush();
  return true;
}

// Only the active tab's view is ever on the window.
function attachActiveView() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  const attached = mainWindow.getBrowserViews();
  for (const t of tabs.values()) {
    if (t.view !== paneView && attached.includes(t.view)) mainWindow.removeBrowserView(t.view);
  }
  if (paneVisible && paneView) {
    if (!attached.includes(paneView)) mainWindow.addBrowserView(paneView);
    paneView.setBounds(paneBounds());
    paneView.setAutoResize({ width: false, height: !paneRendererBounds }); // explicit bounds win when the console sends them
  }
}

function closeTab(id) {
  const tab = tabs.get(Number(id));
  if (!tab) return false;
  const wc = liveContents(tab);
  const index = tabOrder.indexOf(tab.id);
  const url = wc ? wc.getURL() : "";
  if (policy.paneUrlAllowed(url)) {
    closedTabs.push({ url, title: wc.getTitle(), index });
    if (closedTabs.length > 20) closedTabs.shift();
  }
  const wasActive = tab.id === activeTabId;
  tabOrder.splice(index, 1);
  tabs.delete(tab.id);
  if (mainWindow && !mainWindow.isDestroyed() && mainWindow.getBrowserViews().includes(tab.view)) {
    mainWindow.removeBrowserView(tab.view);
  }
  if (wc) wc.close();
  if (tabOrder.length === 0) {
    // The pane is never empty. The replacement is created at about:blank so the
    // browser agent can find it again, exactly as it found the first tab.
    paneView = null;
    ensurePaneView();
    attachActiveView();
  } else if (wasActive) {
    activateTab(tabOrder[Math.min(index, tabOrder.length - 1)]);
  }
  schedulePush();
  return true;
}

function reopenClosedTab() {
  const last = closedTabs.pop();
  if (!last) return null;
  return openTab(last.url, { at: last.index });
}

function selectTabByOffset(step) {
  if (tabOrder.length < 2) return;
  const i = tabOrder.indexOf(activeTabId);
  activateTab(tabOrder[(i + step + tabOrder.length) % tabOrder.length]);
}

// ------------------------------ state push ------------------------------ //

function tabInfo(tab) {
  const wc = liveContents(tab);
  const url = wc ? wc.getURL() : "";
  const blank = policy.isBlankUrl(url);
  return {
    id: tab.id, url: blank ? "" : url, title: blank || !wc ? "" : wc.getTitle(), favicon: tab.favicon,
    loading: wc ? wc.isLoading() : false, active: tab.id === activeTabId, audible: wc ? wc.isCurrentlyAudible() : false,
    error: tab.fail ? { code: tab.fail.code, description: tab.fail.description, url: tab.fail.url } : null,
  };
}

function paneState() {
  const tab = activeTab();
  const wc = liveContents(tab);
  const nav = wc && wc.navigationHistory;
  const can = (fn, legacy) => {
    try { return nav && typeof nav[fn] === "function" ? nav[fn]() : wc[legacy](); } catch { return false; }
  };
  const url = wc ? wc.getURL() : "";
  const blank = policy.isBlankUrl(url);
  return {
    open: paneVisible, url: blank ? "" : url, title: wc && !blank ? wc.getTitle() : "",
    loading: wc ? wc.isLoading() : false,
    canGoBack: wc ? can("canGoBack", "canGoBack") : false,
    canGoForward: wc ? can("canGoForward", "canGoForward") : false,
    error: tab ? tab.fail : null,
    tabs: tabOrder.map((id) => tabInfo(tabs.get(id))),
    activeTab: activeTabId,
    zoom: wc ? wc.getZoomFactor() : 1,
    find: tab && tab.find ? { ...tab.find } : null,
    closedTabs: closedTabs.length,
    blockedPopups: tab ? tab.blockedPopups : 0,
  };
}

function pushPaneStateNow() {
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send("pane:state", paneState());
}
// A page fires title, loading and navigation events in bursts; the console gets
// one state a few milliseconds later, not one per event.
let paneStateTimer = null;
function schedulePush() {
  if (paneStateTimer) return;
  paneStateTimer = setTimeout(() => {
    paneStateTimer = null;
    pushPaneStateNow();
  }, 30);
}
const pushPaneState = schedulePush;

function showPane() {
  if (!mainWindow || mainWindow.isDestroyed()) return false;
  ensurePaneView();
  paneVisible = true;
  attachActiveView();
  pushPaneState();
  return true;
}

function hidePane() {
  if (!mainWindow || mainWindow.isDestroyed() || !paneView) {
    paneVisible = false;
    return true;
  }
  for (const t of tabs.values()) {
    if (mainWindow.getBrowserViews().includes(t.view)) mainWindow.removeBrowserView(t.view);
  }
  paneVisible = false;
  pushPaneState();
  return true;
}

// ----------------------- favicons, zoom, find, print ----------------------- //

const faviconCache = new Map(); // address -> small PNG data URL, "" when it could not be used
async function faviconData(ses, src) {
  if (faviconCache.has(src)) return faviconCache.get(src);
  let out = "";
  try {
    // No cookies, a short deadline, a size cap, and only an image ever leaves this
    // function: the console shows it as <img src="data:...">, nothing else.
    const res = await ses.fetch(src, { credentials: "omit", signal: AbortSignal.timeout(5000) });
    const type = String(res.headers.get("content-type") || "").toLowerCase();
    if (res.ok && (type.startsWith("image/") || type === "")) {
      const buf = Buffer.from(await res.arrayBuffer());
      if (buf.length > 0 && buf.length <= 262144) {
        const img = nativeImage.createFromBuffer(buf);
        if (!img.isEmpty()) out = img.resize({ width: 32, height: 32 }).toDataURL();
      }
    }
  } catch (exc) {
    log("favicon not loaded (non-fatal):", exc && exc.message ? exc.message : exc);
  }
  if (faviconCache.size >= 300) faviconCache.delete(faviconCache.keys().next().value);
  faviconCache.set(src, out);
  return out;
}

function applySiteZoom(tab) {
  const wc = liveContents(tab);
  if (!wc) return;
  const wanted = zoomStore.get()[policy.zoomHost(wc.getURL())] || 1;
  if (Math.abs(wc.getZoomFactor() - wanted) > 0.001) wc.setZoomFactor(wanted);
}

function setTabZoom(tab, factor) {
  const wc = liveContents(tab);
  if (!wc) return 1;
  const f = Math.max(0.25, Math.min(5, factor));
  wc.setZoomFactor(f);
  const host = policy.zoomHost(wc.getURL());
  if (host) {
    const table = { ...zoomStore.get() };
    if (Math.abs(f - 1) < 0.001) delete table[host];
    else table[host] = f;
    zoomStore.set(table);
  }
  schedulePush();
  return f;
}

function stopFind(tab) {
  const wc = liveContents(tab);
  if (wc) wc.stopFindInPage("clearSelection");
  tab.find = null;
}

function printTab(tab, { pdf = false } = {}) {
  const wc = liveContents(tab);
  if (!wc) return Promise.resolve({ ok: false, error: "no page to print" });
  if (!pdf) {
    // The system print dialog (it has "Save as PDF" in its own menu). The owner
    // finishes or cancels it; nothing prints without that.
    wc.print({ silent: false, printBackground: true });
    return Promise.resolve({ ok: true });
  }
  return wc.printToPDF({ printBackground: true }).then((buf) => {
    const dir = downloadsDir();
    fs.mkdirSync(dir, { recursive: true });
    const base = policy.safeFileName((wc.getTitle() || "page").slice(0, 80)) + ".pdf";
    const name = policy.uniqueFileName(base, (n) => fs.existsSync(path.join(dir, n)));
    const target = path.join(dir, name);
    fs.writeFileSync(target, buf);
    addDownloadRecord({ url: wc.getURL(), filename: name, path: target, mime: "application/pdf", state: "completed", received: buf.length, total: buf.length });
    return { ok: true, path: target };
  }, (exc) => ({ ok: false, error: String((exc && exc.message) || exc) }));
}

function sendConsoleCommand(cmd) {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  // Keys typed in a page go to the page's own webContents; the console's find box
  // and address bar can only take them once the console has the keyboard.
  if (cmd === "find" || cmd === "address") mainWindow.webContents.focus();
  mainWindow.webContents.send("pane:command", cmd);
}

// Chrome's shortcuts, for when the PAGE has the keyboard (the console binds the
// same keys itself through ctx.keys while the console has it). Returns true when
// the key was ours. Never touches a key the page needs for itself (no Cmd+C/V/X/A/Z).
function handlePaneShortcut(tab, input) {
  if (input.control && !input.meta && !input.alt && input.key === "Tab") {
    selectTabByOffset(input.shift ? -1 : 1);
    return true;
  }
  const mod = process.platform === "darwin" ? input.meta : input.control;
  if (!mod || input.alt) return false;
  const key = String(input.key || "").toLowerCase();
  const wc = liveContents(tab);
  if (!wc) return false;
  if (key === "t") { if (input.shift) reopenClosedTab(); else openTab(null); return true; }
  if (key === "w" && !input.shift) { closeTab(tab.id); return true; }
  if (key === "l") { sendConsoleCommand("address"); return true; }
  if (key === "f") { sendConsoleCommand("find"); return true; }
  if (key === "g") { sendConsoleCommand(input.shift ? "find-prev" : "find-next"); return true; }
  if (key === "p" && !input.shift) { printTab(tab); return true; }
  if (key === "r" && !input.shift) { wc.reload(); return true; }
  if (key === "[" || key === "]") {
    const nav = wc.navigationHistory;
    if (key === "[" && nav.canGoBack()) nav.goBack();
    else if (key === "]" && nav.canGoForward()) nav.goForward();
    return true;
  }
  if (key === "=" || key === "+") { setTabZoom(tab, policy.nextZoom(wc.getZoomFactor(), "in")); return true; }
  if (key === "-" || key === "_") { setTabZoom(tab, policy.nextZoom(wc.getZoomFactor(), "out")); return true; }
  if (key === "0") { setTabZoom(tab, 1); return true; }
  if (/^[1-9]$/.test(key) && !input.shift) {
    const n = parseInt(key, 10);
    activateTab(n === 9 ? tabOrder[tabOrder.length - 1] : tabOrder[n - 1]);
    return true;
  }
  return false;
}

// ----------------------------- history, bookmarks ----------------------------- //

function recordVisit(tab) {
  const wc = liveContents(tab);
  if (!wc) return;
  const url = wc.getURL();
  if (!policy.paneUrlAllowed(url)) return;
  const before = historyStore.get();
  const after = policy.addVisit(before, { id: newId(), url, title: wc.getTitle(), at: Date.now() });
  if (after !== before) historyStore.set(after);
}

function listHistory(query, limit) {
  return policy.searchHistory(historyStore.get(), query, limit);
}

function addHistoryEntry(url, title) {
  if (!policy.paneUrlAllowed(url)) return null;
  const before = historyStore.get();
  const after = policy.addVisit(before, { id: newId(), url, title, at: Date.now() }, { dedupeMs: 0 });
  historyStore.set(after);
  return after[0] || null;
}

function removeHistory(sel) {
  const r = policy.removeFrom(historyStore.get(), sel);
  if (r.removed) historyStore.set(r.list);
  return r.removed;
}

function listBookmarks() {
  return bookmarkStore.get();
}

function addBookmarkEntry(url, title) {
  const r = policy.addBookmark(bookmarkStore.get(), { id: newId(), url, title, at: Date.now() });
  if (r.entry) bookmarkStore.set(r.list);
  return r;
}

function removeBookmarkEntry(sel) {
  const r = policy.removeFrom(bookmarkStore.get(), sel);
  if (r.removed) bookmarkStore.set(r.list);
  return r.removed;
}

// -------------------------------- downloads -------------------------------- //
// Every download in the pane's profile lands in ~/Downloads (the folder the
// Downloads watcher, dourmouse/security/downloads.py, already assesses) under a name
// that never overwrites anything. Chromium writes it as <name>.crdownload until it
// is whole, and the watcher skips that suffix, so a half-written file is never
// assessed. When it completes it carries the quarantine flag and its origin, like a
// browser's downloads, so Gatekeeper and the watcher see where it came from.
// NOTHING here ever opens a downloaded file by itself: opening is an explicit click
// in the console, and refused for anything that runs code (policy.isOpenableDownload).

let downloads = null; // newest first
const downloadItems = new Map(); // id -> DownloadItem, only while it is in flight
const downloadHandlerInstalled = new WeakSet();
let downloadsTimer = null;

function downloadsDir() {
  const override = process.env.DOURMOUSE_DOWNLOADS_DIR || "";
  return override && path.isAbsolute(override) ? override : app.getPath("downloads");
}

function downloadList() {
  if (downloads) return downloads;
  // A record left "progressing" by a previous run can never finish: say so.
  downloads = downloadStore.get().map((r) => (r.state === "progressing" ? { ...r, state: "interrupted", error: "the app closed during the download" } : r));
  return downloads;
}

function persistDownloads() {
  downloadStore.set(downloadList().filter((r) => r.state !== "progressing").slice(0, 100));
}

function pushDownloads() {
  if (downloadsTimer) return;
  downloadsTimer = setTimeout(() => {
    downloadsTimer = null;
    if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send("pane:downloads", downloadView());
  }, 100);
}

function downloadView() {
  return downloadList().slice(0, 100).map((r) => ({
    id: r.id, filename: r.filename, url: String(r.url || "").slice(0, 500), state: r.state, paused: Boolean(r.paused),
    received: r.received || 0, total: r.total > 0 ? r.total : 0, percent: policy.percent(r.received, r.total),
    startedAt: r.startedAt, endedAt: r.endedAt || 0, mime: r.mime || "", path: r.path, error: r.error || "",
    quarantined: r.quarantined === undefined ? null : r.quarantined, openable: r.state === "completed" && policy.isOpenableDownload(r.filename),
  }));
}

function addDownloadRecord(fields) {
  const rec = { id: newId(), startedAt: Date.now(), endedAt: Date.now(), paused: false, error: "", quarantined: null, ...fields };
  downloadList().unshift(rec);
  persistDownloads();
  pushDownloads();
  return rec;
}

function markDownloaded(rec) {
  if (process.platform !== "darwin") {
    rec.quarantined = false;
    return Promise.resolve();
  }
  const flag = policy.quarantineValue(Date.now(), app.getName() || "Dourmouse", crypto.randomUUID().toUpperCase());
  const run = (args) => new Promise((resolve) => {
    execFile("/usr/bin/xattr", args, { timeout: 10000 }, (err) => resolve(err ? String(err.message || err) : ""));
  });
  return run(["-w", "com.apple.quarantine", flag, rec.path]).then((qErr) => {
    rec.quarantined = !qErr;
    if (qErr) log("download not quarantined:", qErr);
    return run(["-w", "com.apple.metadata:kMDItemWhereFroms", policy.whereFromPlist([rec.url, rec.referrer]), rec.path]);
  }).then((wErr) => {
    if (wErr) log("download origin not recorded:", wErr);
  });
}

function installDownloadHandler(ses) {
  if (downloadHandlerInstalled.has(ses)) return;
  downloadHandlerInstalled.add(ses);
  ses.on("will-download", (event, item, sourceContents) => {
    const source = tabForContents(sourceContents);
    if (source && !source.downloadLimit.allow()) {
      // Ten in a minute from one tab is a page in a loop, not an owner.
      event.preventDefault();
      log("download refused: this tab started too many in a minute");
      return;
    }
    const dir = downloadsDir();
    try {
      fs.mkdirSync(dir, { recursive: true });
    } catch (exc) {
      event.preventDefault();
      log("download refused: the downloads folder is not usable:", exc.message || exc);
      return;
    }
    const wanted = policy.safeFileName(item.getFilename());
    const taken = (n) => fs.existsSync(path.join(dir, n)) || fs.existsSync(path.join(dir, `${n}.crdownload`));
    const name = policy.uniqueFileName(wanted, taken);
    const target = path.join(dir, name);
    // Written as <name>.crdownload and renamed when whole: Electron writes straight to
    // the path it is given, and the Downloads watcher only skips that suffix.
    const partial = `${target}.crdownload`;
    item.setSavePath(partial);
    const rec = {
      id: newId(), url: item.getURL(), referrer: sourceContents && !sourceContents.isDestroyed() ? sourceContents.getURL() : "",
      filename: name, path: target, mime: item.getMimeType(), state: "progressing", paused: false, received: 0,
      total: item.getTotalBytes(), startedAt: Date.now(), endedAt: 0, error: "", quarantined: null,
    };
    downloadList().unshift(rec);
    downloadItems.set(rec.id, item);
    item.on("updated", (_e, state) => {
      rec.received = item.getReceivedBytes();
      rec.total = item.getTotalBytes();
      rec.paused = item.isPaused();
      rec.state = state === "interrupted" ? "interrupted" : "progressing";
      pushDownloads();
    });
    item.once("done", (_e, state) => {
      downloadItems.delete(rec.id);
      rec.received = item.getReceivedBytes();
      rec.total = item.getTotalBytes();
      rec.endedAt = Date.now();
      rec.paused = false;
      rec.state = state === "completed" ? "completed" : state === "cancelled" ? "cancelled" : "interrupted";
      if (rec.state === "completed") {
        try {
          let final = rec.path;
          if (fs.existsSync(final)) {
            rec.filename = policy.uniqueFileName(rec.filename, (n) => fs.existsSync(path.join(dir, n)));
            final = path.join(dir, rec.filename);
          }
          fs.renameSync(partial, final);
          rec.path = final;
        } catch (exc) {
          rec.state = "interrupted";
          rec.error = `the file could not be finished: ${exc.message || exc}`;
        }
      } else if (rec.state === "interrupted") {
        rec.error = "the download did not finish";
      }
      const finish = () => {
        persistDownloads();
        pushDownloads();
      };
      if (rec.state === "completed") markDownloaded(rec).then(finish);
      else finish();
    });
    pushDownloads();
  });
}

async function downloadAction(id, action) {
  const rec = downloadList().find((r) => r.id === id);
  if (!rec) return { ok: false, error: "no such download" };
  const item = downloadItems.get(id);
  if (action === "cancel") {
    if (!item) return { ok: false, error: "that download is not running" };
    item.cancel();
    return { ok: true };
  }
  if (action === "pause" || action === "resume") {
    if (!item) return { ok: false, error: "that download is not running" };
    if (action === "pause") item.pause();
    else if (item.canResume()) item.resume();
    else return { ok: false, error: "that download cannot be resumed" };
    return { ok: true };
  }
  if (action === "remove") {
    if (item) return { ok: false, error: "cancel it first" };
    downloads = downloadList().filter((r) => r.id !== id);
    persistDownloads();
    pushDownloads();
    return { ok: true };
  }
  if (action === "open" || action === "reveal") {
    if (rec.state !== "completed") return { ok: false, error: "the download has not finished" };
    if (!fs.existsSync(rec.path)) return { ok: false, error: "the file is no longer there" };
    if (action === "reveal") {
      shell.showItemInFolder(rec.path);
      return { ok: true };
    }
    if (!policy.isOpenableDownload(rec.filename)) {
      return { ok: false, error: "this kind of file can run code, so it is not opened from here. Use Show in folder." };
    }
    const err = await shell.openPath(rec.path);
    return err ? { ok: false, error: err } : { ok: true };
  }
  return { ok: false, error: "unknown action" };
}

// ------------------------------- console IPC ------------------------------- //

// The console window's own keys while the BROWSER screen is showing and the console
// (not the page) has the keyboard. Only Cmd+W is claimed here: every other shortcut
// is bound by the screen itself, but Cmd+W is a window-menu key the page cannot stop.
function wireConsoleKeys(win) {
  win.webContents.on("before-input-event", (event, input) => {
    if (!browserScreenActive || input.type !== "keyDown" || input.alt || input.shift) return;
    const mod = process.platform === "darwin" ? input.meta : input.control;
    if (mod && String(input.key).toLowerCase() === "w" && activeTab()) {
      event.preventDefault();
      closeTab(activeTabId);
    }
  });
}

// Only the console window may open or reveal a file.
function fromConsole(evt) {
  return Boolean(mainWindow && !mainWindow.isDestroyed() && evt.sender === mainWindow.webContents && policy.isAppOrigin(evt.sender.getURL(), PORT));
}

// The console's own controls (finding #116). Same URL rule as the bridge.
ipcMain.handle("pane:navigate", (_evt, url) => {
  if (!policy.paneUrlAllowed(url)) return { ok: false, error: "url must be a real http(s) URL" };
  const view = ensurePaneView();
  if (!paneVisible) showPane();
  loadInTab(view.webContents, url);
  return { ok: true };
});
ipcMain.handle("pane:nav", (_evt, what) => {
  if (!paneView) return false;
  const wc = paneView.webContents, nav = wc.navigationHistory;
  if (what === "back" && paneState().canGoBack) (nav && nav.goBack ? nav.goBack() : wc.goBack());
  else if (what === "forward" && paneState().canGoForward) (nav && nav.goForward ? nav.goForward() : wc.goForward());
  else if (what === "reload") wc.reload();
  else if (what === "stop") wc.stop();
  return true;
});
ipcMain.handle("pane:bounds", (_evt, r) => {
  const ok = r && ["x", "y", "width", "height"].every((k) => Number.isFinite(r[k]));
  paneRendererBounds = ok ? { x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height) } : null;
  if (paneView && paneVisible) paneView.setBounds(paneBounds());
  return true;
});
ipcMain.handle("pane:show", () => showPane());
ipcMain.handle("pane:hide", () => hidePane());
ipcMain.handle("pane:state", () => paneState());
// The console tells the shell while its BROWSER screen is the one showing, so that
// Cmd+W closes the tab there (as in Chrome) instead of closing the whole window.
ipcMain.handle("pane:screen", (evt, active) => {
  if (fromConsole(evt)) browserScreenActive = active === true;
  return true;
});
ipcMain.handle("pane:tab-new", (_evt, url) => {
  const blank = url === undefined || url === null || url === "";
  // Same rule as /tabs/new: an address that is not a real http(s) page is refused,
  // not quietly swapped for an empty tab.
  if (!blank && !policy.paneUrlAllowed(url)) return { ok: false, error: "url must be a real http(s) URL" };
  const tab = openTab(blank ? null : url);
  return tab ? { ok: true, id: tab.id } : { ok: false, error: tabs.size >= MAX_TABS ? `at most ${MAX_TABS} tabs` : "could not open a tab" };
});
ipcMain.handle("pane:tab-close", (_evt, id) => ({ ok: closeTab(id) }));
ipcMain.handle("pane:tab-select", (_evt, id) => ({ ok: activateTab(id) }));
ipcMain.handle("pane:tab-reopen", () => {
  const tab = reopenClosedTab();
  return tab ? { ok: true, id: tab.id } : { ok: false, error: "no closed tab to reopen" };
});
ipcMain.handle("pane:find", (_evt, text, opts) => {
  const tab = activeTab();
  const wc = liveContents(tab);
  if (!wc) return { ok: false, error: "no page" };
  const q = typeof text === "string" ? text.slice(0, 200) : "";
  if (!q) {
    stopFind(tab);
    schedulePush();
    return { ok: true };
  }
  const o = opts && typeof opts === "object" ? opts : {};
  const same = tab.find && tab.find.text === q;
  tab.find = { text: q, active: same ? tab.find.active : 0, matches: same ? tab.find.matches : 0 };
  // Electron's naming is the reverse of what it reads like: findNext TRUE begins a new
  // search session (a new word), FALSE steps to the next or previous match of the
  // current one. The console says "next" for the second case, so it is inverted here.
  const continuing = o.findNext === true && Boolean(same);
  wc.findInPage(q, { forward: o.forward !== false, findNext: !continuing, matchCase: o.matchCase === true });
  return { ok: true };
});
ipcMain.handle("pane:find-stop", () => {
  const tab = activeTab();
  if (tab && liveContents(tab)) stopFind(tab);
  schedulePush();
  return { ok: true };
});
ipcMain.handle("pane:focus-page", () => {
  const wc = liveContents(activeTab());
  if (wc && paneVisible) wc.focus();
  return true;
});
ipcMain.handle("pane:zoom", (_evt, action) => {
  const tab = activeTab();
  const wc = liveContents(tab);
  if (!wc) return { ok: false, error: "no page" };
  if (action !== "in" && action !== "out" && action !== "reset") return { ok: false, error: "action must be in, out or reset" };
  return { ok: true, zoom: setTabZoom(tab, policy.nextZoom(wc.getZoomFactor(), action)) };
});
ipcMain.handle("pane:print", (_evt, opts) => printTab(activeTab(), { pdf: Boolean(opts && opts.pdf) }));
ipcMain.handle("pane:downloads", () => downloadView());
ipcMain.handle("pane:download-action", (evt, id, action) => {
  if (typeof id !== "string" || typeof action !== "string") return { ok: false, error: "bad request" };
  if ((action === "open" || action === "reveal") && !fromConsole(evt)) return { ok: false, error: "only the console may do that" };
  return downloadAction(id, action);
});
ipcMain.handle("pane:downloads-clear", () => {
  downloads = downloadList().filter((r) => r.state === "progressing");
  persistDownloads();
  pushDownloads();
  return { ok: true };
});

// ------------------------------- pane bridge ------------------------------- //

function startPaneBridge() {
  const server = http.createServer((req, res) => {
    const respond = (status, body) => {
      res.writeHead(status, { "Content-Type": "application/json" });
      res.end(JSON.stringify(body));
    };
    // Localhost-only by construction (bound to 127.0.0.1 below, matching
    // this whole app's existing 127.0.0.1-only posture) -- no auth needed
    // for the same reason webui.py's own loopback-only endpoints don't.
    //
    // Finding #135: "localhost only" does not keep out a web page, because a
    // browser will connect to 127.0.0.1 on any page's behalf. The callers
    // here are local programs (browser_agent.py, the Python tools), which
    // send neither Origin nor Sec-Fetch-Site; a browser always sends at least
    // one of them on a cross-site request, and names the attacker's own host
    // in Host under DNS rebinding. Refuse both.
    const hostHeader = String(req.headers.host || "").toLowerCase();
    const ownHost = hostHeader === `127.0.0.1:${PANE_BRIDGE_PORT}` || hostHeader === `localhost:${PANE_BRIDGE_PORT}`;
    if (!ownHost || req.headers.origin !== undefined || req.headers["sec-fetch-site"] !== undefined) {
      respond(403, { ok: false, error: "browser-originated requests are not accepted here" });
      return;
    }
    let parsed;
    try {
      parsed = new URL(req.url, "http://127.0.0.1");
    } catch {
      respond(400, { ok: false, error: "bad request address" });
      return;
    }
    const route = parsed.pathname;
    const params = parsed.searchParams;
    // A small JSON body, read once. A body that is not JSON is an empty one: each
    // route then refuses the missing field itself with its own message.
    const withBody = (fn) => {
      let body = "";
      req.on("data", (c) => { body += c; if (body.length > 8192) req.destroy(); });
      req.on("end", () => {
        let obj = {};
        try { obj = JSON.parse(body || "{}"); } catch { obj = {}; }
        try {
          fn(obj && typeof obj === "object" ? obj : {});
        } catch (err) {
          respond(500, { ok: false, error: String(err && err.message || err) });
        }
      });
    };
    const isGet = req.method === "GET";
    const isPost = req.method === "POST";
    if (isGet && route === "/status") {
      respond(200, { active: paneVisible, cdpEndpoint: `http://127.0.0.1:${CDP_PORT}`, tabCount: tabs.size, activeTab: activeTabId });
    } else if (isPost && route === "/show") {
      respond(200, { ok: showPane() });
    } else if (isPost && route === "/hide") {
      respond(200, { ok: hidePane() });
    } else if (isPost && route.startsWith("/navigate")) {
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
      // So: no proxy, no rewriting, real cookies, real sessions. With tabs this
      // navigates the ACTIVE tab.
      withBody((obj) => {
        const url = obj.url;
        // Only real http(s). Refusing file:// and data:// here matters: this
        // bridge is reachable from any local process, and a BrowserView
        // pointed at file:// would read the user's disk.
        if (!policy.paneUrlAllowed(url)) {
          respond(400, { ok: false, error: "url must be a real http(s) URL" });
          return;
        }
        const view = ensurePaneView();
        if (!paneVisible) showPane();
        loadInTab(view.webContents, url);
        respond(200, { ok: true, url });
      });
    } else if (isPost && /^\/(back|forward|reload)$/.test(route)) {
      // Real history, which the iframe pane could never have: its own code
      // comments note cross-origin history "isn't reachable from the parent
      // page". A BrowserView owns its history outright.
      if (!paneView) { respond(409, { ok: false, error: "pane not open" }); return; }
      const wc = paneView.webContents;
      const nav = wc.navigationHistory;
      try {
        if (route === "/back") {
          if (nav && typeof nav.canGoBack === "function" ? nav.canGoBack() : wc.canGoBack())
            (nav && nav.goBack ? nav.goBack() : wc.goBack());
        } else if (route === "/forward") {
          if (nav && typeof nav.canGoForward === "function" ? nav.canGoForward() : wc.canGoForward())
            (nav && nav.goForward ? nav.goForward() : wc.goForward());
        } else {
          wc.reload();
        }
        respond(200, { ok: true });
      } catch (err) {
        respond(500, { ok: false, error: String(err && err.message || err) });
      }
    } else if (isGet && route === "/tabs") {
      respond(200, { ok: true, tabs: paneState().tabs, active: activeTabId, closedTabs: closedTabs.length });
    } else if (isPost && route === "/tabs/new") {
      withBody((obj) => {
        if (obj.url !== undefined && obj.url !== null && obj.url !== "" && !policy.paneUrlAllowed(obj.url)) {
          respond(400, { ok: false, error: "url must be a real http(s) URL" });
          return;
        }
        const tab = openTab(obj.url || null, { background: obj.background === true });
        if (!tab) { respond(409, { ok: false, error: `at most ${MAX_TABS} tabs` }); return; }
        if (!paneVisible && obj.background !== true) showPane();
        respond(200, { ok: true, id: tab.id });
      });
    } else if (isPost && route === "/tabs/close") {
      withBody((obj) => {
        const id = obj.id === undefined ? activeTabId : Number(obj.id);
        if (!tabs.has(id)) { respond(404, { ok: false, error: "no such tab" }); return; }
        respond(200, { ok: closeTab(id) });
      });
    } else if (isPost && route === "/tabs/select") {
      withBody((obj) => {
        const id = Number(obj.id);
        if (!tabs.has(id)) { respond(404, { ok: false, error: "no such tab" }); return; }
        if (!paneVisible) showPane();
        respond(200, { ok: activateTab(id) });
      });
    } else if (isPost && route === "/tabs/reopen") {
      const tab = reopenClosedTab();
      if (!tab) { respond(404, { ok: false, error: "no closed tab to reopen" }); return; }
      if (!paneVisible) showPane();
      respond(200, { ok: true, id: tab.id });
    } else if (isGet && route === "/downloads") {
      respond(200, { ok: true, downloads: downloadView() });
    } else if (isPost && route === "/downloads/cancel") {
      // The bridge can stop a download. It cannot open or reveal one: those are the
      // console's explicit actions (a driven page must never launch a file).
      withBody((obj) => {
        downloadAction(String(obj.id || ""), "cancel").then((r) => respond(r.ok ? 200 : 409, r));
      });
    } else if (isGet && route === "/history") {
      respond(200, { ok: true, history: listHistory(params.get("q"), params.get("limit")) });
    } else if (isPost && route === "/history/add") {
      withBody((obj) => {
        const entry = addHistoryEntry(obj.url, obj.title);
        if (!entry) { respond(400, { ok: false, error: "url must be a real http(s) URL" }); return; }
        respond(200, { ok: true, entry });
      });
    } else if (isPost && route === "/history/remove") {
      withBody((obj) => {
        if (typeof obj.id !== "string" && typeof obj.url !== "string") { respond(400, { ok: false, error: "id or url is required" }); return; }
        respond(200, { ok: true, removed: removeHistory(typeof obj.id === "string" ? { id: obj.id } : { url: obj.url }) });
      });
    } else if (isPost && route === "/history/clear") {
      const removed = historyStore.get().length;
      historyStore.set([]);
      respond(200, { ok: true, removed });
    } else if (isGet && route === "/bookmarks") {
      respond(200, { ok: true, bookmarks: listBookmarks() });
    } else if (isPost && route === "/bookmarks/add") {
      withBody((obj) => {
        const r = addBookmarkEntry(obj.url, obj.title);
        if (!r.entry) { respond(400, { ok: false, error: "url must be a real http(s) URL (and the list is not full)" }); return; }
        pushPaneState();
        respond(200, { ok: true, added: r.added, bookmark: r.entry });
      });
    } else if (isPost && route === "/bookmarks/remove") {
      withBody((obj) => {
        if (typeof obj.id !== "string" && typeof obj.url !== "string") { respond(400, { ok: false, error: "id or url is required" }); return; }
        const removed = removeBookmarkEntry(typeof obj.id === "string" ? { id: obj.id } : { url: obj.url });
        pushPaneState();
        respond(200, { ok: true, removed });
      });
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
  lockToAppOrigin(win);
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
  // The Dock tile of a running shell shows the Dourmouse icon, the same one the
  // pinned Dourmouse.app carries (finding #159). Best effort: a missing icon
  // must never stop the app from starting.
  if (process.platform === "darwin" && app.dock) {
    try {
      app.dock.setIcon(path.join(__dirname, "resources", "icon.png"));
    } catch (exc) {
      log("dock icon not set (non-fatal):", exc.message || exc);
    }
  }
  try {
    await ensureServer();
  } catch (exc) {
    log("FATAL: could not bring up the server:", exc.message || exc);
    // A pinned app that just bounces and vanishes tells the owner nothing.
    dialog.showErrorBox("Dourmouse could not start", String((exc && exc.message) || exc));
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
  installPermissionPolicy(session.defaultSession);
  lockToAppOrigin(mainWindow);
  wireConsoleKeys(mainWindow);
  mainWindow.loadURL(`${BASE_URL}${START_PATH}`);
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
  lockToAppOrigin(mapWindow);
  mapWindow.loadURL(`${BASE_URL}/map`);

  const openAtlasAtLaunch = process.env.DOURMOUSE_OPEN_ATLAS_LAB === "1";
  atlasWindow = new BrowserWindow({
    width: 1100,
    height: 760,
    show: openAtlasAtLaunch,
    title: "ATLAS // STRATEGY LAB",
    webPreferences: { preload: PRELOAD, contextIsolation: true },
  });
  lockToAppOrigin(atlasWindow);
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
      lockToAppOrigin(mainWindow);
      wireConsoleKeys(mainWindow);
      mainWindow.loadURL(`${BASE_URL}${START_PATH}`);
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
  flushBrowserStores();
  stopVisionHelpers();
  // Only tear down a server THIS process spawned -- never kill a dev
  // server the user is reusing (REUSE_EXISTING_SERVER / already-running).
  if (!REUSE_EXISTING_SERVER) stopServer();
});
