// Dourmouse native shell -- import from Chrome (Phase B3).
//
// CommonJS with no Electron imports, like policy.js, so plain node can test every rule.
// main.js owns the native dialogs and the stores; this file owns the reading and the
// merging. Everything here is READ-ONLY toward Chrome and explicit:
//
//   * Bookmarks and history come from a Chrome profile folder the owner picked in a native
//     dialog. Exactly two files in it are ever opened, "Bookmarks" and "History", and only
//     as regular files (a symbolic link is refused). History is a copy in a private temp
//     folder, read with SQLite in read-only mode, and the copy is deleted afterwards.
//   * Passwords come only from a CSV the owner exported from Chrome and picked in a native
//     dialog. Chrome's own password database ("Login Data") and its Keychain key ("Chrome
//     Safe Storage") are never read, and neither are cookies or any other file in the folder.
//   * The CSV is read into memory, saved into the encrypted vault, and never copied. Nothing
//     here logs a value, and the counts returned never include one.

const passwords = require("./passwords");
const policy = require("./policy");

const MAX_BOOKMARKS_FILE = 20 * 1024 * 1024;
const MAX_HISTORY_FILE = 600 * 1024 * 1024;
const MAX_CSV_BYTES = 5 * 1024 * 1024;
const MAX_CSV_ROWS = 10000;
const MAX_FIELD = 4096;
const MAX_IMPORT_BOOKMARKS = 5000;
const MAX_IMPORT_HISTORY = 5000;
const WEBKIT_EPOCH_MS = 11644473600000; // 1601-01-01 to 1970-01-01, in milliseconds

// Chrome stores times as microseconds since 1601. 0 and nonsense become 0 (unknown).
function chromeTimeToMs(micros) {
  const n = Number(micros);
  if (!Number.isFinite(n) || n <= 0) return 0;
  const ms = Math.round(n / 1000 - WEBKIT_EPOCH_MS);
  return ms > 0 && ms < 4102444800000 ? ms : 0;
}

// ------------------------------- bookmarks ------------------------------- //

// Chrome's "Bookmarks" file: { roots: { bookmark_bar, other, synced, ... } } with nested
// folders. Flattened, web pages only, in the order Chrome shows them.
function parseChromeBookmarks(text) {
  let doc;
  try {
    doc = JSON.parse(String(text));
  } catch {
    return { ok: false, error: "The Bookmarks file is not valid JSON.", items: [] };
  }
  if (!doc || typeof doc !== "object" || !doc.roots || typeof doc.roots !== "object") {
    return { ok: false, error: "That does not look like a Chrome Bookmarks file.", items: [] };
  }
  const items = [];
  let seen = 0;
  const walk = (node, depth) => {
    if (!node || typeof node !== "object" || depth > 20 || seen >= 20000) return;
    seen += 1;
    if (node.type === "url" && typeof node.url === "string") {
      items.push({ url: node.url, title: typeof node.name === "string" ? node.name : "", at: chromeTimeToMs(node.date_added) });
    }
    if (Array.isArray(node.children)) for (const c of node.children) walk(c, depth + 1);
  };
  for (const key of Object.keys(doc.roots)) walk(doc.roots[key], 0);
  return { ok: true, items };
}

// Folds imported bookmarks into the list. Addresses that are not real web pages, and ones
// already there, are counted, not added.
function mergeBookmarks(existing, incoming, newId, now = Date.now) {
  let list = existing;
  const counts = { found: incoming.length, added: 0, existing: 0, skipped: 0, full: 0 };
  for (const b of incoming.slice(0, MAX_IMPORT_BOOKMARKS)) {
    if (!policy.paneUrlAllowed(b.url) || b.url.length > 2048) {
      counts.skipped += 1;
      continue;
    }
    if (list.some((x) => x.url === b.url)) {
      counts.existing += 1;
      continue;
    }
    const r = policy.addBookmark(list, { id: newId(), url: b.url, title: b.title, at: b.at || now() });
    if (r.added) {
      list = r.list;
      counts.added += 1;
    } else {
      counts.full += 1;
    }
  }
  counts.skipped += Math.max(0, incoming.length - MAX_IMPORT_BOOKMARKS);
  return { list, counts };
}

// ------------------------------- history ------------------------------- //

// One entry per address (the newest visit), merged with what is already there, newest first,
// cut at the history cap. Chrome's own count of visits is not kept: this browser's list is a
// list of visits, and an imported page is one visit.
function mergeHistory(existing, incoming, newId, now = Date.now) {
  const counts = { found: incoming.length, added: 0, existing: 0, skipped: 0, dropped: 0 };
  const have = new Set(existing.map((e) => e.url));
  const fresh = [];
  for (const h of incoming.slice(0, MAX_IMPORT_HISTORY)) {
    if (!policy.paneUrlAllowed(h.url) || h.url.length > 2048) {
      counts.skipped += 1;
      continue;
    }
    if (have.has(h.url)) {
      counts.existing += 1;
      continue;
    }
    have.add(h.url);
    fresh.push({ id: newId(), url: h.url, title: policy.cleanTitle(h.title), at: Number(h.at) || now() });
  }
  counts.skipped += Math.max(0, incoming.length - MAX_IMPORT_HISTORY);
  const all = [...existing, ...fresh].sort((a, b) => b.at - a.at);
  const kept = all.slice(0, policy.HISTORY_CAP);
  counts.dropped = all.length - kept.length;
  const freshSet = new Set(fresh);
  counts.added = kept.filter((e) => freshSet.has(e)).length;
  return { list: kept, counts };
}

// ------------------------- Chrome profile folder ------------------------- //

// deps: { fs, path, os, readHistory(copyPath) -> [{ url, title, ms }] }
// `want`: { bookmarks, history }. Returns what was found, never writes to Chrome's folder.
function readChromeProfile(deps, dir, want = { bookmarks: true, history: true }) {
  const { fs, path, os } = deps;
  const out = { ok: false, error: "", bookmarks: null, history: null, notes: [] };
  let st;
  try {
    st = fs.lstatSync(dir);
  } catch {
    out.error = "That folder cannot be read.";
    return out;
  }
  if (!st.isDirectory()) {
    out.error = "Pick the Chrome profile folder itself (for example Default), not a file.";
    return out;
  }
  // Only these two names are ever built into a path. Nothing else in the folder is opened.
  const regular = (name, cap) => {
    const p = path.join(dir, name);
    let s;
    try {
      s = fs.lstatSync(p);
    } catch {
      return { present: false };
    }
    if (s.isSymbolicLink() || !s.isFile()) return { present: true, refused: `${name} is not a regular file, so it was not read.` };
    if (s.size > cap) return { present: true, refused: `${name} is larger than ${Math.round(cap / (1024 * 1024))} MB, so it was not read.` };
    return { present: true, path: p };
  };

  if (want.bookmarks) {
    const f = regular("Bookmarks", MAX_BOOKMARKS_FILE);
    if (!f.present) out.notes.push("No Bookmarks file in that folder.");
    else if (f.refused) out.notes.push(f.refused);
    else {
      const parsed = parseChromeBookmarks(fs.readFileSync(f.path, "utf8"));
      if (parsed.ok) out.bookmarks = parsed.items;
      else out.notes.push(parsed.error);
    }
  }
  if (want.history) {
    const f = regular("History", MAX_HISTORY_FILE);
    if (!f.present) out.notes.push("No History file in that folder.");
    else if (f.refused) out.notes.push(f.refused);
    else {
      let tmp = "";
      try {
        tmp = fs.mkdtempSync(path.join(os.tmpdir(), "dm-import-"));
        fs.chmodSync(tmp, 0o700);
        const copy = path.join(tmp, "History");
        fs.copyFileSync(f.path, copy);
        const rows = deps.readHistory(copy);
        out.history = rows
          .filter((r) => r && typeof r.url === "string")
          .map((r) => ({ url: r.url, title: typeof r.title === "string" ? r.title : "", at: Number(r.ms) > 0 ? Number(r.ms) : 0 }));
      } catch (exc) {
        out.notes.push(`The history could not be read (${String((exc && exc.message) || exc).slice(0, 160)}). If Chrome is open, quit it and try again.`);
      } finally {
        if (tmp) {
          try {
            fs.rmSync(tmp, { recursive: true, force: true });
          } catch {
            /* a temp folder that cannot be removed is reported by the OS cleaner, not here */
          }
        }
      }
    }
  }
  out.ok = out.bookmarks !== null || out.history !== null;
  if (!out.ok && !out.error) out.error = out.notes.join(" ") || "Nothing to import was found in that folder.";
  return out;
}

const HISTORY_SQL =
  "SELECT url, title, ((last_visit_time / 1000) - " + WEBKIT_EPOCH_MS + ") AS ms FROM urls WHERE hidden = 0 AND last_visit_time > 0 ORDER BY last_visit_time DESC LIMIT " + MAX_IMPORT_HISTORY;

// The reader main.js uses: node's own SQLite when it is there, the sqlite3 command line tool
// that ships with macOS otherwise. Both open the temp COPY read-only. The time is converted
// inside SQL: Chrome's microsecond value is larger than a JavaScript number holds exactly.
function makeSqliteReader({ execFileSync, requireModule }) {
  return function readHistory(copyPath) {
    let sqlite = null;
    try {
      sqlite = requireModule("node:sqlite");
    } catch {
      sqlite = null;
    }
    if (sqlite && sqlite.DatabaseSync) {
      const db = new sqlite.DatabaseSync(copyPath, { readOnly: true });
      try {
        return db.prepare(HISTORY_SQL).all().map((r) => ({ url: r.url, title: r.title, ms: Number(r.ms) }));
      } finally {
        db.close();
      }
    }
    const text = execFileSync("/usr/bin/sqlite3", ["-readonly", "-json", copyPath, HISTORY_SQL], { timeout: 20000, maxBuffer: 64 * 1024 * 1024, encoding: "utf8" });
    const rows = JSON.parse(text || "[]");
    return rows.map((r) => ({ url: r.url, title: r.title, ms: Number(r.ms) }));
  };
}

// ------------------------------ password CSV ------------------------------ //

// RFC 4180: quoted fields, "" for a quote, commas and line breaks inside quotes. Bounded.
function parseCsv(text) {
  let s = String(text);
  if (s.charCodeAt(0) === 0xfeff) s = s.slice(1);
  const rows = [];
  let row = [];
  let field = "";
  let quoted = false;
  let tooLong = false;
  const push = () => {
    row.push(field.length > MAX_FIELD ? (tooLong = true, field.slice(0, MAX_FIELD)) : field);
    field = "";
  };
  for (let i = 0; i < s.length; i += 1) {
    const c = s[i];
    if (quoted) {
      if (c === '"') {
        if (s[i + 1] === '"') {
          field += '"';
          i += 1;
        } else quoted = false;
      } else field += c;
    } else if (c === '"' && field === "") quoted = true;
    else if (c === ",") push();
    else if (c === "\n" || c === "\r") {
      if (c === "\r" && s[i + 1] === "\n") i += 1;
      push();
      if (row.length > 1 || row[0] !== "") rows.push(row);
      row = [];
      if (rows.length > MAX_CSV_ROWS + 1) return { rows: rows.slice(0, MAX_CSV_ROWS + 1), truncated: true, tooLong };
    } else field += c;
  }
  if (field !== "" || row.length) {
    push();
    if (row.length > 1 || row[0] !== "") rows.push(row);
  }
  return { rows, truncated: false, tooLong };
}

// A Chrome export is name,url,username,password,note (other browsers add or drop columns, so
// columns are found by their header, not their position). Returns the logins that could be
// saved and counts of the ones that could not, with no value anywhere in the counts.
function passwordRowsFromCsv(text) {
  const parsed = parseCsv(text);
  if (parsed.rows.length < 2) return { ok: false, error: "That file has no password rows.", rows: [], counts: {} };
  const header = parsed.rows[0].map((h) => String(h).trim().toLowerCase());
  const col = (...names) => header.findIndex((h) => names.includes(h));
  const iUrl = col("url", "login_uri", "origin", "website");
  const iUser = col("username", "login_username", "user");
  const iPass = col("password", "login_password");
  if (iUrl < 0 || iPass < 0) return { ok: false, error: "That does not look like a Chrome password export: it needs url and password columns.", rows: [], counts: {} };
  const counts = { rows: parsed.rows.length - 1, usable: 0, notWeb: 0, notSecure: 0, noPassword: 0, tooLong: 0, truncated: parsed.truncated };
  const rows = [];
  for (const r of parsed.rows.slice(1, MAX_CSV_ROWS + 1)) {
    const origin = passwords.webOrigin(r[iUrl]);
    if (!origin) {
      counts.notWeb += 1;
      continue;
    }
    const pw = r[iPass] === undefined ? "" : r[iPass];
    if (!pw) {
      counts.noPassword += 1;
      continue;
    }
    if (pw.length > passwords.MAX_PASSWORD) {
      counts.tooLong += 1;
      continue;
    }
    if (!passwords.savableOrigin(origin)) {
      counts.notSecure += 1;
      continue;
    }
    rows.push({ origin, username: iUser >= 0 ? r[iUser] || "" : "", password: pw });
  }
  counts.usable = rows.length;
  return { ok: true, rows, counts };
}

// Saves the rows into the vault one by one. The vault decides added, updated or same; a
// vault without encryption refuses everything and the loop stops at the first refusal.
// The result carries counts only.
function importPasswords(vault, rows) {
  const out = { added: 0, updated: 0, same: 0, refused: 0, never: 0, full: 0, unavailable: false };
  for (const r of rows) {
    const res = vault.save({ origin: r.origin, username: r.username, password: r.password });
    if (res.ok) {
      if (res.status === "added") out.added += 1;
      else if (res.status === "updated") out.updated += 1;
      else out.same += 1;
    } else if (/encryption is not available/.test(res.error || "")) {
      out.unavailable = true;
      break;
    } else if (/turned off/.test(res.error || "")) out.never += 1;
    else if (/full/.test(res.error || "")) {
      out.full += 1;
      break;
    } else out.refused += 1;
  }
  return out;
}

module.exports = {
  MAX_CSV_BYTES, MAX_CSV_ROWS, MAX_IMPORT_BOOKMARKS, MAX_IMPORT_HISTORY, WEBKIT_EPOCH_MS, HISTORY_SQL,
  chromeTimeToMs, parseChromeBookmarks, mergeBookmarks, mergeHistory, readChromeProfile, makeSqliteReader,
  parseCsv, passwordRowsFromCsv, importPasswords,
};
