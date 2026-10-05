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

const electronApi = require("electron");
const { app, BrowserWindow, BrowserView, ipcMain, shell, Tray, Menu, nativeImage, Notification, session, dialog, safeStorage, systemPreferences } = electronApi;
const { spawn, execFile } = require("child_process");
const crypto = require("crypto");
const http = require("http");
const fs = require("fs");
const path = require("path");
const policy = require("./policy");
const permLib = require("./permissions");
const pwLib = require("./passwords");
const profLib = require("./profiles");
const extLib = require("./extensions");
const impLib = require("./importers");
const drmLib = require("./drm");

// An isolated copy (a test run, a second profile) can keep every file the shell
// writes (window state, the browser profile, history, bookmarks, downloads list)
// out of the owner's real folder: DOURMOUSE_USER_DATA_DIR, absolute path only. Set
// before the app is ready, so it also wins over a launcher that picked a folder.
if (process.env.DOURMOUSE_USER_DATA_DIR && path.isAbsolute(process.env.DOURMOUSE_USER_DATA_DIR)) {
  app.setPath("userData", process.env.DOURMOUSE_USER_DATA_DIR);
}
// Phase B2: saved passwords are encrypted with Electron's safeStorage, whose key sits in the
// macOS Keychain under "<app name> Safe Storage". An isolated copy (a test run) can use its own
// name, so it never reads or creates the owner's real Keychain item. Set before the app is ready.
if (/^[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}$/.test(process.env.DOURMOUSE_ELECTRON_APP_NAME || "")) {
  app.setName(process.env.DOURMOUSE_ELECTRON_APP_NAME);
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
// Phase I2: a restarted server gets a fresh secret (see spawnServerProcess), so this is a let.
let OWNER_SECRET = require("crypto").randomBytes(32).toString("base64url");
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

async function waitForServer(url, timeoutMs = 60000, intervalMs = 300, shouldAbort = null) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (shouldAbort && shouldAbort()) return false;
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
  const proc = spawnServerProcess();
  const ok = await waitForServer(`${BASE_URL}/workspace`, 60000, 300, () => proc._dmExited === true);
  if (!ok) {
    if (proc._dmExited) {
      throw new Error(
        `The Dourmouse server stopped while starting (${describeExit(proc)}). What it said is in ${serverLogPath()}`
      );
    }
    throw new Error(`server did not answer at ${BASE_URL}/workspace within the startup deadline. Log: ${serverLogPath()}`);
  }
  proc._dmReady = true;
  await armOwnerCookie();
  serverSupervised = true;
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

// --------------------------------------------------------------------- //
// Server supervision and crash recovery (phase I2, finding #171)
//
// The server is a child process. If it exits while the app runs (and the app is
// not quitting), it is started again with a short, growing pause: at most
// SUPERVISOR.maxRestarts restarts inside SUPERVISOR.windowMs. Every start gets a
// FRESH owner secret on stdin (finding #162: never env, never a file) and the
// cookie is set again on the default session; the old secret died with the old
// process. A restart is told to the owner as one small toast in the console. When
// the last allowed restart also fails, ONE dialog names the log file. The server
// writes its own crash marker (webui.RunMarker) so the next start also raises one
// alert. What this cannot see: a server that is alive but stuck (it still answers
// nothing); only an exit is treated as a failure.
// --------------------------------------------------------------------- //

// Mutable on purpose: the tests shorten the pauses.
const SUPERVISOR = {
  maxRestarts: 3,
  windowMs: 2 * 60 * 1000,
  backoffMs: [1000, 3000, 8000],
  readyTimeoutMs: 60000,
  hangGraceMs: 10000,
  consoleWindowMs: 2 * 60 * 1000,
  noticeRetries: 15,
  noticeRetryMs: 1000,
};
const SERVER_LOG_MAX_BYTES = 2 * 1024 * 1024;
const RESTART_NOTICE_TITLE = "The server stopped and was restarted";

let serverSupervised = false; // true once a server this app started has answered
let serverQuitting = false;
let serverGaveUp = false;
let serverRestartTimes = [];
let serverRestartTimer = null;
let serverLogStream = null;
let alertStream = null;
let serverGaveUpDialogShown = false;

function serverLogPath() {
  return path.join(app.getPath("userData"), "logs", "server.log");
}

function openServerLog() {
  if (serverLogStream) return;
  try {
    const file = serverLogPath();
    fs.mkdirSync(path.dirname(file), { recursive: true });
    try {
      if (fs.statSync(file).size > SERVER_LOG_MAX_BYTES) fs.renameSync(file, `${file}.1`);
    } catch (_exc) {
      /* no log yet */
    }
    serverLogStream = fs.createWriteStream(file, { flags: "a", mode: 0o600 });
    serverLogStream.on("error", () => {
      serverLogStream = null;
    });
  } catch (_exc) {
    serverLogStream = null; // a log is a convenience: the server must still start
  }
}

function writeServerLog(text) {
  if (serverLogStream) serverLogStream.write(text);
}

// One line for the app's own events, to the console and to the same file the server writes to.
function supervisorLog(message) {
  log(message);
  openServerLog();
  writeServerLog(`[supervisor] ${new Date().toISOString()} ${message}\n`);
}

function describeExit(proc) {
  const info = proc._dmExit || {};
  if (info.error) return `could not be started: ${info.error}`;
  return info.signal ? `signal ${info.signal}` : `exit code ${info.code}`;
}

function spawnServerProcess() {
  // A fresh secret for every server this app starts (finding #162). It goes to the child on
  // stdin and nowhere else; a restart replaces it and the cookie is set again.
  OWNER_SECRET = crypto.randomBytes(32).toString("base64url");
  openServerLog();
  supervisorLog(`spawning ${VENV_PYTHON} -m dourmouse.webui (port ${PORT})`);
  const proc = spawn(VENV_PYTHON, ["-m", "dourmouse.webui"], {
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
      // Only a file name, for the alert the server raises after an unclean run.
      DOURMOUSE_SERVER_LOG: serverLogPath(),
      // The server's output goes to a pipe, where Python would hold it back in blocks: unbuffered,
      // so the log is complete when the process dies.
      PYTHONUNBUFFERED: "1",
    },
    stdio: ["pipe", "pipe", "pipe"],
  });
  serverProcess = proc;
  ownerGateArmed = true;
  try {
    fs.writeFileSync(serverPidFile(), String(proc.pid));
  } catch (_exc) {
    /* the pid file only helps the next launch clean up */
  }
  proc.stdin.on("error", () => {});
  proc.stdin.end(`${OWNER_SECRET}\n`);
  proc.stdout.on("data", (d) => {
    process.stdout.write(`[server] ${d}`);
    writeServerLog(d);
  });
  proc.stderr.on("data", (d) => {
    process.stderr.write(`[server:err] ${d}`);
    writeServerLog(d);
  });
  proc.on("error", (exc) => onServerExit(proc, null, null, exc)); // a spawn failure has no exit event
  proc.on("exit", (code, signal) => onServerExit(proc, code, signal, null));
  return proc;
}

// Finding #171: starts that never answered, counted since the last server that did, so a server
// that hangs at start (60 s each try) cannot slip past the time-window cap and restart forever.
let serverFailedStarts = 0;

function onServerExit(proc, code, signal, error) {
  if (proc._dmExited) return;
  proc._dmExited = true;
  if (proc._dmReady) serverFailedStarts = 0;
  else serverFailedStarts += 1;
  proc._dmExit = { code, signal, error: error ? String(error.message || error) : "" };
  supervisorLog(`server process exited (${describeExit(proc)})`);
  if (proc !== serverProcess || !serverSupervised || serverQuitting || proc._dmExpected) return;
  planServerRestart();
}

function planServerRestart() {
  if (serverRestartTimer || serverGaveUp || serverQuitting) return;
  const now = Date.now();
  serverRestartTimes = serverRestartTimes.filter((t) => now - t < SUPERVISOR.windowMs);
  if (serverRestartTimes.length >= SUPERVISOR.maxRestarts || serverFailedStarts > SUPERVISOR.maxRestarts) {
    giveUpOnServer();
    return;
  }
  serverRestartTimes.push(now);
  const pause = SUPERVISOR.backoffMs[Math.min(serverRestartTimes.length - 1, SUPERVISOR.backoffMs.length - 1)];
  supervisorLog(`restarting the server in ${pause} ms (try ${serverRestartTimes.length} of ${SUPERVISOR.maxRestarts})`);
  serverRestartTimer = setTimeout(() => {
    serverRestartTimer = null;
    restartServer().catch((exc) => supervisorLog(`restart failed: ${(exc && exc.message) || exc}`));
  }, pause);
}

async function restartServer() {
  if (serverQuitting) return;
  const proc = spawnServerProcess();
  // The new secret is already in the child; put the matching cookie in place before the
  // server answers, so the console's first request is not refused.
  await armOwnerCookie();
  const ok = await waitForServer(`${BASE_URL}/workspace`, SUPERVISOR.readyTimeoutMs, 300, () => proc._dmExited === true || serverQuitting);
  if (serverQuitting) return;
  if (proc._dmExited) return; // its exit already planned the next try, or gave up
  if (!ok) {
    // Alive but never answered: end it, which is counted like any other failure.
    supervisorLog("the restarted server did not answer in time; ending it");
    try {
      proc.kill("SIGKILL");
    } catch (_exc) {
      /* already gone */
    }
    return;
  }
  proc._dmReady = true;
  supervisorLog("the server is back");
  if (alertStream) startAlertNotifications(); // the old event stream died with the old server
  refreshAlerts();
  showConsoleNotice("warn", RESTART_NOTICE_TITLE, "Your window stays open. Details are in the notification centre.");
}

function giveUpOnServer() {
  serverGaveUp = true;
  supervisorLog(`the server stopped ${SUPERVISOR.maxRestarts + 1} times within ${SUPERVISOR.windowMs / 1000} seconds; not starting it again`);
  if (serverGaveUpDialogShown) return;
  serverGaveUpDialogShown = true;
  const parent = mainWindow && !mainWindow.isDestroyed() ? mainWindow : undefined;
  const options = {
    type: "error",
    title: "Dourmouse",
    message: "The Dourmouse server keeps stopping and was not started again.",
    detail:
      `It stopped ${SUPERVISOR.maxRestarts + 1} times in ${SUPERVISOR.windowMs / 60000} minutes, so automatic restarts are paused.\n\n` +
      `What it said before stopping is in this file:\n${serverLogPath()}`,
    buttons: ["Copy the log path", "Try again", "Quit Dourmouse"],
    defaultId: 1,
    cancelId: 2,
  };
  const pending = parent ? dialog.showMessageBox(parent, options) : dialog.showMessageBox(options);
  Promise.resolve(pending)
    .then((result) => {
      const choice = result && typeof result.response === "number" ? result.response : 2;
      if (choice === 0) {
        // Copied, not revealed: the shell keeps exactly one call that reveals a file in Finder (a
        // download's own action; test_browser_tabs_wiring pins that), and a path on the clipboard
        // pastes into Finder's "Go to Folder".
        electronApi.clipboard.writeText(serverLogPath());
      } else if (choice === 1) {
        serverGaveUp = false;
        serverGaveUpDialogShown = false;
        serverRestartTimes = [];
        serverFailedStarts = 0;
        planServerRestart();
      } else {
        app.quit();
      }
    })
    .catch((exc) => supervisorLog(`the stopped-server dialog failed: ${(exc && exc.message) || exc}`));
}

// A small toast in the console, through the shell's own toast stack (window.__dmShell.toasts).
// The text is fixed by this file; nothing the server or a page said goes into the script.
// A page that is not the shell yet (loading, sign-in) is retried for a few seconds.
function showConsoleNotice(level, title, detail, attempt = 0) {
  if (!mainWindow || mainWindow.isDestroyed() || mainWindow.webContents.isDestroyed()) return;
  const payload = JSON.stringify({ level, title, detail, ttl: 12000 });
  const code = `(() => { const s = window.__dmShell; if (!s || !s.toasts) return false; s.toasts.show(${payload}); return true; })()`;
  Promise.resolve(mainWindow.webContents.executeJavaScript(code))
    .then((shown) => {
      if (!shown && attempt < SUPERVISOR.noticeRetries) {
        setTimeout(() => showConsoleNotice(level, title, detail, attempt + 1), SUPERVISOR.noticeRetryMs);
      }
    })
    .catch(() => {
      if (attempt < SUPERVISOR.noticeRetries) {
        setTimeout(() => showConsoleNotice(level, title, detail, attempt + 1), SUPERVISOR.noticeRetryMs);
      }
    });
}

function stopServer() {
  serverQuitting = true;
  if (serverRestartTimer) {
    clearTimeout(serverRestartTimer);
    serverRestartTimer = null;
  }
  if (serverProcess && !serverProcess.killed) {
    log("stopping spawned server process");
    serverProcess._dmExpected = true;
    try {
      serverProcess.kill();
    } catch {
      /* best-effort teardown, matches dourmouse/desktop.py's own finally block */
    }
  }
}

// The console window's own renderer. A crash or a hang gets one reload; a second one inside
// the window is not retried in a loop but put in front of the owner once.
const consoleRecoveries = policy.createLimiter(1, SUPERVISOR.consoleWindowMs);
let consoleRecovering = false;
let consoleGaveUpShown = false;
let consoleForcedCrash = false; // the render-process-gone that our own forcefullyCrashRenderer causes

function recoverConsole(win, reason) {
  if (consoleRecovering || serverQuitting || !win || win.isDestroyed()) return;
  const wc = win.webContents;
  if (!consoleRecoveries.allow()) {
    supervisorLog(`the console window ${reason} again within ${SUPERVISOR.consoleWindowMs / 1000} seconds; not reloading it again`);
    if (!consoleGaveUpShown) {
      consoleGaveUpShown = true;
      Promise.resolve(
        dialog.showMessageBox(win, {
          type: "error",
          title: "Dourmouse",
          message: "The Dourmouse window stopped working twice in a row.",
          detail: `It was reloaded once already. What happened is in this file:\n${serverLogPath()}`,
          buttons: ["Reload", "Quit Dourmouse"],
          defaultId: 0,
          cancelId: 1,
        })
      )
        .then((result) => {
          consoleGaveUpShown = false;
          if (result && result.response === 0 && !win.isDestroyed()) win.webContents.reload();
          else if (result && result.response === 1) app.quit();
        })
        .catch(() => {
          consoleGaveUpShown = false;
        });
    }
    return;
  }
  consoleRecovering = true;
  supervisorLog(`the console window ${reason}; reloading it once`);
  const done = () => {
    consoleRecovering = false;
  };
  setTimeout(() => {
    if (win.isDestroyed() || wc.isDestroyed()) return done();
    const current = wc.getURL();
    wc.once("did-finish-load", done);
    setTimeout(done, 8000);
    if (current && policy.navigationAllowed(current, PORT)) wc.reload();
    else wc.loadURL(`${BASE_URL}${START_PATH}`);
  }, 250);
}

function wireConsoleRecovery(win) {
  const wc = win.webContents;
  let hangTimer = null;
  const clearHang = () => {
    if (hangTimer) clearTimeout(hangTimer);
    hangTimer = null;
  };
  wc.on("render-process-gone", (_evt, details) => {
    supervisorLog(`console renderer gone: ${JSON.stringify(details || {})}`);
    clearHang();
    if (!policy.isCrashReason((details && details.reason) || "")) return;
    if (consoleForcedCrash) {
      consoleForcedCrash = false; // the hang path below already started the one reload
      return;
    }
    // Finding #171: a crash while the one reload is still loading is a failed recovery; it must
    // reach the give-up dialog, not be swallowed by the "already recovering" guard.
    if (consoleRecovering) consoleRecovering = false;
    recoverConsole(win, "crashed");
  });
  win.on("unresponsive", () => {
    supervisorLog("the console window is not responding");
    clearHang();
    hangTimer = setTimeout(() => {
      hangTimer = null;
      if (win.isDestroyed() || wc.isDestroyed()) return;
      supervisorLog("the console window is still not responding; ending its process and reloading");
      try {
        consoleForcedCrash = true;
        wc.forcefullyCrashRenderer();
      } catch (_exc) {
        /* the reload below is still tried */
      }
      recoverConsole(win, "stopped responding");
    }, SUPERVISOR.hangGraceMs);
  });
  win.on("responsive", () => {
    if (hangTimer) supervisorLog("the console window is responding again");
    clearHang();
  });
  win.on("closed", clearHang);
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
const PANE_PARTITION = "persist:dourmouse-browser"; // the default profile's; profiles.js carries the same string
// Every tab is created with exactly these preferences: no preload (a web page
// never gets the console's bridge), context isolation on, the profile's partition.
const TAB_WEB_PREFERENCES = { contextIsolation: true, partition: PANE_PARTITION };
// Phase B3: the partition of the ACTIVE profile (the one above for the default profile, that name
// plus a dash and the profile's name for a named one). The object above is the single source of
// every tab's preferences; a profile switch changes its `partition` and nothing else.
function activePartition() {
  return profLib.partitionFor(activeProfileName);
}
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

// The browser folder (and a profile's folder inside it) is owner-only, and so is a file that holds
// ciphertext or the owner's site decisions. A failure to tighten is logged, never swallowed silently.
function lockDown(folder, file, fileMode) {
  try {
    fs.chmodSync(folder, 0o700);
    if (file && fileMode) fs.chmodSync(file, fileMode);
  } catch (exc) {
    log("browser store permissions could not be tightened:", exc.message || exc);
  }
}

// Options: `mode` writes the file with those permissions from the start (the files that hold
// ciphertext are 0600), and `lazy` writes it only after set() was called, so reading a store
// that nobody has changed never creates a file.
// Phase B3: `dir` says which folder the file lives in (a profile's own folder), and every write
// leaves the folder 0700 and a file that holds secrets or decisions 0600, even when the folder or
// the file was created earlier with looser permissions.
function makeStore(file, fallback, { mode: fileMode = 0, lazy = false, dir = browserDataDir } = {}) {
  let data = null;
  let timer = null;
  let changed = false;
  const target = () => path.join(dir(), file);
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
    if (data === null || (lazy && !changed)) return;
    try {
      fs.mkdirSync(dir(), { recursive: true, mode: 0o700 });
      const tmp = `${target()}.tmp`;
      // A file that holds secrets (the password list) is written owner-only from the start.
      fs.writeFileSync(tmp, JSON.stringify(data), fileMode ? { mode: fileMode } : undefined);
      fs.renameSync(tmp, target());
      lockDown(dir(), fileMode ? target() : "", fileMode);
    } catch (exc) {
      log(`browser store ${file} could not be saved:`, exc.message || exc);
    }
  }
  function set(value) {
    data = value;
    changed = true;
    if (!timer) timer = setTimeout(flush, 500);
  }
  return { get, set, flush };
}

// Phase B3: profiles. The list of profiles and which one is active live in profiles.json. The
// "default" profile keeps today's folder (browser/) and today's partition, so nothing that exists
// moves; a named profile keeps its files in browser/profiles/<name>/ and has its own partition.
const registryStore = makeStore("profiles.json", { active: "default", names: [] }, { mode: 0o600, lazy: true });
let profileRegistry = profLib.sanitizeRegistry(registryStore.get());
let activeProfileName = profileRegistry.active;
TAB_WEB_PREFERENCES.partition = activePartition(); // the pane opens in the profile that was in use when the app last ran
function saveProfileRegistry(next) {
  profileRegistry = next;
  registryStore.set({ active: next.active, names: next.names });
}

// History, bookmarks, zoom, site decisions, encrypted logins and encrypted addresses belong to the
// profile (the last three hold only ciphertext, written 0600). The downloads list is the browser's
// own and is shared: every download lands in ~/Downloads whichever profile fetched it.
const profileStores = new Map(); // profile name -> its stores, made on first use
function storesFor(name) {
  let set = profileStores.get(name);
  if (!set) {
    const dir = () => profLib.dirFor(path, browserDataDir(), name);
    set = {
      history: makeStore("history.json", [], { dir }),
      bookmarks: makeStore("bookmarks.json", [], { dir }),
      zoom: makeStore("zoom.json", {}, { dir }),
      permission: makeStore("permissions.json", { sites: {} }, { mode: 0o600, lazy: true, dir }),
      password: makeStore("passwords.json", { entries: [], never: [] }, { mode: 0o600, lazy: true, dir }),
      address: makeStore("addresses.json", { profiles: [] }, { mode: 0o600, lazy: true, dir }),
    };
    profileStores.set(name, set);
  }
  return set;
}
// Every use below goes through the ACTIVE profile at the moment of the call, so switching profile
// needs no change to any call site.
const profileStore = (key) => ({
  get: () => storesFor(activeProfileName)[key].get(),
  set: (v) => storesFor(activeProfileName)[key].set(v),
  flush: () => storesFor(activeProfileName)[key].flush(),
});
const historyStore = profileStore("history");
const bookmarkStore = profileStore("bookmarks");
const zoomStore = profileStore("zoom");
const permissionStore = profileStore("permission");
const passwordStore = profileStore("password");
const addressStore = profileStore("address");
const downloadStore = makeStore("downloads.json", []);

function flushBrowserStores() {
  for (const set of profileStores.values()) for (const s of Object.values(set)) s.flush();
  downloadStore.flush();
  registryStore.flush();
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

// ------------------- site permissions, passwords, autofill (B2) ------------------- //
// Finding S34 denied every permission to every page in the pane. Phase B2 replaces that with
// what Chrome does: ask the owner once per site, remember the answer, let the owner see and
// undo it. The rules that keep it safe:
//   * A grant is made ONLY by the console window, through the IPC handlers further down, which
//     check the sender. No pane bridge route (the HTTP door the agent and the server use) can
//     grant, and no page can: a page has no IPC at all.
//   * Only camera, microphone, geolocation, notifications, clipboard-read and fullscreen can
//     ever be granted. Screen capture, MIDI, USB, serial, HID and openExternal are refused.
//   * A prompt exists only while the BROWSER screen is the one showing, so there is somebody
//     to answer it. Otherwise the request is refused on the spot and nothing is stored.
//   * Camera and microphone also obey the privacy kill switch and macOS's own permission.
//   * Passwords and addresses are ciphertext at rest (safeStorage). They are filled only after
//     a click by the owner, only into the top page, only at the origin they were saved for.

// Asking Electron whether encryption is available is NOT free: on macOS the first call creates
// (or opens) this app's Keychain item. So it is never done at start-up or on a state push, only
// when a password or an address is actually about to be saved or opened, or when the owner opens
// the Passwords panel. `known()` reports what is already known without asking.
let encryptionAvailable = null;
const cipher = {
  available() {
    if (encryptionAvailable !== null) return encryptionAvailable;
    try {
      encryptionAvailable = Boolean(safeStorage.isEncryptionAvailable());
      // Linux can fall back to a hard-coded key ("basic_text"); that is not encryption.
      if (encryptionAvailable && process.platform === "linux" && typeof safeStorage.getSelectedStorageBackend === "function") {
        encryptionAvailable = safeStorage.getSelectedStorageBackend() !== "basic_text";
      }
    } catch {
      encryptionAvailable = false;
    }
    return encryptionAvailable;
  },
  known: () => encryptionAvailable,
  encrypt: (text) => safeStorage.encryptString(String(text)).toString("base64"),
  decrypt: (b64) => safeStorage.decryptString(Buffer.from(String(b64), "base64")),
};
const vault = pwLib.createVault({ store: passwordStore, crypto: cipher, newId });
const addressBook = pwLib.createAddressBook({ store: addressStore, crypto: cipher, newId });

const promptQueue = permLib.createPromptQueue();
const tempGrants = permLib.createTempGrants();
const pendingSaves = new Map(); // tab id -> a login waiting for Save, Never or Not now (the password stays here, in the main process)
const SAVE_TTL_MS = 120000;
const fillCache = new Map(); // origin -> { ver, entries }
let permNotice = "";
let permNoticeAt = 0;

let sitesChecked = false;
function siteTable() {
  const d = permissionStore.get();
  if (!sitesChecked) {
    sitesChecked = true;
    d.sites = permLib.sanitizeSites(d.sites);
  }
  return d.sites;
}
function storeSites(next) {
  permissionStore.set({ sites: next });
}

const hostOfOrigin = (origin) => {
  try {
    return new URL(origin).host;
  } catch {
    return String(origin);
  }
};

function setNotice(text) {
  permNotice = String(text || "");
  permNoticeAt = Date.now();
  schedulePush();
  setTimeout(schedulePush, 21000).unref(); // the bar drops the notice after twenty seconds
}

// The privacy kill switch (the same flag the tray toggles, read through the same route the tray
// uses). Fail closed: if it cannot be read, the camera and the microphone stay off.
const visionGate = { ok: false, mic: false, camera: false, at: 0 };
function withTimeout(promise, ms) {
  return Promise.race([promise, new Promise((_resolve, reject) => setTimeout(() => reject(new Error("timed out")), ms))]);
}
async function freshVisionGate() {
  try {
    const status = await withTimeout(fetchJson(`${BASE_URL}/api/vision/status`, { method: "GET" }), 1500);
    const ks = status && status.kill_switch;
    if (ks && typeof ks === "object") {
      visionGate.ok = true;
      visionGate.mic = ks.mic_enabled !== false;
      visionGate.camera = ks.camera_enabled !== false;
    } else {
      visionGate.ok = false;
    }
  } catch {
    visionGate.ok = false;
  }
  visionGate.at = Date.now();
  return visionGate;
}

const TCC_NAME = { microphone: "microphone", camera: "camera" };
// Whether the camera and microphone may be used right now: the kill switch first, then macOS.
async function mediaGate(keys) {
  const wanted = keys.filter((k) => k === "microphone" || k === "camera");
  if (!wanted.length) return { ok: true };
  const gate = await freshVisionGate();
  if (!gate.ok) return { ok: false, reason: "The privacy switch could not be read, so the camera and microphone stay off." };
  for (const k of wanted) {
    if (k === "microphone" && !gate.mic) return { ok: false, reason: "The microphone is switched off by the privacy kill switch." };
    if (k === "camera" && !gate.camera) return { ok: false, reason: "The camera is switched off by the privacy kill switch." };
  }
  if (process.platform === "darwin" && systemPreferences && typeof systemPreferences.getMediaAccessStatus === "function") {
    for (const k of wanted) {
      const status = systemPreferences.getMediaAccessStatus(TCC_NAME[k]);
      if (status === "granted") continue;
      if (status === "not-determined" && typeof systemPreferences.askForMediaAccess === "function") {
        let granted = false;
        try {
          granted = await systemPreferences.askForMediaAccess(TCC_NAME[k]);
        } catch {
          granted = false;
        }
        if (granted) continue;
      }
      return { ok: false, reason: `macOS has not allowed the ${k} for this app. Turn it on in System Settings, Privacy and Security.` };
    }
  }
  return { ok: true };
}
// The synchronous twin, for permission CHECKS (they cannot wait). It uses the last answer the
// kill switch gave and refreshes it in the background; before the first answer it says no.
function mediaCheckOk(keys) {
  const wanted = keys.filter((k) => k === "microphone" || k === "camera");
  if (!wanted.length) return true;
  if (Date.now() - visionGate.at > 5000) freshVisionGate();
  if (!visionGate.ok) return false;
  for (const k of wanted) {
    if (k === "microphone" && !visionGate.mic) return false;
    if (k === "camera" && !visionGate.camera) return false;
  }
  if (process.platform === "darwin" && systemPreferences && typeof systemPreferences.getMediaAccessStatus === "function") {
    for (const k of wanted) if (systemPreferences.getMediaAccessStatus(TCC_NAME[k]) !== "granted") return false;
  }
  return true;
}

// Who is asking, and is it the page the owner is looking at? A frame from another origin never
// borrows the top page's decisions.
function askingOrigin(wc, requestingUrl) {
  const top = permLib.originOf(wc.getURL());
  const asked = permLib.originOf(requestingUrl || wc.getURL());
  return top && asked && top === asked ? asked : "";
}

function handlePaneRequest(tab, wc, permission, callback, details) {
  const origin = askingOrigin(wc, details && details.requestingUrl);
  if (!origin) return callback(false);
  // B3: the key system for protected video. Allowed only for the page the owner is on, and only
  // when the Widevine component is really there; otherwise (stock Electron) it stays refused.
  if (permission === "mediaKeySystem") return callback(drmState.ready === true);
  const cls = permLib.classifyRequest(permission, details);
  if (cls.action === "allow") return callback(true);
  if (cls.action === "deny") return callback(false);
  const sites = siteTable();
  const unknown = [];
  for (const key of cls.keys) {
    const d = permLib.getDecision(sites, origin, key);
    if (d === "block") return callback(false);
    if (d !== "allow" && !tempGrants.has(tab.id, origin, key)) unknown.push(key);
  }
  const grant = (keys) => {
    mediaGate(keys).then((g) => {
      if (!g.ok) setNotice(g.reason);
      callback(g.ok);
    });
  };
  if (!unknown.length) return grant(cls.keys);
  // Nobody to ask: refuse now and store nothing.
  if (!browserScreenActive || !mainWindow || mainWindow.isDestroyed()) return callback(false);
  const queued = promptQueue.request(tab.id, origin, unknown, (decision) => {
    if (decision === "allow") {
      let next = siteTable();
      for (const key of unknown) {
        const r = permLib.setDecision(next, origin, key, "allow");
        if (r.ok) next = r.sites;
      }
      storeSites(next);
      grant(cls.keys);
    } else if (decision === "once") {
      tempGrants.add(tab.id, origin, unknown);
      grant(cls.keys);
    } else if (decision === "block") {
      let next = siteTable();
      for (const key of unknown) {
        const r = permLib.setDecision(next, origin, key, "block");
        if (r.ok) next = r.sites;
      }
      storeSites(next);
      callback(false);
    } else {
      callback(false); // dismissed, expired or the tab went away: no, and nothing is remembered
    }
    schedulePush();
  });
  if (!queued.ok) return callback(false);
  schedulePush();
}

function handlePaneCheck(tab, wc, permission, requestingOrigin, details) {
  const origin = askingOrigin(wc, requestingOrigin || undefined);
  if (!origin) return false;
  if (permission === "mediaKeySystem") return drmState.ready === true;
  const cls = permLib.classifyCheck(permission, details);
  if (cls.action === "allow") return true;
  if (cls.action === "deny") return false;
  const sites = siteTable();
  for (const key of cls.keys) {
    if (permLib.getDecision(sites, origin, key) !== "allow" && !tempGrants.has(tab.id, origin, key)) return false;
  }
  return mediaCheckOk(cls.keys);
}

const permissionPolicyInstalled = new WeakSet();
const paneSessions = new WeakSet();
function installPermissionPolicy(ses) {
  if (permissionPolicyInstalled.has(ses)) return;
  permissionPolicyInstalled.add(ses);
  ses.setPermissionRequestHandler((wc, permission, callback, details) => {
    const tab = tabForContents(wc);
    if (tab) return handlePaneRequest(tab, wc, permission, callback, details);
    if (paneSessions.has(ses)) return callback(false); // anything else in the pane's session: no
    const origin = (details && details.requestingUrl) || (wc && wc.getURL()) || "";
    callback(policy.permissionAllowed(permission, origin, PORT));
  });
  ses.setPermissionCheckHandler((wc, permission, requestingOrigin, details) => {
    const tab = tabForContents(wc);
    if (tab) return handlePaneCheck(tab, wc, permission, requestingOrigin, details);
    if (paneSessions.has(ses)) return false;
    return policy.permissionAllowed(permission, requestingOrigin, PORT);
  });
}

// Everything the pane's session needs before its first page loads: the permission policy, the
// refusal of screen capture and device pickers, and the form helper script.
const FORM_HELPER = path.join(__dirname, "content", "frame-forms.js");
function installPaneSession(ses) {
  if (!ses || paneSessions.has(ses)) return;
  paneSessions.add(ses); // the handlers read this at call time: a pane session never falls back to the app's own rules
  installPermissionPolicy(ses);
  try {
    if (typeof ses.setDisplayMediaRequestHandler === "function") ses.setDisplayMediaRequestHandler((_request, callback) => callback({}));
    if (typeof ses.setDevicePermissionHandler === "function") ses.setDevicePermissionHandler(() => false);
  } catch (exc) {
    log("pane session: could not install the screen-capture and device refusals:", exc.message || exc);
  }
  try {
    if (typeof ses.registerPreloadScript === "function") ses.registerPreloadScript({ type: "frame", filePath: FORM_HELPER });
  } catch (exc) {
    log("pane session: the form helper script could not be registered, so saving and filling passwords is off:", exc.message || exc);
  }
}

// ------------------------------ logins and fills ------------------------------ //

function fillEntriesFor(origin) {
  const ver = vault.version();
  const hit = fillCache.get(origin);
  if (hit && hit.ver === ver) return hit.entries;
  const entries = vault.entriesFor(origin);
  fillCache.set(origin, { ver, entries });
  if (fillCache.size > 50) fillCache.delete(fillCache.keys().next().value);
  return entries;
}

// A message from the form helper is believed only if it came from the top page of a pane tab.
// The origin is read from the frame's real address, never from the message.
function trustedTabMessage(evt) {
  const tab = tabForContents(evt.sender);
  const frame = evt.senderFrame;
  if (!tab || !frame || frame.parent !== null) return null;
  const origin = permLib.originOf(frame.url);
  if (!origin || origin !== permLib.originOf(evt.sender.getURL())) return null;
  return { tab, origin };
}

ipcMain.on("dm:forms", (evt, msg) => {
  const m = trustedTabMessage(evt);
  if (!m || !msg || typeof msg !== "object") return;
  m.tab.forms = { origin: m.origin, pw: msg.pw === true, user: msg.user === true, addr: msg.addr === true };
  schedulePush();
});

ipcMain.on("dm:pw-submit", (evt, msg) => {
  const m = trustedTabMessage(evt);
  if (!m || !msg || typeof msg.username !== "string" || typeof msg.password !== "string") return;
  if (msg.username.length > pwLib.MAX_USERNAME || msg.password.length < 1 || msg.password.length > pwLib.MAX_PASSWORD) return;
  if (!m.tab.loginLimit) m.tab.loginLimit = policy.createLimiter(10, 60000);
  if (!m.tab.loginLimit.allow()) return;
  const username = pwLib.cleanUsername(msg.username);
  const verdict = vault.classify({ origin: m.origin, username, password: msg.password });
  if (verdict.status === "unavailable") {
    setNotice("Passwords cannot be saved: this Mac's secure storage is not available.");
    return;
  }
  if (verdict.status !== "new" && verdict.status !== "update") return; // same, never or refused: say nothing
  pendingSaves.set(m.tab.id, { id: newId(), origin: m.origin, username, password: msg.password, existingId: verdict.id || "", at: Date.now() });
  schedulePush();
});

function fillLogin(tab, entryId) {
  const wc = liveContents(tab);
  if (!wc || tab !== activeTab()) return { ok: false, error: "that page is not the one in front" };
  const origin = permLib.originOf(wc.getURL());
  if (!pwLib.savableOrigin(origin)) return { ok: false, error: "logins are not filled on this kind of page" };
  const c = vault.credentials(String(entryId));
  if (!c) return { ok: false, error: "that login cannot be read" };
  if (c.origin !== origin) return { ok: false, error: "that login belongs to another site" };
  vault.touch(String(entryId));
  wc.send("dm:fill-login", { origin, username: c.username, password: c.password });
  return { ok: true };
}

function fillAddress(tab, profileId) {
  const wc = liveContents(tab);
  if (!wc || tab !== activeTab()) return { ok: false, error: "that page is not the one in front" };
  const origin = permLib.originOf(wc.getURL());
  if (!origin) return { ok: false, error: "addresses are filled only on web pages" };
  const fields = addressBook.get(String(profileId));
  if (!fields) return { ok: false, error: "that address cannot be read" };
  wc.send("dm:fill-address", { origin, fields });
  return { ok: true };
}

// A click on a login or address field in the page: a native menu at the pointer, so the owner
// picks what to fill. Nothing is filled until an item is chosen.
ipcMain.on("dm:field-click", (evt, msg) => {
  const m = trustedTabMessage(evt);
  if (!m || !msg || !paneVisible || m.tab !== activeTab() || !mainWindow || mainWindow.isDestroyed()) return;
  let items = [];
  let head = "";
  if (msg.kind === "password" || msg.kind === "username") {
    head = `Saved logins for ${hostOfOrigin(m.origin)}`;
    items = fillEntriesFor(m.origin).map((e) => ({ label: e.username || "(no username)", click: () => fillLogin(m.tab, e.id) }));
  } else if (msg.kind === "address") {
    head = "Saved addresses";
    items = addressBook.list().filter((p) => p.readable).map((p) => ({ label: p.label || "Address", click: () => fillAddress(m.tab, p.id) }));
  }
  if (!items.length) return;
  Menu.buildFromTemplate([{ label: head, enabled: false }, { type: "separator" }, ...items]).popup({ window: mainWindow });
});

// What the BROWSER screen needs to draw its bars. Sent to the console window only, over IPC.
// It never holds a password, and no bridge route returns it.
function privacyState() {
  const tab = activeTab();
  const out = {
    perm: null, pendingPerms: promptQueue.size(), save: null, fill: null, fillAddress: null,
    notice: Date.now() - permNoticeAt < 20000 ? permNotice : "",
    vault: { available: cipher.known(), ...vault.counts() }, addresses: addressBook.count(), sites: permLib.countSites(siteTable()),
  };
  if (!tab) return out;
  const p = promptQueue.forTab(tab.id);
  if (p) out.perm = { id: p.id, origin: p.origin, host: hostOfOrigin(p.origin), keys: p.keys, text: permLib.promptSentence(p.origin, p.keys) };
  const s = pendingSaves.get(tab.id);
  if (s) out.save = { id: s.id, origin: s.origin, host: hostOfOrigin(s.origin), username: s.username, update: Boolean(s.existingId) };
  const wc = liveContents(tab);
  const origin = wc ? permLib.originOf(wc.getURL()) : "";
  if (origin && tab.forms && tab.forms.origin === origin) {
    if (tab.forms.pw || tab.forms.user) {
      const entries = fillEntriesFor(origin);
      if (entries.length) out.fill = { origin, host: hostOfOrigin(origin), entries };
    }
    if (tab.forms.addr && addressBook.count()) {
      out.fillAddress = { profiles: addressBook.list().filter((x) => x.readable).map((x) => ({ id: x.id, label: x.label })) };
    }
  }
  return out;
}

// What the bridge may say: counts, never a name, a site or a value.
function privacyCounts() {
  return {
    permissions: { pendingPrompts: promptQueue.size(), ...permLib.countSites(siteTable()) },
    passwords: { ...vault.counts(), encryption: cipher.known() },
    addresses: { count: addressBook.count() },
  };
}

// A tab went away or moved to another origin: what was waiting or granted for the old page ends.
function endPageGrants(tab, newUrl) {
  const origin = permLib.originOf(newUrl);
  tempGrants.dropForeign(tab.id, origin);
  promptQueue.dropForeign(tab.id, origin);
  tab.forms = null;
}
function forgetTabPrivacy(tabId) {
  tempGrants.dropTab(tabId);
  promptQueue.dropTab(tabId);
  const s = pendingSaves.get(tabId);
  if (s) s.password = "";
  pendingSaves.delete(tabId);
}
setInterval(() => {
  let changed = promptQueue.expire() > 0;
  const t = Date.now();
  for (const [id, s] of pendingSaves) {
    if (t - s.at >= SAVE_TTL_MS) {
      s.password = "";
      pendingSaves.delete(id);
      changed = true;
    }
  }
  if (changed) schedulePush();
}, 10000).unref();

// The first tab is the one dourmouse/browser_agent.py attaches to: it finds "the"
// pane by looking for the one still-blank about:blank page at attach time, so the
// first tab is created at about:blank and nothing else is ever created there
// while other tabs exist (a new tab shows NEW_TAB_URL instead).
function ensurePaneView() {
  if (paneView && !paneView.webContents.isDestroyed()) return paneView;
  // The session is prepared BEFORE the first page exists, so the form helper script is there
  // from the very first document.
  if (typeof session.fromPartition === "function") installPaneSession(session.fromPartition(TAB_WEB_PREFERENCES.partition));
  paneView = new BrowserView({ webPreferences: TAB_WEB_PREFERENCES });
  paneView.webContents.session.setUserAgent(chromeUserAgent());
  installPermissionPolicy(paneView.webContents.session); // finding S34: the pane's session carries the policy from its first page
  installPaneSession(paneView.webContents.session);
  installDownloadHandler(paneView.webContents.session);
  startExtensions(paneView.webContents.session); // B3: the owner's approved extensions, once per session
  const tab = registerTab(paneView, "about:blank");
  activeTabId = tab.id;
  return paneView;
}

function loadInTab(wc, url) {
  const owner = tabForContents(wc);
  if (owner && policy.paneUrlAllowed(url)) owner.requested = url; // what the tab was last asked to open, for a crash recovery
  // did-fail-load reports a failed load to the console; the promise rejection
  // carries nothing more, so it is consumed here rather than left unhandled.
  const go = () => wc.loadURL(url).catch((exc) => log("tab load did not finish:", exc && exc.message ? exc.message : exc));
  // B3: while a session is still loading its extensions, a real page waits for them (a few
  // hundred milliseconds), so a content script is in place for the first page. The blank anchor
  // page the browser agent looks for never waits.
  const pending = url !== "about:blank" && wc.session ? extSessionInit.get(wc.session) : null;
  if (pending) pending.then(() => { if (!wc.isDestroyed()) go(); });
  else go();
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
    endPageGrants(tab, wc.getURL());
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
    // Phase C2: a key press that raises before-input-event is the owner's own (the browser agent's
    // CDP key events never raise it, measured on Electron 44.3), so the owner takes the tab.
    if (input.type === "keyDown") noteOwnerInput(tab, "key");
    if (input.type === "keyDown" && handlePaneShortcut(tab, input)) event.preventDefault();
  });
  // Phase C2: a click, scroll or touch is the owner's unless the agent declared it is clicking.
  wc.on("input-event", (_evt, input) => onPaneInputEvent(tab, input));
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
  forgetTabPrivacy(tab.id);
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

// Phase C1: the CDP target id of a tab and its webContents id, for the bridge's /tabs and
// /status only (not for the console's pane:state push). browser_agent.py maps the active tab
// to its Playwright Page by this target id, so it follows the tab the owner is looking at
// without guessing from the address. The id is read from the tab itself with a debugger
// session that is attached for the one command and detached again (never left attached, and
// never attached to a tab that is gone). Cached per tab; `refresh` re-reads it. Calls are
// serialised so two requests cannot detach each other's session.
let targetIdQueue = Promise.resolve();
function tabTargetId(tab, refresh = false) {
  const run = async () => {
    const wc = liveContents(tab);
    if (!wc) return "";
    if (tab.targetId && !refresh) return tab.targetId;
    const dbg = wc.debugger;
    if (!dbg || typeof dbg.attach !== "function") return tab.targetId || "";
    const already = typeof dbg.isAttached === "function" && dbg.isAttached();
    try {
      if (!already) dbg.attach("1.3");
      const r = await withTimeout(dbg.sendCommand("Target.getTargetInfo"), 2000);
      tab.targetId = String((r && r.targetInfo && r.targetInfo.targetId) || "");
    } catch (exc) {
      log("tab target id unavailable:", String((exc && exc.message) || exc));
    } finally {
      if (!already) {
        try { dbg.detach(); } catch (exc) { log("debugger detach failed:", String((exc && exc.message) || exc)); }
      }
    }
    return tab.targetId || "";
  };
  const next = targetIdQueue.then(run, run);
  targetIdQueue = next.then(() => undefined, () => undefined);
  return next;
}

async function bridgeTabsView(refresh) {
  const state = paneState();
  const list = await Promise.all(state.tabs.map(async (info) => {
    const tab = tabs.get(info.id);
    const wc = liveContents(tab);
    return { ...info, wcId: wc ? wc.id : 0, targetId: tab ? await tabTargetId(tab, refresh) : "" };
  }));
  return { ok: true, tabs: list, active: activeTabId, closedTabs: closedTabs.length };
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
    profile: activeProfileName,
  };
}

function pushPaneStateNow() {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send("pane:state", paneState());
    // The privacy bars (permission prompt, Save password, Fill) ride the same push but go
    // out on their own channel: the pane's own state is also what the bridge reads.
    mainWindow.webContents.send("pane:privacy", privacyState());
  }
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

// ------------------------- shared control (phase C2) ------------------------- //
// The owner and the browser agent share the pane, and the owner's real input always wins. Design:
// ~/Documents/DOURMOUSE/C2_SHARED_CONTROL_DESIGN.md. Measured on Electron 44.3 before relying on
// it: a CDP key event (the agent's) never raises before-input-event and an OS key press does; a
// CDP click and a real click look the same in input-event, so the agent declares a short pointer
// window around each click it sends; the agent's text goes in through /control/type
// (webContents.insertText), which raises no input event at all. Whatever the agent did not
// declare counts as the owner's, so a mistake makes the model back off, never push through.

const CONTROL_OWNER_HOLD_MS = 2500; // the owner touched the tab this recently: no agent action starts there
const CONTROL_LEASE_MS = 60000; // an action not heard from for this long is over (the agent died)
const CONTROL_POINTER_MAX_MS = 3000; // a pointer window the agent never closed closes itself
const CONTROL_POINTER_GRACE_MS = 150;
const CONTROL_POINTER_SLOP_PX = 12;
const CONTROL_TYPE_MAX = 512; // characters per /control/type call
const OWNER_POINTER_EVENTS = new Set(["mouseDown", "mouseWheel", "touchStart", "gestureTapDown", "gesturePinchBegin"]);
const CONTROL_NAVIGATING_TOOLS = new Set(["open", "back", "submit", "signin"]);
const CONTROL_TOOL_RE = /^[a-z_]{1,24}$/;
const CONTROL_OUTCOMES = new Set(["done", "error", "owner-input", "stopped", "owner-control", "no-tab", "expired", "focus-moved"]);
const control = {
  held: false, // the owner pressed Take control: nothing starts until Let the model act
  lastOwner: new Map(), // tab id -> when the owner last pressed a key, clicked, scrolled or touched there
  actions: new Map(), // action id -> the agent's claim on one tab
  waiting: null, // { tabId, tool, until } while the agent waits for the owner to pause
  last: null, // { tool, outcome, at }: the last action that ended, for the console's note
};
let controlPushTimer = null;
let controlExpiryTimer = null;

function controlSweep(now) {
  for (const [id, a] of control.actions) {
    if (now - a.seen > CONTROL_LEASE_MS) {
      control.actions.delete(id);
      control.last = { tool: a.tool, outcome: "expired", at: now };
    }
  }
  for (const [id, t] of control.lastOwner) {
    if (now - t >= CONTROL_OWNER_HOLD_MS || !tabs.has(id)) control.lastOwner.delete(id);
  }
  if (control.waiting && control.waiting.until <= now) control.waiting = null;
}

function controlState() {
  controlSweep(Date.now());
  if (control.held) return "owner-control";
  if (control.actions.size) return "model-acting";
  if (control.waiting) return "model-waiting";
  if (control.lastOwner.size) return "owner-active";
  return "idle";
}

// The bridge's view: the state and counts only (no tab title, address or text).
function controlBridgeView() {
  const state = controlState();
  return {
    ok: true, state, held: control.held, acting: control.actions.size, waiting: Boolean(control.waiting),
    ownerActiveTabs: control.lastOwner.size, ownerHoldMs: CONTROL_OWNER_HOLD_MS,
  };
}

// The console's view: which tab and which tool, so the bar can say what is happening.
function controlConsoleView() {
  const state = controlState();
  return {
    state, held: control.held, activeTab: activeTabId,
    acting: [...control.actions.values()].map((a) => ({ tabId: a.tabId, tool: a.tool, since: a.since, interrupted: a.interrupted ? a.interrupted.kind : "", stopped: a.stopped || "" })),
    waiting: control.waiting ? { tabId: control.waiting.tabId, tool: control.waiting.tool } : null,
    ownerTabs: [...control.lastOwner.keys()],
    last: control.last ? { ...control.last } : null,
  };
}

function pushControlNow() {
  controlPushTimer = null;
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send("pane:control", controlConsoleView());
}
function pushControl() {
  if (!controlPushTimer) controlPushTimer = setTimeout(pushControlNow, 40);
}
// The bar changes by itself when the owner's hold or the agent's wait lapses: one timer for the next lapse.
function armControlExpiry() {
  if (controlExpiryTimer) clearTimeout(controlExpiryTimer);
  const now = Date.now();
  let next = Infinity;
  for (const t of control.lastOwner.values()) next = Math.min(next, t + CONTROL_OWNER_HOLD_MS);
  if (control.waiting) next = Math.min(next, control.waiting.until);
  for (const a of control.actions.values()) next = Math.min(next, a.seen + CONTROL_LEASE_MS + 1);
  if (next === Infinity) { controlExpiryTimer = null; return; }
  controlExpiryTimer = setTimeout(() => {
    controlExpiryTimer = null;
    pushControl();
    armControlExpiry();
  }, Math.max(20, next - now + 20));
  if (typeof controlExpiryTimer.unref === "function") controlExpiryTimer.unref();
}

// The owner's own input on a pane tab. Every action the agent holds on that tab is interrupted at
// once, here, in the one event loop that also applies the agent's text: its next step is refused.
function noteOwnerInput(tab, kind) {
  if (!tab || tabs.get(tab.id) !== tab) return;
  const now = Date.now();
  const first = !control.lastOwner.has(tab.id);
  control.lastOwner.set(tab.id, now);
  let hit = false;
  for (const a of control.actions.values()) {
    if (a.tabId === tab.id && !a.interrupted && !a.stopped) {
      a.interrupted = { kind, at: now };
      hit = true;
    }
  }
  if (first || hit) pushControl();
  armControlExpiry();
}

function agentPointerOpen(tab, input, now) {
  for (const a of control.actions.values()) {
    if (a.tabId !== tab.id || !a.pointer || a.pointer.until < now) continue;
    const at = a.pointer.at;
    if (!at || input.type !== "mouseDown" || !Number.isFinite(input.x) || !Number.isFinite(input.y)) return true;
    // The agent names its point in CSS pixels; the event carries widget pixels (CSS times the zoom).
    const wc = liveContents(tab);
    const zoom = wc ? wc.getZoomFactor() : 1;
    for (const s of [1, zoom]) {
      if (Math.abs(input.x - at.x * s) <= CONTROL_POINTER_SLOP_PX && Math.abs(input.y - at.y * s) <= CONTROL_POINTER_SLOP_PX) return true;
    }
  }
  return false;
}

function onPaneInputEvent(tab, input) {
  if (!input || !OWNER_POINTER_EVENTS.has(input.type)) return; // key events: see before-input-event
  if (agentPointerOpen(tab, input, Date.now())) return;
  noteOwnerInput(tab, input.type === "mouseWheel" ? "scroll" : input.type === "mouseDown" ? "click" : "touch");
}

// Why an action may not take its next step, or null when it may.
function controlVerdict(a) {
  if (!a) return { reason: "expired" };
  a.seen = Date.now();
  if (a.stopped) return { reason: a.stopped };
  if (control.held) return { reason: "owner-control" };
  if (a.interrupted) return { reason: "owner-input", kind: a.interrupted.kind };
  if (!tabs.has(a.tabId) || !liveContents(tabs.get(a.tabId))) return { reason: "no-tab" };
  return null;
}

// Stop every action in flight (the console's Stop and Take control). A navigation the agent
// started is stopped too; the agent's next step is refused and it is told why.
function controlStopAll(why) {
  let n = 0;
  for (const a of control.actions.values()) {
    if (a.stopped) continue;
    a.stopped = why;
    n += 1;
    const wc = liveContents(tabs.get(a.tabId));
    if (wc && CONTROL_NAVIGATING_TOOLS.has(a.tool) && wc.isLoading()) wc.stop();
  }
  pushControl();
  return n;
}

async function controlRoute(name, obj) {
  const now = Date.now();
  controlSweep(now);
  if (name === "begin") {
    const tabId = Number(obj.tab);
    const tool = typeof obj.tool === "string" && CONTROL_TOOL_RE.test(obj.tool) ? obj.tool : "act";
    if (!tabs.has(tabId)) return [404, { ok: false, reason: "no-tab" }];
    if (control.held) return [409, { ok: false, reason: "owner-control" }];
    const last = control.lastOwner.get(tabId);
    if (last !== undefined && now - last < CONTROL_OWNER_HOLD_MS) {
      control.waiting = { tabId, tool, until: now + 1000 };
      pushControl();
      armControlExpiry();
      return [409, { ok: false, reason: "owner-active", retryInMs: CONTROL_OWNER_HOLD_MS - (now - last) }];
    }
    const id = crypto.randomBytes(12).toString("base64url");
    control.actions.set(id, { id, tabId, tool, since: now, seen: now, interrupted: null, stopped: "", pointer: null, typed: 0 });
    if (control.waiting && control.waiting.tabId === tabId) control.waiting = null;
    pushControl();
    armControlExpiry();
    return [200, { ok: true, action: id, ownerHoldMs: CONTROL_OWNER_HOLD_MS }];
  }
  const a = control.actions.get(String(obj.action || ""));
  if (name === "end") {
    if (a) {
      control.actions.delete(a.id);
      const outcome = CONTROL_OUTCOMES.has(obj.outcome) ? obj.outcome : "done";
      control.last = { tool: a.tool, outcome: a.stopped && outcome !== "done" ? a.stopped : outcome, at: now };
      pushControl();
    }
    return [200, { ok: true }];
  }
  const no = controlVerdict(a);
  if (name === "check") return no ? [409, { ok: false, ...no }] : [200, { ok: true }];
  if (name === "pointer") {
    if (obj.on === true) {
      if (no) return [409, { ok: false, ...no }];
      const ms = Math.max(50, Math.min(CONTROL_POINTER_MAX_MS, Number(obj.ms) || 1500));
      const at = Number.isFinite(obj.x) && Number.isFinite(obj.y) ? { x: obj.x, y: obj.y } : null;
      a.pointer = { until: now + ms, at };
    } else if (a && a.pointer) {
      a.pointer.until = Math.min(a.pointer.until, now + CONTROL_POINTER_GRACE_MS);
    }
    return [200, { ok: true }];
  }
  if (name === "type") {
    if (no) return [409, { ok: false, ...no }];
    const text = obj.text;
    if (typeof text !== "string" || !text || text.length > CONTROL_TYPE_MAX) return [400, { ok: false, reason: "bad-text", error: `text must be 1 to ${CONTROL_TYPE_MAX} characters` }];
    const wc = liveContents(tabs.get(a.tabId));
    await wc.insertText(text);
    a.typed += text.length;
    return [200, { ok: true, typed: text.length }];
  }
  return [404, { ok: false, error: "not found" }];
}

ipcMain.handle("control:state", (evt) => (consoleOnly(evt) ? controlConsoleView() : null));
ipcMain.handle("control:stop", (evt) => (consoleOnly(evt) ? { ok: true, stopped: controlStopAll("stopped") } : REFUSED));
ipcMain.handle("control:take", (evt) => {
  if (!consoleOnly(evt)) return REFUSED;
  control.held = true;
  return { ok: true, stopped: controlStopAll("owner-control") };
});
ipcMain.handle("control:release", (evt) => {
  if (!consoleOnly(evt)) return REFUSED;
  control.held = false;
  pushControl();
  return { ok: true };
});

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

// ------------------- site permissions, passwords, autofill: console IPC (B2) ------------------- //
// Every handler below answers ONLY the console window's top page. A page in the pane has no IPC
// at all, and the pane bridge (the HTTP door) has no route that grants or reads any of this.
// That is what keeps a driven page, the browser agent and the server from granting themselves
// a permission or reading a password.

function consoleOnly(evt) {
  const frame = evt && evt.senderFrame;
  return fromConsole(evt) && Boolean(frame) && frame.parent === null;
}
const REFUSED = { ok: false, error: "only the console may do that" };

ipcMain.handle("pane:privacy", (evt) => (consoleOnly(evt) ? privacyState() : null));

ipcMain.handle("pane:perm-answer", (evt, id, decision) => {
  if (!consoleOnly(evt)) return REFUSED;
  if (typeof id !== "string" || !["allow", "once", "block", "dismiss"].includes(decision)) return { ok: false, error: "bad request" };
  return { ok: Boolean(promptQueue.answer(id, decision)) };
});

ipcMain.handle("site:perms", (evt) => (consoleOnly(evt) ? { ok: true, sites: permLib.listSites(siteTable()) } : REFUSED));
ipcMain.handle("site:perm-set", (evt, origin, key, decision) => {
  if (!consoleOnly(evt)) return REFUSED;
  if (typeof origin !== "string" || typeof key !== "string" || !["allow", "block", "reset"].includes(decision)) return { ok: false, error: "bad request" };
  if (decision === "reset") {
    const r = permLib.clearDecision(siteTable(), origin, key);
    storeSites(r.sites);
    schedulePush();
    return { ok: true, removed: r.removed };
  }
  const r = permLib.setDecision(siteTable(), origin, key, decision);
  if (!r.ok) return { ok: false, error: r.error };
  storeSites(r.sites);
  schedulePush();
  return { ok: true };
});
ipcMain.handle("site:perm-forget", (evt, origin) => {
  if (!consoleOnly(evt) || typeof origin !== "string") return REFUSED;
  const r = permLib.clearDecision(siteTable(), origin);
  storeSites(r.sites);
  schedulePush();
  return { ok: true, removed: r.removed };
});
ipcMain.handle("site:perms-clear", (evt) => {
  if (!consoleOnly(evt)) return REFUSED;
  const n = permLib.countSites(siteTable()).decisions;
  storeSites({});
  schedulePush();
  return { ok: true, removed: n };
});

ipcMain.handle("pw:list", (evt) => (consoleOnly(evt) ? { ok: true, available: cipher.available(), entries: vault.list(), never: vault.never() } : REFUSED));
ipcMain.handle("pw:delete", (evt, id) => {
  if (!consoleOnly(evt) || typeof id !== "string") return REFUSED;
  const ok = vault.remove(id);
  schedulePush();
  return { ok };
});
ipcMain.handle("pw:never-remove", (evt, origin) => {
  if (!consoleOnly(evt) || typeof origin !== "string") return REFUSED;
  const ok = vault.removeNever(origin);
  schedulePush();
  return { ok };
});
ipcMain.handle("pw:save-answer", (evt, id, answer) => {
  if (!consoleOnly(evt)) return REFUSED;
  if (typeof id !== "string" || !["save", "never", "dismiss"].includes(answer)) return { ok: false, error: "bad request" };
  let found = null;
  for (const [tabId, s] of pendingSaves) if (s.id === id) found = [tabId, s];
  if (!found) return { ok: false, error: "that prompt is gone" };
  const [tabId, s] = found;
  let result = { ok: true, status: "dismissed" };
  if (answer === "save") result = vault.save({ origin: s.origin, username: s.username, password: s.password });
  else if (answer === "never") result = { ok: vault.addNever(s.origin), status: "never" };
  s.password = "";
  pendingSaves.delete(tabId);
  schedulePush();
  return result;
});
// Finding #163: a CDP client can call the console's IPC, so a fill requested over IPC needs a
// native confirmation a script cannot press. The field-click menu is itself native and needs none.
let fillConfirming = false;
async function confirmFillNatively(kind, c) {
  if (fillConfirming) return false;
  fillConfirming = true;
  try {
    const box = await dialog.showMessageBox(mainWindow, {
      type: "question", buttons: [`Fill ${kind}`, "Cancel"], defaultId: 1, cancelId: 1, noLink: true,
      title: `Fill saved ${kind}`,
      message: `Fill the saved ${kind} for ${c.username || c.label || "this entry"} into the page now showing?`,
      detail: c.origin ? `Site: ${hostOfOrigin(c.origin)}` : "",
    });
    return box.response === 0;
  } finally {
    fillConfirming = false;
  }
}
ipcMain.handle("pw:fill", async (evt, entryId) => {
  if (!consoleOnly(evt) || typeof entryId !== "string") return REFUSED;
  const c = vault.credentials(entryId);
  if (!c) return { ok: false, error: "that login cannot be read" };
  if (!(await confirmFillNatively("password", c))) return { ok: false, error: "cancelled" };
  return fillLogin(activeTab(), entryId);
});
// Showing a password needs a person: a native dialog, which only a real click answers. The
// console's own confirmation comes first; this is the one a script driving the console cannot press.
let revealing = false;
ipcMain.handle("pw:reveal", async (evt, id) => {
  if (!consoleOnly(evt) || typeof id !== "string") return REFUSED;
  if (revealing) return { ok: false, error: "another confirmation is already open" };
  const c = vault.credentials(id);
  if (!c) return { ok: false, error: "that login cannot be read" };
  revealing = true;
  try {
    const box = await dialog.showMessageBox(mainWindow, {
      type: "question", buttons: ["Show password", "Cancel"], defaultId: 1, cancelId: 1, noLink: true,
      title: "Show saved password",
      message: `Show the saved password for ${c.username || "this login"} on ${hostOfOrigin(c.origin)}?`,
      detail: "Anyone who can see your screen will be able to read it.",
    });
    if (box.response !== 0) return { ok: false, error: "cancelled" };
    return { ok: true, password: c.password };
  } finally {
    revealing = false;
  }
});

ipcMain.handle("addr:list", (evt) => (consoleOnly(evt) ? { ok: true, available: cipher.available(), profiles: addressBook.list() } : REFUSED));
ipcMain.handle("addr:save", (evt, raw, id) => {
  if (!consoleOnly(evt)) return REFUSED;
  const r = addressBook.save(raw, typeof id === "string" ? id : undefined);
  schedulePush();
  return r;
});
ipcMain.handle("addr:delete", (evt, id) => {
  if (!consoleOnly(evt) || typeof id !== "string") return REFUSED;
  const ok = addressBook.remove(id);
  schedulePush();
  return { ok };
});
ipcMain.handle("addr:fill", async (evt, id) => {
  if (!consoleOnly(evt) || typeof id !== "string") return REFUSED;
  if (!(await confirmFillNatively("address", { label: "this address" }))) return { ok: false, error: "cancelled" };
  return fillAddress(activeTab(), id);
});


// ------------------- extensions, profiles, import, DRM: console IPC (B3) ------------------- //
// The same rule as B2: a CDP client on the local machine can call the console's IPC, so anything
// privileged here ends in a NATIVE macOS dialog that a script cannot press, and nothing here is
// reachable from the pane bridge (the HTTP door), which can only READ non-secret lists.
//   * Adding an extension: a native folder picker, then a native confirmation that names the
//     extension and every permission it asks for, then a copy into the app's own folder.
//   * Enabling one that was off: the same native confirmation again.
//   * Importing from Chrome: a native folder (or file) picker and a native confirmation with counts.
//   * Removing a profile: a native confirmation, because its logins go with it.

// One native dialog at a time, so a script cannot stack them.
let nativeBusy = false;
async function nativeGuard(fn) {
  if (nativeBusy) return { ok: false, error: "another confirmation is already open" };
  nativeBusy = true;
  try {
    return await fn();
  } finally {
    nativeBusy = false;
  }
}

async function confirmNatively({ title, message, detail, ok }) {
  if (!mainWindow || mainWindow.isDestroyed()) return false;
  const box = await dialog.showMessageBox(mainWindow, {
    type: "question", buttons: [ok, "Cancel"], defaultId: 1, cancelId: 1, noLink: true, title, message, detail: detail || "",
  });
  return box.response === 0;
}

async function pickNatively(options) {
  if (!mainWindow || mainWindow.isDestroyed()) return "";
  const r = await dialog.showOpenDialog(mainWindow, options);
  return !r || r.canceled || !Array.isArray(r.filePaths) || !r.filePaths[0] ? "" : String(r.filePaths[0]);
}

// ---- extensions ----

const extensionStore = makeStore("extensions.json", { entries: [] }, { mode: 0o600, lazy: true });
let extensionRegistry = null;
function extRegistry() {
  if (!extensionRegistry) extensionRegistry = extLib.sanitizeRegistry(extensionStore.get());
  return extensionRegistry;
}
function saveExtRegistry(next) {
  extensionRegistry = next;
  extensionStore.set({ entries: next.entries });
  extensionStore.flush(); // an approval is not left in a debounce timer
}
function extensionsDir() {
  return path.join(browserDataDir(), "extensions");
}
function paneSession() {
  return typeof session.fromPartition === "function" ? session.fromPartition(TAB_WEB_PREFERENCES.partition) : null;
}
// Electron moved these onto `session.extensions`; the older names are the fallback.
function extApi(ses) {
  if (!ses) return null;
  if (ses.extensions && typeof ses.extensions.loadExtension === "function") return ses.extensions;
  if (typeof ses.loadExtension === "function") return { loadExtension: (p, o) => ses.loadExtension(p, o), removeExtension: (id) => ses.removeExtension(id) };
  return null;
}
// What happened to each extension in each SESSION (every profile has its own): our id ->
// { loaded, electronId, error }. Kept per session so a failed load in one profile can never make
// another profile forget the id it needs to unload what it did load.
const extStatusBySession = new WeakMap();
function statusMap(ses) {
  let m = extStatusBySession.get(ses);
  if (!m) {
    m = new Map();
    extStatusBySession.set(ses, m);
  }
  return m;
}
const extSessionList = []; // every pane session an extension may be loaded into (one per profile used)
const extSessionInit = new WeakMap(); // session -> the promise of its first batch of loads, while it runs

function extFail(ses, id, message) {
  statusMap(ses).set(id, { loaded: false, electronId: "", error: String(message).slice(0, 300) });
  log(`extension ${id} not loaded: ${message}`);
  return { ok: false, error: String(message) };
}

// Loads one APPROVED COPY into a session. Its fingerprint must still be the one recorded when the
// owner approved it; a copy that was edited since is refused.
async function loadExtensionInto(ses, entry) {
  const api = extApi(ses);
  if (!api) return extFail(ses, entry.id, "This build of Electron has no extension support.");
  const dir = path.join(extensionsDir(), entry.id);
  let tree = "";
  try {
    tree = extLib.hashTree({ fs, path }, dir);
  } catch (exc) {
    return extFail(ses, entry.id, `Its files could not be read (${exc.message || exc}).`);
  }
  if (tree !== entry.tree) return extFail(ses, entry.id, "Its files changed after you approved it, so it was not loaded. Remove it and add it again.");
  try {
    const ext = await api.loadExtension(dir, { allowFileAccess: false });
    statusMap(ses).set(entry.id, { loaded: true, electronId: String((ext && ext.id) || ""), error: "" });
    return { ok: true };
  } catch (exc) {
    return extFail(ses, entry.id, `Electron could not load it: ${(exc && exc.message) || exc}`);
  }
}

function unloadExtensionFrom(ses, id) {
  const st = statusMap(ses).get(id);
  const api = extApi(ses);
  if (st && st.electronId && api && typeof api.removeExtension === "function") {
    try {
      api.removeExtension(st.electronId);
    } catch (exc) {
      log("extension could not be unloaded:", exc.message || exc);
    }
  }
  statusMap(ses).set(id, { loaded: false, electronId: "", error: "" });
}

// The first batch for a session: every enabled extension, one after the other. Pages opened by
// the owner wait for it (see loadInTab) so a content script is there for the first real page.
function blockExtensionsFromLocalServices(ses) {
  // Finding #164: an all-sites extension must not read the app's own local server. A request that
  // comes from an extension page or worker (no tab behind it, or an extension referrer) to a
  // loopback address is cancelled; the app's own pages in a tab are unaffected.
  try {
    ses.webRequest.onBeforeRequest({ urls: ["http://127.0.0.1:*/*", "http://localhost:*/*", "http://[::1]:*/*", "http://0.0.0.0:*/*"] }, (details, callback) => {
      const fromExtension = !details.webContents || String(details.referrer || "").startsWith("chrome-extension://");
      callback({ cancel: Boolean(fromExtension) });
    });
  } catch (exc) {
    log("could not install the local-services block:", exc.message || exc);
  }
}

function unloadExtensionEverywhere(id) {
  for (const ses of extSessionList) {
    unloadExtensionFrom(ses, id);
    statusMap(ses).delete(id);
  }
}

function startExtensions(ses) {
  if (!ses || extSessionInit.has(ses)) return;
  extSessionList.push(ses);
  blockExtensionsFromLocalServices(ses);
  const enabled = extRegistry().entries.filter((e) => e.enabled);
  if (!enabled.length) {
    extSessionInit.set(ses, null);
    return;
  }
  const run = (async () => {
    for (const e of enabled) await loadExtensionInto(ses, e);
  })().catch((exc) => log("extensions: loading stopped:", exc.message || exc)).finally(() => {
    extSessionInit.set(ses, null);
    schedulePush();
  });
  extSessionInit.set(ses, run);
}

function extensionList() {
  const ses = paneSession();
  return {
    ok: true, supported: Boolean(extApi(ses)), note: extLib.EXTENSION_SUPPORT_NOTE,
    extensions: extRegistry().entries.map((e) => extLib.publicView(e, statusMap(ses).get(e.id))),
  };
}

function readOrNull(p) {
  try {
    // Finding #164: a crafted folder can make this a symlink to /dev/zero or a FIFO.
    const st = fs.lstatSync(p);
    if (!st.isFile() || st.size > 262144) return null;
    return fs.readFileSync(p, "utf8");
  } catch {
    return null;
  }
}

async function addExtensionFlow() {
  if (!extApi(paneSession())) return { ok: false, error: "This build of Electron has no extension support." };
  if (extRegistry().entries.length >= extLib.MAX_EXTENSIONS) return { ok: false, error: `At most ${extLib.MAX_EXTENSIONS} extensions.` };
  const src = await pickNatively({
    title: "Choose an unpacked extension folder", message: "Pick the folder that holds the extension's manifest.json.",
    buttonLabel: "Choose", properties: ["openDirectory"],
  });
  if (!src) return { ok: false, cancelled: true, error: "cancelled" };
  const manifestPath = path.join(src, "manifest.json");
  let st;
  try {
    st = fs.lstatSync(manifestPath);
  } catch {
    return { ok: false, error: "That folder has no manifest.json. Pick the folder of an unpacked extension." };
  }
  if (!st.isFile() || st.size > extLib.MAX_MANIFEST_BYTES) return { ok: false, error: "manifest.json is not a normal file of a sensible size." };
  const raw = fs.readFileSync(manifestPath);
  let manifest;
  try {
    manifest = JSON.parse(raw.toString("utf8"));
  } catch {
    return { ok: false, error: "manifest.json is not valid JSON." };
  }
  const info = extLib.inspectManifest(manifest, (loc) => readOrNull(path.join(src, "_locales", loc, "messages.json")));
  if (!info.ok) return { ok: false, error: info.error };
  const text = extLib.confirmationText(info, path.basename(src));
  if (!(await confirmNatively({ title: "Add extension", message: text.message, detail: text.detail, ok: "Add extension" }))) {
    return { ok: false, cancelled: true, error: "cancelled" };
  }
  const id = extLib.newExtensionId(crypto.randomBytes);
  const dest = path.join(extensionsDir(), id);
  try {
    fs.mkdirSync(extensionsDir(), { recursive: true, mode: 0o700 });
    lockDown(extensionsDir(), "", 0);
  } catch (exc) {
    return { ok: false, error: `The extensions folder could not be made: ${exc.message || exc}` };
  }
  const discard = () => fs.rmSync(dest, { recursive: true, force: true });
  const copied = extLib.copyTree({ fs, path }, src, dest);
  if (!copied.ok) {
    discard();
    return { ok: false, error: copied.error };
  }
  // What was copied must be what was shown to the owner.
  let tree = "";
  try {
    if (extLib.sha256(fs.readFileSync(path.join(dest, "manifest.json"))) !== extLib.sha256(raw)) throw new Error("manifest.json changed while it was being copied");
    tree = extLib.hashTree({ fs, path }, dest);
  } catch (exc) {
    discard();
    return { ok: false, error: `The copy could not be verified: ${exc.message || exc}` };
  }
  const entry = { id, name: info.name, version: info.version, enabled: true, addedAt: Date.now(), tree, risk: info.risk, summary: info.lines.slice(0, 20) };
  const added = extLib.addEntry(extRegistry(), entry);
  if (!added.ok) {
    discard();
    return { ok: false, error: added.error };
  }
  saveExtRegistry(added.registry);
  await loadExtensionInto(paneSession(), entry);
  schedulePush();
  return { ok: true, extension: extLib.publicView(entry, statusMap(paneSession()).get(entry.id)) };
}

async function enableExtensionFlow(id) {
  const entry = extRegistry().entries.find((e) => e.id === id);
  if (!entry) return { ok: false, error: "No such extension." };
  if (entry.enabled) return { ok: true, extension: extLib.publicView(entry, statusMap(paneSession()).get(entry.id)) };
  const text = extLib.confirmationText({ name: entry.name, version: entry.version, lines: entry.summary }, "(already in this browser)");
  const detail = text.detail.replace(/^Folder: .*\n\n/, "");
  if (!(await confirmNatively({ title: "Enable extension", message: `Turn "${entry.name}" on again?`, detail, ok: "Enable" }))) {
    return { ok: false, cancelled: true, error: "cancelled" };
  }
  const next = extLib.setEnabled(extRegistry(), id, true);
  saveExtRegistry(next.registry);
  await loadExtensionInto(paneSession(), { ...entry, enabled: true });
  schedulePush();
  return { ok: true, extension: extLib.publicView({ ...entry, enabled: true }, statusMap(paneSession()).get(id)) };
}

ipcMain.handle("ext:list", (evt) => (consoleOnly(evt) ? extensionList() : REFUSED));
ipcMain.handle("ext:add", (evt) => (consoleOnly(evt) ? nativeGuard(addExtensionFlow) : REFUSED));
ipcMain.handle("ext:enable", (evt, id) => {
  if (!consoleOnly(evt) || typeof id !== "string") return REFUSED;
  return nativeGuard(() => enableExtensionFlow(id));
});
ipcMain.handle("ext:disable", (evt, id) => {
  if (!consoleOnly(evt) || typeof id !== "string") return REFUSED;
  const next = extLib.setEnabled(extRegistry(), id, false);
  if (!next.ok) return { ok: false, error: next.error };
  saveExtRegistry(next.registry);
  unloadExtensionEverywhere(id);
  schedulePush();
  return { ok: true };
});
ipcMain.handle("ext:remove", (evt, id) => {
  if (!consoleOnly(evt) || typeof id !== "string") return REFUSED;
  const next = extLib.removeEntry(extRegistry(), id);
  if (!next.ok) return { ok: false, error: next.error };
  unloadExtensionEverywhere(id);
  saveExtRegistry(next.registry);
  try {
    fs.rmSync(path.join(extensionsDir(), id), { recursive: true, force: true });
  } catch (exc) {
    log("extension files could not be removed:", exc.message || exc);
  }
  schedulePush();
  return { ok: true };
});

// ---- profiles ----

// Everything a profile switch must forget: the pane's tabs (their pages belong to the old cookie
// jar), anything waiting for an answer, and every in-memory copy of a login.
function closeAllTabs() {
  for (const tab of [...tabs.values()]) {
    const wc = liveContents(tab);
    forgetTabPrivacy(tab.id);
    if (mainWindow && !mainWindow.isDestroyed() && mainWindow.getBrowserViews().includes(tab.view)) mainWindow.removeBrowserView(tab.view);
    if (wc) wc.close();
  }
  tabs.clear();
  tabOrder = [];
  closedTabs.length = 0;
  activeTabId = 0;
  paneView = null;
}

function switchProfile(rawName) {
  const next = profLib.setActive(profileRegistry, rawName);
  if (!next.ok) return { ok: false, error: next.error };
  if (next.name === activeProfileName) return { ok: true, active: activeProfileName, unchanged: true };
  for (const s of Object.values(storesFor(activeProfileName))) s.flush();
  promptQueue.dropAll();
  for (const [, s] of pendingSaves) s.password = "";
  pendingSaves.clear();
  fillCache.clear();
  closeAllTabs();
  activeProfileName = next.name;
  TAB_WEB_PREFERENCES.partition = activePartition();
  sitesChecked = false;
  saveProfileRegistry(next.registry);
  registryStore.flush();
  // The pane is never empty: the new profile's first tab is created at about:blank so the browser
  // agent can find it again, exactly as it found the first tab of the first profile.
  ensurePaneView();
  attachActiveView();
  schedulePush();
  return { ok: true, active: activeProfileName };
}

function profileListView() {
  return { ok: true, active: activeProfileName, profiles: profLib.listProfiles(profileRegistry), max: profLib.MAX_PROFILES };
}

async function removeProfileFlow(rawName) {
  const name = profLib.cleanName(rawName);
  const next = profLib.removeProfile(profileRegistry, name);
  if (!next.ok) return { ok: false, error: next.error };
  const set = storesFor(name);
  const counts = {
    passwords: set.password.get().entries ? set.password.get().entries.length : 0,
    bookmarks: set.bookmarks.get().length,
    history: set.history.get().length,
  };
  const ok = await confirmNatively({
    title: "Remove profile", ok: "Remove profile",
    message: `Remove the profile "${name}" and everything in it?`,
    detail: `Its saved passwords (${counts.passwords}), bookmarks (${counts.bookmarks}), history (${counts.history}), site permissions, cookies and logins are deleted from this Mac. This cannot be undone.`,
  });
  if (!ok) return { ok: false, cancelled: true, error: "cancelled" };
  const partition = profLib.partitionFor(name);
  profileStores.delete(name);
  try {
    fs.rmSync(profLib.dirFor(path, browserDataDir(), name), { recursive: true, force: true });
  } catch (exc) {
    log("profile files could not be removed:", exc.message || exc);
  }
  try {
    const ses = session.fromPartition(partition);
    if (ses && typeof ses.clearStorageData === "function") await ses.clearStorageData();
    if (ses && typeof ses.clearCache === "function") await ses.clearCache();
  } catch (exc) {
    log("profile storage could not be cleared:", exc.message || exc);
  }
  saveProfileRegistry(next.registry);
  registryStore.flush();
  schedulePush();
  return { ok: true };
}

ipcMain.handle("profile:list", (evt) => (consoleOnly(evt) ? profileListView() : REFUSED));
ipcMain.handle("profile:switch", (evt, name) => (consoleOnly(evt) && typeof name === "string" ? switchProfile(name) : REFUSED));
ipcMain.handle("profile:create", (evt, name) => {
  if (!consoleOnly(evt) || typeof name !== "string") return REFUSED;
  const r = profLib.addProfile(profileRegistry, name);
  if (!r.ok) return { ok: false, error: r.error };
  saveProfileRegistry(r.registry);
  registryStore.flush();
  schedulePush();
  return { ok: true, name: r.name };
});
ipcMain.handle("profile:remove", (evt, name) => {
  if (!consoleOnly(evt) || typeof name !== "string") return REFUSED;
  return nativeGuard(() => removeProfileFlow(name));
});

// ---- import from Chrome (read-only, explicit) ----

const sqliteReader = impLib.makeSqliteReader({ execFileSync: require("child_process").execFileSync, requireModule: (n) => require(n) });
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

async function importChromeFlow(want) {
  const w = { bookmarks: want && want.bookmarks !== false, history: want && want.history !== false };
  if (!w.bookmarks && !w.history) return { ok: false, error: "Choose bookmarks, history or both." };
  const dir = await pickNatively({
    title: "Choose a Chrome profile folder", message: "Pick the Chrome profile folder (for example Default). Dourmouse reads its Bookmarks and History files and nothing else.",
    buttonLabel: "Choose", properties: ["openDirectory"], defaultPath: path.join(app.getPath("home") || "", "Library", "Application Support", "Google", "Chrome"),
  });
  if (!dir) return { ok: false, cancelled: true, error: "cancelled" };
  const found = impLib.readChromeProfile({ fs, path, os: require("os"), readHistory: sqliteReader }, dir, w);
  if (!found.ok) return { ok: false, error: found.error };
  const bm = found.bookmarks ? impLib.mergeBookmarks(bookmarkStore.get(), found.bookmarks, newId) : null;
  const hi = found.history ? impLib.mergeHistory(historyStore.get(), found.history, newId) : null;
  const lines = [];
  if (bm) lines.push(`${plural(bm.counts.found, "bookmark", "bookmarks")} found, ${bm.counts.added} new`);
  if (hi) lines.push(`${plural(hi.counts.found, "history entry", "history entries")} found, ${hi.counts.added} new`);
  for (const n of found.notes) lines.push(n);
  const ok = await confirmNatively({
    title: "Import from Chrome", ok: "Import",
    message: `Import into the profile "${activeProfileName}"?`,
    detail: `${lines.join("\n")}\n\nChrome's files are read, never changed. Passwords, cookies and anything else in that folder are not touched.`,
  });
  if (!ok) return { ok: false, cancelled: true, error: "cancelled" };
  if (bm) bookmarkStore.set(bm.list);
  if (hi) historyStore.set(hi.list);
  schedulePush();
  return { ok: true, profile: activeProfileName, bookmarks: bm ? bm.counts : null, history: hi ? hi.counts : null, notes: found.notes };
}

async function importPasswordsFlow() {
  if (!cipher.available()) return { ok: false, error: "This Mac's secure storage is not available, so no password can be imported. Nothing is stored without encryption." };
  const file = await pickNatively({
    title: "Choose a Chrome password export", message: "Pick the CSV file you exported from Chrome (Settings, Passwords, Export). Dourmouse reads it once and never copies it.",
    buttonLabel: "Choose", properties: ["openFile"], filters: [{ name: "Chrome password export (CSV)", extensions: ["csv"] }],
  });
  if (!file) return { ok: false, cancelled: true, error: "cancelled" };
  let st;
  try {
    st = fs.statSync(file);
  } catch {
    return { ok: false, error: "That file cannot be read." };
  }
  if (!st.isFile() || st.size > impLib.MAX_CSV_BYTES) return { ok: false, error: "That file is not a CSV export of a sensible size." };
  const parsed = impLib.passwordRowsFromCsv(fs.readFileSync(file, "utf8"));
  if (!parsed.ok) return { ok: false, error: parsed.error };
  if (!parsed.rows.length) return { ok: false, error: "No password in that file can be saved (only https sites and this Mac are saved)." };
  const c = parsed.counts;
  const skipped = c.notWeb + c.notSecure + c.noPassword + c.tooLong;
  const ok = await confirmNatively({
    title: "Import passwords", ok: "Import passwords",
    message: `Import ${plural(parsed.rows.length, "password", "passwords")} from ${path.basename(file)} into the profile "${activeProfileName}"?`,
    detail: `${skipped ? `${plural(skipped, "row", "rows")} cannot be saved (not a secure web address, or no password).\n` : ""}` +
      "They are encrypted with this Mac's Keychain key and kept on this Mac only. The file is not copied or changed. " +
      "It holds your passwords in plain text, so delete it when this is done.",
  });
  if (!ok) return { ok: false, cancelled: true, error: "cancelled" };
  const result = impLib.importPasswords(vault, parsed.rows);
  fillCache.clear();
  schedulePush();
  if (result.unavailable) return { ok: false, error: "Encryption became unavailable, so nothing more was saved.", ...result };
  return { ok: true, profile: activeProfileName, rows: c.rows, usable: c.usable, skipped, ...result };
}

ipcMain.handle("import:chrome", (evt, want) => (consoleOnly(evt) ? nativeGuard(() => importChromeFlow(want && typeof want === "object" ? want : {})) : REFUSED));
ipcMain.handle("import:passwords", (evt) => (consoleOnly(evt) ? nativeGuard(importPasswordsFlow) : REFUSED));

// ---- DRM (Widevine) status ----
// Stock Electron has no Widevine. The castLabs build does, and exposes `components`; this file does
// not install it (scripts/install_drm_electron.sh and B3_DRM_PLAN.md describe that opt-in). Here
// the shell only asks, without ever throwing, and reports what is really there.

let drmState = { checked: false, hasComponents: false, ready: false, status: null, error: "" };
function startDrmCheck() {
  // DOURMOUSE_DRM=0 leaves protected video off even on a build that has it (the key system request
  // then stays refused), for an owner who prefers not to have a content decryption module at all.
  if (process.env.DOURMOUSE_DRM === "0") {
    drmState = { checked: true, hasComponents: false, ready: false, status: null, error: "", disabled: true };
    return;
  }
  drmLib.startDrm(electronApi, { log }).then((st) => {
    drmState = st;
    schedulePush();
  }).catch((exc) => {
    drmState = { checked: true, hasComponents: false, ready: false, status: null, error: String((exc && exc.message) || exc) };
  });
}
async function drmReport() {
  let probe = null;
  const wc = mainWindow && !mainWindow.isDestroyed() ? mainWindow.webContents : null;
  if (wc && typeof wc.executeJavaScriptInIsolatedWorld === "function") {
    try {
      const r = await withTimeout(wc.executeJavaScriptInIsolatedWorld(2999, [{ code: drmLib.EME_PROBE_SOURCE }]), 4000);
      probe = r && typeof r === "object" ? r : null;
    } catch {
      probe = null;
    }
  }
  return drmLib.describeDrm(drmState, probe, process.versions);
}
ipcMain.handle("drm:status", async (evt) => (consoleOnly(evt) ? { ok: true, ...(await drmReport()) } : REFUSED));

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
      // Phase B2: counts only. No site, no username, no value: see privacyCounts().
      respond(200, {
        active: paneVisible, cdpEndpoint: `http://127.0.0.1:${CDP_PORT}`, tabCount: tabs.size, activeTab: activeTabId, ...privacyCounts(),
        profile: activeProfileName, profiles: profileRegistry.names.length + 1, extensions: extLib.countEntries(extRegistry()),
        drm: { build: drmState.hasComponents ? "castlabs-ecs" : "stock-electron", ready: drmState.ready === true },
      });
    } else if (isGet && route === "/control") {
      // Phase C2: who has the pane right now. State and counts only.
      respond(200, controlBridgeView());
    } else if (isPost && /^\/control\/(begin|check|type|pointer|end)$/.test(route)) {
      // Phase C2: the browser agent's claim on a tab (see controlRoute). There is deliberately no
      // route here that stops, takes or releases control: those are the owner's, in the console.
      withBody((obj) => {
        controlRoute(route.slice("/control/".length), obj).then(
          ([status, body]) => respond(status, body),
          (exc) => respond(500, { ok: false, reason: "error", error: String((exc && exc.message) || exc) }),
        );
      });
    } else if (isGet && route === "/profiles") {
      // Read-only: the names of the profiles and which one is active. There is no route here that
      // switches, creates or removes one, and none that imports anything.
      respond(200, profileListView());
    } else if (isGet && route === "/extensions") {
      // Read-only and non-secret: names, versions and on or off. There is deliberately no route
      // here that adds, enables or removes an extension: that is the console's native dialog alone.
      const v = extensionList();
      respond(200, { ok: true, supported: v.supported, extensions: v.extensions.map((e) => ({ name: e.name, version: e.version, enabled: e.enabled, loaded: e.loaded, risk: e.risk })) });
    } else if (isGet && route === "/drm") {
      drmReport().then((r) => respond(200, { ok: true, ...r }), (exc) => respond(500, { ok: false, error: String((exc && exc.message) || exc) }));
    } else if (isGet && route === "/permissions") {
      // Read-only and non-secret: which sites hold a stored decision. There is deliberately no
      // route here that grants, changes or revokes one, and none that reads a password.
      respond(200, { ok: true, sites: permLib.listSites(siteTable()) });
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
      // Each tab also carries wcId (the webContents id) and targetId (the CDP target id): see tabTargetId.
      bridgeTabsView(params.get("refresh") === "1").then(
        (view) => respond(200, view),
        (exc) => respond(500, { ok: false, error: String((exc && exc.message) || exc) }),
      );
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
  // Phase I2: called again after a server restart; the stream of the dead server is dropped.
  if (alertStream) {
    try {
      alertStream.destroy();
    } catch (_exc) {
      /* already closed */
    }
    alertStream = null;
  }
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
  alertStream = req;
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
  if (typeof url !== "string" || !(/^https?:\/\//i.test(url) || policy.systemSettingsUrlAllowed(url))) return false;
  try {
    await shell.openExternal(url);
    return true;
  } catch {
    return false;
  }
});

// Finding #171 (phase I2 speed budget): the map window is created hidden, but Chromium still treats a
// window made with show:false as VISIBLE (document.visibilityState was "visible" in it), and the map
// page runs about 90 CSS animations and polls the server every second: measured at over 100% of a CPU
// core, from launch, for a window nobody had opened (and it slowed the first screen of the console).
// So the page is only loaded the first time the window is shown.
let mapLoaded = false;
function loadMapOnce() {
  if (mapLoaded || !mapWindow || mapWindow.isDestroyed()) return;
  mapLoaded = true;
  mapWindow.loadURL(`${BASE_URL}/map`);
}

ipcMain.handle("bridge:open_map", () => {
  if (mapWindow && !mapWindow.isDestroyed()) {
    loadMapOnce();
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
  wireConsoleRecovery(mainWindow);
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
  // Not loaded until first shown (see loadMapOnce); the smoke test below needs it loaded. It starts on
  // a one line placeholder page, not on nothing: a window that has never navigated is a DevTools target
  // without a page, which Playwright's attach (browser_agent.py) can wait on forever.
  mapWindow.loadURL("data:text/html,<title>Agent orchestration map</title>");
  if (process.env.DOURMOUSE_ELECTRON_VERIFY === "1") loadMapOnce();

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
  // B3: is this the build that has Widevine? Asked once, never blocks the start, never throws.
  startDrmCheck();

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
      wireConsoleRecovery(mainWindow);
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
  // Phase I2: quitting is also what stops the restart supervision, so this runs first.
  serverQuitting = true;
  if (!REUSE_EXISTING_SERVER) stopServer();
});
