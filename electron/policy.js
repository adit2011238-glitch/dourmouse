// Dourmouse native shell -- pure security decisions (findings S34, S35).
//
// CommonJS with no Electron imports on purpose, so plain node can test every
// rule (dourmouse/tests/test_electron_hardening.py). main.js wires these into
// the real session and window handlers.

const DEFAULT_PORT = 8765;

function appPort(port) {
  if (port !== undefined && port !== null) return String(port);
  return String(parseInt(process.env.DOURMOUSE_UI_PORT || String(DEFAULT_PORT), 10));
}

// True only for the app's own origin: http on 127.0.0.1 or localhost at the
// server port. Parsed with URL, so "http://127.0.0.1:8765@evil.example" and
// "http://127.0.0.1:8765.evil.example" are judged by their real host.
function isAppOrigin(url, port) {
  let parsed;
  try {
    parsed = new URL(String(url));
  } catch {
    return false;
  }
  if (parsed.protocol !== "http:") return false;
  if (parsed.hostname !== "127.0.0.1" && parsed.hostname !== "localhost") return false;
  return parsed.port === appPort(port);
}

// The only permissions the app's own pages use: the microphone and camera
// (push-to-talk, voice page, hand-tracking; all started by an explicit user
// action) and clipboard writes (the COPY buttons). Web Notifications are not
// used (alerts go through the main process), and geolocation, clipboard reads,
// screen capture and the rest are never needed.
// "fullscreen" is the media player's fullscreen button (finding #139).
const APP_PERMISSIONS = new Set(["media", "clipboard-sanitized-write", "fullscreen"]);

// Deny by default. A remote page (anything in the browser pane) is never
// granted anything, whatever it asks for.
function permissionAllowed(permission, origin, port) {
  return APP_PERMISSIONS.has(permission) && isAppOrigin(origin, port);
}

// Main and task windows may only ever show the app itself.
function navigationAllowed(url, port) {
  return isAppOrigin(url, port);
}

// Anything else is handed to the OS browser, and only for plain web links:
// never file:, javascript:, data: or a custom scheme handler.
// Finding #169: the APPS screen's permission buttons open exactly one kind of non-web link,
// the macOS Privacy & Security panes. Nothing else outside http(s) is ever handed to the OS.
const SYSTEM_SETTINGS_PRIVACY_RE = /^x-apple\.systempreferences:com\.apple\.preference\.security\?Privacy_[A-Za-z]{1,40}$/;

function systemSettingsUrlAllowed(url) {
  return typeof url === "string" && SYSTEM_SETTINGS_PRIVACY_RE.test(url);
}

function externalUrlAllowed(url) {
  try {
    const protocol = new URL(String(url)).protocol;
    return protocol === "http:" || protocol === "https:";
  } catch {
    return false;
  }
}


// --------------------------------------------------------------------- //
// Browser pane helpers (B1: tabs, downloads, zoom, history, bookmarks).
// Pure on purpose, like everything above: main.js owns the Electron objects and
// the disk, these own the decisions, so plain node tests every one of them.
// --------------------------------------------------------------------- //

// The pane only ever loads real web pages. The same rule guards /navigate, the
// console's own address bar, new tabs and pop-ups: never file:, data:, javascript:,
// about: or a custom scheme, whoever asks. (The internal blank and new-tab pages
// are loaded by main.js itself, never from a caller's URL.)
const MAX_PANE_URL = 8192;
function paneUrlAllowed(url) {
  return typeof url === "string" && url.length <= MAX_PANE_URL && /^https?:\/\/[^\s/]/i.test(url);
}

// Internal blank pages (the agent's about:blank anchor tab and the data: new tab
// page) are not addresses the owner should see.
function isBlankUrl(url) {
  const u = String(url || "");
  return u === "" || u === "about:blank" || (u.startsWith("data:text/html") && u.endsWith("#dm-newtab"));
}

// At most `max` events inside any `windowMs` window. A page that opens tabs or
// downloads in a loop gets cut off instead of filling the strip or the disk.
function createLimiter(max, windowMs, now = Date.now) {
  let stamps = [];
  return {
    allow() {
      const t = now();
      stamps = stamps.filter((s) => t - s < windowMs);
      if (stamps.length >= max) return false;
      stamps.push(t);
      return true;
    },
  };
}

// Why a renderer ended. "killed" and "clean-exit" are somebody's decision (the owner
// closing the tab, the OS under memory pressure); the rest are the process dying.
const CRASH_REASONS = new Set(["crashed", "abnormal-exit", "launch-failed", "oom", "integrity-failure"]);
function isCrashReason(reason) {
  return CRASH_REASONS.has(String(reason));
}

// Chrome's zoom ladder.
const ZOOM_STEPS = [0.25, 0.33, 0.5, 0.67, 0.75, 0.8, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2, 2.5, 3, 4, 5];
function nextZoom(current, direction) {
  const c = Number.isFinite(current) && current > 0 ? current : 1;
  if (direction === "reset") return 1;
  if (direction === "in") {
    const hit = ZOOM_STEPS.find((z) => z > c + 0.001);
    return hit === undefined ? ZOOM_STEPS[ZOOM_STEPS.length - 1] : hit;
  }
  if (direction === "out") {
    const lower = ZOOM_STEPS.filter((z) => z < c - 0.001);
    return lower.length ? lower[lower.length - 1] : ZOOM_STEPS[0];
  }
  return c;
}

// Zoom is remembered per site, the way Chrome does it. "" means no site (a blank page).
function zoomHost(url) {
  try {
    const u = new URL(String(url));
    return u.protocol === "http:" || u.protocol === "https:" ? u.hostname.toLowerCase() : "";
  } catch {
    return "";
  }
}

// A name a download may be saved under: the last path piece only, no control
// characters, no leading dots (no hidden files), bounded, never empty.
function safeFileName(name) {
  let n = String(name === undefined || name === null ? "" : name).replace(/[\u0000-\u001f\u007f]/g, "");
  n = n.split(/[\\/]/).pop() || "";
  n = n.replace(/^[\s.]+/, "").replace(/[\s.]+$/, "");
  if (n.length > 120) {
    const dot = n.lastIndexOf(".");
    const ext = dot > 0 && n.length - dot <= 12 ? n.slice(dot) : "";
    n = n.slice(0, 120 - ext.length) + ext;
  }
  return n || "download";
}

// "report.pdf" -> "report (1).pdf" -> "report (2).pdf" when the name is taken, so a
// download never overwrites a file that is already in the folder.
function uniqueFileName(name, exists) {
  if (!exists(name)) return name;
  const dot = name.lastIndexOf(".");
  const base = dot > 0 ? name.slice(0, dot) : name;
  const ext = dot > 0 ? name.slice(dot) : "";
  for (let i = 1; i < 10000; i += 1) {
    const candidate = `${base} (${i})${ext}`;
    if (!exists(candidate)) return candidate;
  }
  return `${base} (${Date.now()})${ext}`;
}

// Files that run code when opened. The downloads shelf never opens these for the
// owner: it shows them in Finder instead, where Gatekeeper and the quarantine flag
// stand between the file and a launch. Anything with no extension is treated the
// same way.
const EXECUTABLE_EXT = new Set([
  ".app", ".command", ".pkg", ".mpkg", ".dmg", ".iso", ".sh", ".bash", ".zsh", ".csh", ".tcsh", ".ksh", ".fish",
  ".scpt", ".scptd", ".applescript", ".workflow", ".action", ".service", ".terminal", ".tool", ".jar", ".py", ".pl", ".rb",
  ".php", ".js", ".mjs", ".jse", ".vbs", ".wsf", ".ps1", ".bat", ".cmd", ".exe", ".msi", ".com", ".scr", ".lnk",
  ".webloc", ".inetloc", ".url", ".osax", ".prefpane", ".saver", ".plugin", ".kext", ".xpc", ".dylib", ".so", ".pyc",
  ".mobileconfig", ".configprofile", ".ics",
]);
function isOpenableDownload(name) {
  const n = String(name || "");
  const dot = n.lastIndexOf(".");
  if (dot <= 0) return false;
  return !EXECUTABLE_EXT.has(n.slice(dot).toLowerCase());
}

// What a browser writes on a download so Gatekeeper and ~/Downloads' own watcher
// (dourmouse/security/downloads.py) know where a file came from: the quarantine
// flag (flags;hex seconds;agent;uuid) and kMDItemWhereFroms.
function quarantineValue(nowMs, agent, uuid) {
  return `0081;${Math.floor(nowMs / 1000).toString(16)};${String(agent).replace(/[;\s]/g, "")};${String(uuid).replace(/[^0-9A-Fa-f-]/g, "")}`;
}
function xmlEscape(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&apos;" })[c]);
}
function whereFromPlist(urls) {
  const items = (urls || []).filter((u) => typeof u === "string" && u).slice(0, 4).map((u) => `<string>${xmlEscape(u.slice(0, 2048))}</string>`);
  return `<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd"><plist version="1.0"><array>${items.join("")}</array></plist>`;
}

function percent(received, total) {
  if (!(total > 0)) return null;
  return Math.max(0, Math.min(100, Math.round((received / total) * 100)));
}

// ---- history and bookmarks (plain arrays in, plain arrays out) ----------------

const HISTORY_CAP = 5000;
const BOOKMARK_CAP = 1000;
const MAX_STORED_URL = 2048;

function cleanTitle(t) {
  return String(t === undefined || t === null ? "" : t).replace(/[\u0000-\u001f\u007f]/g, " ").trim().slice(0, 300);
}

// Newest first. A reload or a redirect that lands on the same address within
// `dedupeMs` is one visit, not several.
function addVisit(list, entry, opts = {}) {
  const cap = opts.cap || HISTORY_CAP;
  const dedupeMs = opts.dedupeMs === undefined ? 30000 : opts.dedupeMs;
  if (!entry || !paneUrlAllowed(entry.url) || entry.url.length > MAX_STORED_URL) return list;
  const next = { id: entry.id, url: entry.url, title: cleanTitle(entry.title), at: Number(entry.at) || Date.now() };
  const head = list[0];
  if (head && head.url === next.url && next.at - head.at < dedupeMs) {
    const merged = { ...head, at: next.at, title: next.title || head.title };
    return [merged, ...list.slice(1)];
  }
  return [next, ...list].slice(0, cap);
}

function updateVisitTitle(list, url, title) {
  const i = list.findIndex((e) => e.url === url);
  if (i < 0 || i > 3) return list; // only the newest few: a title belongs to the visit that just happened
  const t = cleanTitle(title);
  if (!t || list[i].title === t) return list;
  const copy = list.slice();
  copy[i] = { ...copy[i], title: t };
  return copy;
}

function clampLimit(limit, dflt = 200, max = 1000) {
  const n = parseInt(limit, 10);
  if (!Number.isFinite(n) || n < 1) return dflt;
  return Math.min(max, n);
}

function searchHistory(list, query, limit) {
  const q = String(query || "").trim().toLowerCase().slice(0, 200);
  const out = [];
  const max = clampLimit(limit);
  for (const e of list) {
    if (q && !(e.title.toLowerCase().includes(q) || e.url.toLowerCase().includes(q))) continue;
    out.push(e);
    if (out.length >= max) break;
  }
  return out;
}

function removeFrom(list, { id, url }) {
  const next = list.filter((e) => !((id !== undefined && id !== null && e.id === id) || (id === undefined && url && e.url === url)));
  return { list: next, removed: list.length - next.length };
}

function addBookmark(list, entry) {
  if (!entry || !paneUrlAllowed(entry.url) || entry.url.length > MAX_STORED_URL) return { list, added: false, entry: null };
  const existing = list.find((b) => b.url === entry.url);
  if (existing) {
    const title = cleanTitle(entry.title) || existing.title;
    const updated = { ...existing, title };
    return { list: list.map((b) => (b === existing ? updated : b)), added: false, entry: updated };
  }
  if (list.length >= BOOKMARK_CAP) return { list, added: false, entry: null };
  const made = { id: entry.id, url: entry.url, title: cleanTitle(entry.title) || entry.url.slice(0, 80), at: Number(entry.at) || Date.now() };
  return { list: [...list, made], added: true, entry: made };
}

module.exports = {
  systemSettingsUrlAllowed,
  isAppOrigin, permissionAllowed, navigationAllowed, externalUrlAllowed, APP_PERMISSIONS,
  paneUrlAllowed, isBlankUrl, isCrashReason, createLimiter, ZOOM_STEPS, nextZoom, zoomHost, safeFileName, uniqueFileName,
  isOpenableDownload, quarantineValue, whereFromPlist, percent, addVisit, updateVisitTitle, searchHistory,
  removeFrom, addBookmark, clampLimit, cleanTitle, HISTORY_CAP, BOOKMARK_CAP, MAX_PANE_URL,
};
