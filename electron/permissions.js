// Dourmouse native shell -- site permissions for the browser pane (Phase B2).
//
// CommonJS with no Electron imports, like policy.js, so plain node can test every
// rule. main.js owns the Electron objects and the disk; this file owns the decisions:
// which permissions exist, which ones may ever be asked about, how a stored decision
// is looked up, and the queue of prompts waiting for the owner.
//
// The rules, in one place:
//   * Only these six are ever asked about: camera, microphone, geolocation,
//     notifications, clipboard-read, fullscreen. Everything else a page can ask for
//     (screen capture, MIDI, USB, serial, HID, openExternal, ...) is refused and never
//     prompted, whatever was stored before.
//   * Clipboard WRITE of plain text and images is allowed without a prompt, the way
//     Chrome allows it: the engine itself requires a user gesture for it.
//   * A decision belongs to an origin (scheme, host and port) and a permission. A
//     frame from another origin never inherits the top page's decision.
//   * Nothing in this file can GRANT anything on its own. A grant is stored only by
//     the console window's own IPC handler (main.js), never from the pane bridge.

const SITE_PERMISSIONS = ["camera", "microphone", "geolocation", "notifications", "clipboard-read", "fullscreen"];

// What the prompt bar says after "wants to ...". Written for the owner, not for the API.
const PERMISSION_PHRASES = {
  camera: "use your camera",
  microphone: "use your microphone",
  geolocation: "know your location",
  notifications: "show notifications",
  "clipboard-read": "see text and images you copied",
  fullscreen: "go full screen",
};

// The words used in the Site settings list.
const PERMISSION_LABELS = {
  camera: "Camera",
  microphone: "Microphone",
  geolocation: "Location",
  notifications: "Notifications",
  "clipboard-read": "Clipboard (read)",
  fullscreen: "Full screen",
};

const SILENT_ALLOW = new Set(["clipboard-sanitized-write"]);

const MAX_SITES = 1000;

function isSitePermission(key) {
  return SITE_PERMISSIONS.includes(key);
}

// scheme://host[:port] for a real web page, "" for anything else (about:, data:, file:,
// a custom scheme, a string that is not a URL). Userinfo is dropped by URL itself.
function originOf(url) {
  try {
    const u = new URL(String(url));
    if (u.protocol !== "http:" && u.protocol !== "https:") return "";
    return u.origin.toLowerCase();
  } catch {
    return "";
  }
}

// Electron names the thing a page asked for; this maps it to the decision keys.
//   { action: "ask",   keys: [...] }  needs a stored or a fresh decision for every key
//   { action: "allow", keys: [] }     fine without asking
//   { action: "deny",  keys: [] }     never
// A media request carries mediaTypes (audio, video). An empty or missing list is NOT "both":
// it is how screen sharing arrives, so it is refused.
function classifyRequest(permission, details) {
  const p = String(permission);
  if (SILENT_ALLOW.has(p)) return { action: "allow", keys: [] };
  if (p === "media") {
    const types = details && Array.isArray(details.mediaTypes) ? details.mediaTypes : [];
    const keys = [];
    for (const t of types) {
      if (t === "audio" && !keys.includes("microphone")) keys.push("microphone");
      else if (t === "video" && !keys.includes("camera")) keys.push("camera");
    }
    if (keys.length) return { action: "ask", keys };
    // No audio and no video: that is how Electron delivers getDisplayMedia (screen sharing,
    // seen live: mediaTypes is an empty list), and also any type we do not know. Never offered.
    return { action: "deny", keys: [] };
  }
  if (isSitePermission(p)) return { action: "ask", keys: [p] };
  return { action: "deny", keys: [] };
}

// A synchronous permission CHECK (navigator.permissions.query, enumerateDevices, ...)
// never prompts. For "media" Electron says which kind in details.mediaType; unknown
// means both must be allowed.
function classifyCheck(permission, details) {
  const p = String(permission);
  if (p === "media") {
    const t = details && details.mediaType;
    if (t === "audio") return { action: "ask", keys: ["microphone"] };
    if (t === "video") return { action: "ask", keys: ["camera"] };
    return { action: "ask", keys: ["microphone", "camera"] };
  }
  return classifyRequest(p, details);
}

// -------------------------- the stored decisions -------------------------- //
// { "https://meet.example": { camera: { d: "allow", at: 1759400000000 }, ... } }
// Plain objects in, plain objects out: nothing here mutates its argument.

function getDecision(sites, origin, key) {
  const site = sites && Object.prototype.hasOwnProperty.call(sites, origin) ? sites[origin] : null;
  const entry = site && Object.prototype.hasOwnProperty.call(site, key) ? site[key] : null;
  return entry && (entry.d === "allow" || entry.d === "block") ? entry.d : null;
}

function setDecision(sites, origin, key, decision, at = Date.now()) {
  if (!origin || originOf(origin) !== origin) return { sites, ok: false, error: "not a web origin" };
  if (!isSitePermission(key)) return { sites, ok: false, error: "unknown permission" };
  if (decision !== "allow" && decision !== "block") return { sites, ok: false, error: "decision must be allow or block" };
  const known = Object.prototype.hasOwnProperty.call(sites, origin);
  if (!known && Object.keys(sites).length >= MAX_SITES) return { sites, ok: false, error: "too many sites" };
  const next = { ...sites, [origin]: { ...(known ? sites[origin] : {}), [key]: { d: decision, at: Number(at) || Date.now() } } };
  return { sites: next, ok: true };
}

// key omitted: forget the whole origin.
function clearDecision(sites, origin, key) {
  if (!Object.prototype.hasOwnProperty.call(sites, origin)) return { sites, removed: 0 };
  const site = sites[origin];
  const next = { ...sites };
  if (key === undefined || key === null) {
    delete next[origin];
    return { sites: next, removed: Object.keys(site).length };
  }
  if (!Object.prototype.hasOwnProperty.call(site, key)) return { sites, removed: 0 };
  const rest = { ...site };
  delete rest[key];
  if (Object.keys(rest).length) next[origin] = rest;
  else delete next[origin];
  return { sites: next, removed: 1 };
}

function listSites(sites) {
  const out = [];
  for (const origin of Object.keys(sites || {}).sort()) {
    for (const key of SITE_PERMISSIONS) {
      const d = getDecision(sites, origin, key);
      if (d) out.push({ origin, permission: key, label: PERMISSION_LABELS[key], decision: d, at: Number(sites[origin][key].at) || 0 });
    }
  }
  return out;
}

function countSites(sites) {
  const rows = listSites(sites);
  return { origins: new Set(rows.map((r) => r.origin)).size, decisions: rows.length };
}

// A file the owner or another program edited: keep only what this code could have written.
function sanitizeSites(raw) {
  const out = {};
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return out;
  for (const origin of Object.keys(raw)) {
    if (originOf(origin) !== origin || Object.keys(out).length >= MAX_SITES) continue;
    const site = raw[origin];
    if (!site || typeof site !== "object") continue;
    for (const key of SITE_PERMISSIONS) {
      const e = site[key];
      if (e && (e.d === "allow" || e.d === "block")) {
        out[origin] = { ...(out[origin] || {}), [key]: { d: e.d, at: Number(e.at) || 0 } };
      }
    }
  }
  return out;
}

// ----------------------- "Allow this time" grants ----------------------- //
// Held in memory only, per tab and origin. They end when the tab closes or moves to
// another origin, which is Chrome's rule for a one-time grant.

function createTempGrants() {
  const grants = new Set();
  const id = (tabId, origin, key) => `${tabId}|${origin}|${key}`;
  return {
    add(tabId, origin, keys) {
      for (const k of keys) grants.add(id(tabId, origin, k));
    },
    has(tabId, origin, key) {
      return grants.has(id(tabId, origin, key));
    },
    dropTab(tabId) {
      for (const g of [...grants]) if (g.startsWith(`${tabId}|`)) grants.delete(g);
    },
    // The tab is now at newOrigin: anything it held for another origin is gone.
    dropForeign(tabId, newOrigin) {
      for (const g of [...grants]) {
        if (g.startsWith(`${tabId}|`) && g.split("|")[1] !== newOrigin) grants.delete(g);
      }
    },
    size: () => grants.size,
  };
}

// ----------------------------- prompt queue ----------------------------- //
// A page that asked and is waiting. The owner answers from the console; nobody else can.
// A request that cannot be shown (too many waiting) is refused at once, and a prompt that
// nobody answers in `ttlMs` is dismissed, which the waiter treats as "no" and does not store.

// Two more limits (B3, from the B2 review): a page that asks again and again cannot pile up an
// endless list of waiting callbacks (`maxWaiters` per prompt), and once the owner presses
// Dismiss, the same site asking for the same things is refused quietly for `dismissMs` instead
// of raising the bar again at once. Only the owner's own Dismiss is remembered: a prompt that
// expired or whose tab went away is not.
function createPromptQueue({ max = 20, maxPerTab = 4, ttlMs = 90000, maxWaiters = 50, dismissMs = 60000, now = Date.now } = {}) {
  let items = [];
  let seq = 0;
  const dismissed = new Map(); // "origin|sorted keys" -> time until which it stays refused
  const MAX_DISMISSED = 200;
  const sigOf = (origin, keys) => `${origin}|${keys.slice().sort().join(",")}`;
  const recentlyDismissed = (origin, keys) => {
    const until = dismissed.get(sigOf(origin, keys));
    if (until === undefined) return false;
    if (now() >= until) {
      dismissed.delete(sigOf(origin, keys));
      return false;
    }
    return true;
  };
  const view = (i) => ({ id: i.id, tabId: i.tabId, origin: i.origin, keys: i.keys.slice(), at: i.at });
  const finish = (item, decision) => {
    for (const w of item.waiters) {
      try {
        w(decision);
      } catch {
        /* a waiter that throws must not strand the others */
      }
    }
  };
  return {
    // waiter(decision) is called once with "allow", "once", "block" or "dismiss".
    request(tabId, origin, keys, waiter) {
      const sig = keys.slice().sort().join(",");
      if (recentlyDismissed(origin, keys)) return { ok: false, reason: "dismissed a moment ago" };
      const same = items.find((i) => i.tabId === tabId && i.origin === origin && i.keys.slice().sort().join(",") === sig);
      if (same) {
        if (same.waiters.length >= maxWaiters) return { ok: false, reason: "too many requests for this prompt" };
        same.waiters.push(waiter);
        return { ok: true, id: same.id, merged: true };
      }
      if (items.length >= max || items.filter((i) => i.tabId === tabId).length >= maxPerTab) return { ok: false, reason: "too many prompts waiting" };
      seq += 1;
      const item = { id: `p${seq}`, tabId, origin, keys: keys.slice(), at: now(), waiters: [waiter] };
      items.push(item);
      return { ok: true, id: item.id, merged: false };
    },
    // The oldest prompt of a tab, as plain data for the console. Never includes a waiter.
    forTab(tabId) {
      const i = items.find((x) => x.tabId === tabId);
      return i ? view(i) : null;
    },
    // Answer by id. Returns the prompt (so the caller can store a decision) or null.
    answer(id, decision) {
      const i = items.find((x) => x.id === id);
      if (!i) return null;
      items = items.filter((x) => x !== i);
      const data = view(i);
      if (decision === "dismiss") {
        for (const [k, until] of dismissed) if (now() >= until) dismissed.delete(k);
        if (dismissed.size >= MAX_DISMISSED) dismissed.delete(dismissed.keys().next().value);
        dismissed.set(sigOf(i.origin, i.keys), now() + dismissMs);
      }
      finish(i, decision);
      return data;
    },
    dropTab(tabId) {
      const gone = items.filter((i) => i.tabId === tabId);
      items = items.filter((i) => i.tabId !== tabId);
      gone.forEach((i) => finish(i, "dismiss"));
      return gone.length;
    },
    // The tab moved to another origin: its waiting prompts were about the old page.
    dropForeign(tabId, newOrigin) {
      const gone = items.filter((i) => i.tabId === tabId && i.origin !== newOrigin);
      items = items.filter((i) => !gone.includes(i));
      gone.forEach((i) => finish(i, "dismiss"));
      return gone.length;
    },
    expire() {
      const t = now();
      const gone = items.filter((i) => t - i.at >= ttlMs);
      items = items.filter((i) => !gone.includes(i));
      gone.forEach((i) => finish(i, "dismiss"));
      return gone.length;
    },
    dropAll() {
      const gone = items;
      items = [];
      gone.forEach((i) => finish(i, "dismiss"));
      return gone.length;
    },
    size: () => items.length,
  };
}

// The sentence in the bar: "meet.example wants to use your camera and microphone".
function promptSentence(origin, keys) {
  const phrases = keys.map((k) => PERMISSION_PHRASES[k]).filter(Boolean);
  let host = origin;
  try {
    host = new URL(origin).host;
  } catch {
    /* the origin text is shown as it is */
  }
  if (phrases.length === 0) return `${host} wants a permission`;
  const joined = phrases.length === 2 && phrases[0] === "use your camera" && phrases[1] === "use your microphone"
    ? "use your camera and microphone"
    : phrases.length === 2 && phrases[0] === "use your microphone" && phrases[1] === "use your camera"
      ? "use your microphone and camera"
      : phrases.join(" and ");
  return `${host} wants to ${joined}`;
}

module.exports = {
  SITE_PERMISSIONS, PERMISSION_LABELS, PERMISSION_PHRASES, SILENT_ALLOW, MAX_SITES,
  isSitePermission, originOf, classifyRequest, classifyCheck,
  getDecision, setDecision, clearDecision, listSites, countSites, sanitizeSites,
  createTempGrants, createPromptQueue, promptSentence,
};
