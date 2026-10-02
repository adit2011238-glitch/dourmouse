// Dourmouse native shell -- unpacked Chrome extensions for the browser pane (Phase B3).
//
// CommonJS with no Electron imports, like policy.js, so plain node can test every rule.
// main.js owns session.extensions and the native dialogs; this file owns the decisions:
// what a manifest says in words the owner can judge, which folders may be copied, how the
// approved copy is fingerprinted, and what the stored list looks like after each change.
//
// What this is NOT: the Chrome Web Store. Electron implements only part of the chrome.*
// extension APIs (see EXTENSION_SUPPORT_NOTE), loads UNPACKED folders only (no .crx), and
// has no toolbar buttons or popups. An extension that depends on an API Electron lacks loads
// but does not work, and the panel says so before the owner adds one.
//
// The rules that keep it safe:
//   * An extension is added only by the console window, after a native folder dialog and a
//     native confirmation that lists the manifest's name and every permission it asks for.
//     A web page cannot add one and the pane bridge (the HTTP door) has no route that does.
//   * What is loaded is the APPROVED COPY inside the app's own folder, never the folder the
//     owner picked, and its fingerprint is checked on every load, so an edit made after the
//     approval is refused instead of silently running.

const crypto = require("crypto");

const MAX_EXTENSIONS = 20;
const MAX_MANIFEST_BYTES = 1024 * 1024;
const MAX_TREE_BYTES = 30 * 1024 * 1024;
const MAX_TREE_FILES = 3000;
const MAX_TREE_DEPTH = 12;
const SKIPPED_NAMES = new Set([".git", ".DS_Store", ".svn", ".hg"]);

// Said to the owner in the panel and in the confirmation. Deliberately modest.
const EXTENSION_SUPPORT_NOTE =
  "Dourmouse loads unpacked Chrome extensions with Electron's own extension support, which implements only part of Chrome's " +
  "extension APIs. Checked on Electron 44.3 with a probe extension: content scripts and chrome.runtime, chrome.storage and " +
  "chrome.i18n work there, and the background worker also has chrome.tabs, scripting, alarms, webRequest, declarativeNetRequest, " +
  "management, action and proxy (an API being present is not a promise it behaves like Chrome's). Not available: cookies, " +
  "history, bookmarks, downloads, context menus, notifications, web navigation, windows, identity, sessions and the side panel. " +
  "There are no toolbar buttons or popups. The Chrome Web Store is not available and packed .crx files are not accepted.";

// Plain words for the permissions an owner can reasonably judge. Anything not listed is shown
// by its own name rather than hidden.
const PERMISSION_WORDS = {
  tabs: "see the address and title of your tabs",
  activeTab: "use the tab you are on when you click it",
  storage: "keep its own settings on this Mac",
  unlimitedStorage: "keep a lot of its own data on this Mac",
  cookies: "read and change cookies (including sign-in cookies) for the sites it can reach",
  history: "read and change your browsing history",
  bookmarks: "read and change your bookmarks",
  downloads: "start, see and manage downloads",
  webRequest: "watch the requests your pages make",
  webRequestBlocking: "block or change the requests your pages make",
  declarativeNetRequest: "block or change requests by rule",
  declarativeNetRequestWithHostAccess: "block or change requests by rule on the sites it can reach",
  scripting: "run its own code in pages",
  webNavigation: "see the pages you move between",
  clipboardRead: "read what you copied",
  clipboardWrite: "change what you copied",
  contextMenus: "add items to the right-click menu",
  notifications: "show notifications",
  geolocation: "know your location",
  management: "see and control your other extensions",
  nativeMessaging: "talk to programs on your Mac outside the browser",
  debugger: "take full control of any page it can reach (the debugger)",
  proxy: "change how the browser connects to the internet",
  privacy: "change the browser's privacy settings",
  identity: "use sign-in accounts",
  tabCapture: "record a tab",
  desktopCapture: "record your screen",
  pageCapture: "save a page's contents",
};

// Access that lets an extension see everything the owner does in the browser.
const HIGH_RISK_PERMISSIONS = new Set(["debugger", "nativeMessaging", "proxy", "management", "cookies", "history", "webRequestBlocking", "desktopCapture", "tabCapture", "privacy"]);

function isAllSitesPattern(p) {
  // A wildcard on a bare public suffix (*://*.com/*) reaches as much as <all_urls>.
  return p === "<all_urls>" || /^(\*|https?|file|ftp):\/\/\*\/\*$/.test(p) || /^(\*|https?):\/\/\*\/?$/.test(p)
    || /^(\*|https?):\/\/\*\.[a-z0-9-]+\/?\*?$/i.test(p);
}

function hostOfPattern(p) {
  const m = /^(?:\*|https?|file|ftp|wss?):\/\/([^/]*)/.exec(p);
  if (!m) return "";
  return m[1].replace(/^\*\./, "");
}

function str(v, max) {
  return typeof v === "string" ? v.replace(/[\u0000-\u001f\u007f\u061c\u200b-\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069\ufeff]/g, " ").trim().slice(0, max) : "";
}

function strList(v, maxItems, maxLen) {
  return Array.isArray(v) ? v.filter((x) => typeof x === "string" && x).slice(0, maxItems).map((x) => str(x, maxLen)).filter(Boolean) : [];
}

// "__MSG_appName__" is looked up in the default locale's messages file. `readLocale(locale)`
// returns that file's text or null; anything that does not resolve is shown as written.
function resolveMessage(value, manifest, readLocale) {
  const m = /^__MSG_([A-Za-z0-9_@]+)__$/.exec(String(value));
  if (!m || typeof readLocale !== "function") return String(value);
  const locale = typeof manifest.default_locale === "string" && /^[A-Za-z0-9_-]{1,16}$/.test(manifest.default_locale) ? manifest.default_locale : "";
  if (!locale) return String(value);
  try {
    const messages = JSON.parse(readLocale(locale) || "{}");
    const key = Object.keys(messages).find((k) => k.toLowerCase() === m[1].toLowerCase());
    const text = key && messages[key] && typeof messages[key].message === "string" ? messages[key].message : "";
    return text || String(value);
  } catch {
    return String(value);
  }
}

// What a manifest asks for, in a shape the confirmation and the panel can both show.
//   { ok: false, error }   not an extension this code can describe
//   { ok: true, name, version, manifestVersion, permissions: [{ key, words }], hosts: [..],
//     allSites, risk: "high"|"normal", lines: [..] }
function inspectManifest(manifest, readLocale) {
  if (!manifest || typeof manifest !== "object" || Array.isArray(manifest)) return { ok: false, error: "manifest.json is not a JSON object." };
  const mv = manifest.manifest_version;
  if (mv !== 2 && mv !== 3) return { ok: false, error: "manifest_version must be 2 or 3." };
  const name = typeof manifest.name === "string" ? str(resolveMessage(manifest.name, manifest, readLocale), 80) : "";
  const version = str(manifest.version, 40);
  if (!name) return { ok: false, error: "The extension has no name." };
  if (!version) return { ok: false, error: "The extension has no version." };

  const perms = strList(manifest.permissions, 100, 80).filter((p) => !p.includes("://") && p !== "<all_urls>");
  const hostsFromPerms = strList(manifest.permissions, 100, 200).filter((p) => p.includes("://") || p === "<all_urls>");
  const hostPerms = strList(manifest.host_permissions, 100, 200);
  const scriptHosts = [];
  for (const cs of Array.isArray(manifest.content_scripts) ? manifest.content_scripts.slice(0, 50) : []) {
    if (cs && typeof cs === "object") scriptHosts.push(...strList(cs.matches, 50, 200));
  }
  const hosts = [...new Set([...hostPerms, ...hostsFromPerms, ...scriptHosts])].slice(0, 200);
  const allSites = hosts.some(isAllSitesPattern);

  const permissions = [...new Set(perms)].map((key) => ({ key, words: PERMISSION_WORDS[key] || `use the "${key}" permission` }));
  const optional = strList(manifest.optional_permissions, 50, 80);

  const lines = [];
  if (allSites) lines.push("Read and change everything on every website you open in this browser, including what you type into forms. It runs in every profile.");
  else if (hosts.length) {
    const names = [...new Set(hosts.map(hostOfPattern).filter(Boolean))].slice(0, 6);
    lines.push(`Read and change your data on: ${names.length ? names.join(", ") : "the sites it lists"}${hosts.length > names.length ? " and more" : ""}.`);
  }
  for (const p of permissions) lines.push(p.words.charAt(0).toUpperCase() + p.words.slice(1) + ".");
  if (mv === 2) lines.push("This is a Manifest V2 extension, which current Chromium may refuse to run.");
  if (optional.length) lines.push(`It may ask for more later: ${optional.slice(0, 6).join(", ")}.`);

  const risk = allSites || permissions.some((p) => HIGH_RISK_PERMISSIONS.has(p.key)) ? "high" : "normal";
  return { ok: true, name, version, manifestVersion: mv, permissions, hosts, allSites, risk, lines };
}

// The text of the native confirmation. A person decides on this, so it names what the
// extension is and what it can reach, and says in plain words what adding means.
function confirmationText(info, sourceName) {
  const head = `Add "${info.name}" (version ${info.version}) to this browser?`;
  const body = info.lines.length ? info.lines.map((l) => `  - ${l}`).join("\n") : "  - It asks for no special access.";
  const tail = "Its files are copied into Dourmouse's own folder, and only that copy runs. Remove it from the Extensions panel at any time. " +
    "Not every Chrome extension feature works here.";
  return {
    message: head,
    detail: `Folder: ${str(sourceName, 120)}\n\nIt can:\n${body}\n\n${tail}`,
  };
}

// ----------------------------- the approved copy ----------------------------- //

// Copies a folder the owner picked into `dest`, refusing anything that could escape or
// swell: symbolic links, an absurd number of files, depth or size. Returns the manifest's
// bytes as copied so the caller can check them against what the owner approved.
function copyTree({ fs, path }, src, dest) {
  let files = 0;
  let bytes = 0;
  const walk = (from, to, depth) => {
    if (depth > MAX_TREE_DEPTH) throw new Error("the folder is nested too deeply");
    fs.mkdirSync(to, { recursive: true, mode: 0o700 });
    for (const entry of fs.readdirSync(from, { withFileTypes: true })) {
      if (SKIPPED_NAMES.has(entry.name)) continue;
      const a = path.join(from, entry.name);
      const b = path.join(to, entry.name);
      const st = fs.lstatSync(a);
      if (st.isSymbolicLink()) throw new Error(`the folder contains a symbolic link (${entry.name}), which is not copied`);
      if (st.isDirectory()) {
        walk(a, b, depth + 1);
      } else if (st.isFile()) {
        files += 1;
        bytes += st.size;
        if (files > MAX_TREE_FILES) throw new Error(`the folder holds more than ${MAX_TREE_FILES} files`);
        if (bytes > MAX_TREE_BYTES) throw new Error(`the folder is larger than ${MAX_TREE_BYTES / (1024 * 1024)} MB`);
        fs.copyFileSync(a, b);
        fs.chmodSync(b, 0o600);
      }
    }
  };
  try {
    walk(src, dest, 0);
    return { ok: true, files, bytes };
  } catch (exc) {
    return { ok: false, error: String((exc && exc.message) || exc) };
  }
}

// A fingerprint of the whole copy: every path and every file's own hash, in a fixed order.
function hashTree({ fs, path }, dir) {
  const h = crypto.createHash("sha256");
  const walk = (d, rel) => {
    const names = fs.readdirSync(d).sort();
    for (const n of names) {
      const p = path.join(d, n);
      const st = fs.lstatSync(p);
      const r = rel ? `${rel}/${n}` : n;
      if (st.isSymbolicLink()) throw new Error("a symbolic link appeared in the copy");
      if (st.isDirectory()) {
        h.update(`D:${r}\n`);
        walk(p, r);
      } else if (st.isFile()) {
        h.update(`F:${r}:${crypto.createHash("sha256").update(fs.readFileSync(p)).digest("hex")}\n`);
      }
    }
  };
  walk(dir, "");
  return h.digest("hex");
}

function sha256(buf) {
  return crypto.createHash("sha256").update(buf).digest("hex");
}

// ------------------------------- the stored list ------------------------------- //
// { entries: [{ id, name, version, enabled, addedAt, tree, risk, summary: [..] }] }

function sanitizeRegistry(raw) {
  const src = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
  const entries = [];
  for (const e of Array.isArray(src.entries) ? src.entries : []) {
    if (!e || typeof e !== "object" || typeof e.id !== "string" || !/^[a-f0-9]{16}$/.test(e.id)) continue;
    if (entries.some((x) => x.id === e.id) || entries.length >= MAX_EXTENSIONS) continue;
    if (typeof e.tree !== "string" || !/^[a-f0-9]{64}$/.test(e.tree)) continue;
    entries.push({
      id: e.id,
      name: str(e.name, 80) || "(unnamed)",
      version: str(e.version, 40),
      enabled: e.enabled === true,
      addedAt: Number(e.addedAt) || 0,
      tree: e.tree,
      risk: e.risk === "high" ? "high" : "normal",
      summary: strList(e.summary, 20, 300),
    });
  }
  return { entries };
}

function newExtensionId(randomBytes) {
  return randomBytes(8).toString("hex");
}

function addEntry(registry, entry) {
  if (registry.entries.length >= MAX_EXTENSIONS) return { registry, ok: false, error: `At most ${MAX_EXTENSIONS} extensions.` };
  return { registry: { entries: [...registry.entries, entry] }, ok: true };
}

function setEnabled(registry, id, enabled) {
  if (!registry.entries.some((e) => e.id === id)) return { registry, ok: false, error: "No such extension." };
  return { registry: { entries: registry.entries.map((e) => (e.id === id ? { ...e, enabled: enabled === true } : e)) }, ok: true };
}

function removeEntry(registry, id) {
  if (!registry.entries.some((e) => e.id === id)) return { registry, ok: false, error: "No such extension." };
  return { registry: { entries: registry.entries.filter((e) => e.id !== id) }, ok: true };
}

// What the console and the bridge may see: names, versions, state, a summary of what it can
// reach. Never a path, never the fingerprint.
function publicView(entry, status) {
  const s = status && typeof status === "object" ? status : {};
  return {
    id: entry.id, name: entry.name, version: entry.version, enabled: entry.enabled, addedAt: entry.addedAt, risk: entry.risk,
    summary: entry.summary, loaded: s.loaded === true, error: str(s.error, 300),
  };
}

function countEntries(registry) {
  return { total: registry.entries.length, enabled: registry.entries.filter((e) => e.enabled).length };
}

module.exports = {
  MAX_EXTENSIONS, MAX_MANIFEST_BYTES, MAX_TREE_BYTES, MAX_TREE_FILES, EXTENSION_SUPPORT_NOTE, PERMISSION_WORDS,
  isAllSitesPattern, resolveMessage, inspectManifest, confirmationText, copyTree, hashTree, sha256,
  sanitizeRegistry, newExtensionId, addEntry, setEnabled, removeEntry, publicView, countEntries,
};
