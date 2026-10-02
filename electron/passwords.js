// Dourmouse native shell -- saved passwords and address autofill for the browser pane (Phase B2).
//
// CommonJS with no Electron imports. main.js hands in the two things that need Electron:
// `crypto` (Electron's safeStorage: the key lives in the macOS Keychain) and `store` (a small
// JSON file in the userData folder). Because both are injected, plain node tests every rule
// here with a fake cipher, and the same code runs in the app.
//
// The promises this file keeps:
//   * A password is stored only as ciphertext (safeStorage). If encryption is not available it
//     is NOT stored at all: there is no plaintext fallback.
//   * Nothing here logs, and nothing returns a password except `credentials()` and `reveal()`,
//     which main.js calls only from console-only IPC handlers (after a native confirmation for
//     reveal). The list calls return usernames and sites, never passwords.
//   * A login is tied to an ORIGIN (scheme, host, port). https only, plus plain http on the
//     machine itself (a site under development). A saved login never matches another origin.

const MAX_ENTRIES = 2000;
const MAX_USERNAME = 256;
const MAX_PASSWORD = 1024;
const MAX_NEVER = 1000;

function webOrigin(url) {
  try {
    const u = new URL(String(url));
    if (u.protocol !== "http:" && u.protocol !== "https:") return "";
    return u.origin.toLowerCase();
  } catch {
    return "";
  }
}

function isLoopbackHost(hostname) {
  const h = String(hostname).toLowerCase();
  return h === "localhost" || h === "127.0.0.1" || h === "[::1]" || h.endsWith(".localhost");
}

// https, or plain http on this machine only. A login sent over plain http to a real host
// is not stored or filled: anyone on the network could read it.
function savableOrigin(origin) {
  const o = webOrigin(origin);
  if (!o || o !== String(origin)) return false;
  const u = new URL(o);
  return u.protocol === "https:" || isLoopbackHost(u.hostname);
}

function cleanUsername(v) {
  return String(v === undefined || v === null ? "" : v).replace(/[\u0000-\u001f\u007f\u200b-\u200f\u202a-\u202e\u2066-\u2069]/g, "").trim().slice(0, MAX_USERNAME);
}

function sameUsername(a, b) {
  return cleanUsername(a).toLowerCase() === cleanUsername(b).toLowerCase();
}

// ------------------------------- the vault ------------------------------- //

function createVault({ store, crypto, now = Date.now, newId }) {
  let version = 0;
  const data = () => {
    const d = store.get();
    if (!Array.isArray(d.entries)) d.entries = [];
    if (!Array.isArray(d.never)) d.never = [];
    return d;
  };
  const write = (d) => {
    version += 1;
    store.set({ entries: d.entries, never: d.never });
  };
  const open = (entry) => {
    try {
      const raw = JSON.parse(crypto.decrypt(entry.blob));
      return raw && typeof raw === "object" ? { username: cleanUsername(raw.u), password: String(raw.p === undefined ? "" : raw.p) } : null;
    } catch {
      return null; // the key changed or the file was edited: the entry stays, unreadable, and can be deleted
    }
  };
  const seal = (username, password) => crypto.encrypt(JSON.stringify({ u: username, p: password }));
  const pub = (e) => {
    const c = open(e);
    return { id: e.id, origin: e.origin, username: c ? c.username : "", readable: Boolean(c), created: e.created, updated: e.updated, lastUsed: e.lastUsed || 0 };
  };

  return {
    version: () => version,
    available: () => Boolean(crypto.available()),

    // What a submitted login would do, without writing anything:
    //   unavailable | refused | never | new | update | same
    classify({ origin, username, password }) {
      // Cheap refusals first: a site or password that can never be saved must not even ask
      // the secure storage whether it is available (that is a Keychain access).
      const pw = String(password === undefined || password === null ? "" : password);
      if (!savableOrigin(origin) || !pw || pw.length > MAX_PASSWORD) return { status: "refused" };
      if (data().never.includes(origin)) return { status: "never" };
      if (!crypto.available()) return { status: "unavailable" };
      const user = cleanUsername(username);
      const known = data().entries.find((e) => e.origin === origin && (() => {
        const c = open(e);
        return c ? sameUsername(c.username, user) : false;
      })());
      if (!known) return { status: "new" };
      const c = open(known);
      return { status: c && c.password === pw ? "same" : "update", id: known.id };
    },

    save({ origin, username, password }) {
      const verdict = this.classify({ origin, username, password });
      if (verdict.status === "unavailable") return { ok: false, error: "encryption is not available, so nothing is saved" };
      if (verdict.status === "refused") return { ok: false, error: "this site or password cannot be saved" };
      if (verdict.status === "never") return { ok: false, error: "saving is turned off for this site" };
      if (verdict.status === "same") return { ok: true, status: "same", id: verdict.id };
      const user = cleanUsername(username);
      const d = data();
      const t = now();
      if (verdict.status === "update") {
        const entries = d.entries.map((e) => (e.id === verdict.id ? { ...e, blob: seal(user, String(password)), updated: t } : e));
        write({ ...d, entries });
        return { ok: true, status: "updated", id: verdict.id };
      }
      if (d.entries.length >= MAX_ENTRIES) return { ok: false, error: "the password list is full" };
      const entry = { id: newId(), origin, blob: seal(user, String(password)), created: t, updated: t, lastUsed: 0 };
      write({ ...d, entries: [...d.entries, entry] });
      return { ok: true, status: "added", id: entry.id };
    },

    // For the list in the console: sites and usernames, never a password.
    list() {
      return data().entries.map(pub).sort((a, b) => a.origin.localeCompare(b.origin) || a.username.localeCompare(b.username));
    },

    // For the Fill bar: the logins saved for exactly this origin.
    entriesFor(origin) {
      if (!savableOrigin(origin)) return [];
      return data().entries.filter((e) => e.origin === origin).map(pub).filter((e) => e.readable).map((e) => ({ id: e.id, username: e.username }));
    },

    // The one way a password leaves the vault toward a page: main.js calls this inside a
    // console-only handler, after checking the page's origin is the entry's origin.
    credentials(id) {
      const e = data().entries.find((x) => x.id === id);
      if (!e) return null;
      const c = open(e);
      return c ? { origin: e.origin, username: c.username, password: c.password } : null;
    },

    touch(id) {
      const d = data();
      write({ ...d, entries: d.entries.map((e) => (e.id === id ? { ...e, lastUsed: now() } : e)) });
    },

    remove(id) {
      const d = data();
      const entries = d.entries.filter((e) => e.id !== id);
      if (entries.length === d.entries.length) return false;
      write({ ...d, entries });
      return true;
    },

    never() {
      return data().never.slice().sort();
    },
    isNever(origin) {
      return data().never.includes(origin);
    },
    addNever(origin) {
      if (!savableOrigin(origin)) return false;
      const d = data();
      if (d.never.includes(origin)) return true;
      if (d.never.length >= MAX_NEVER) return false;
      write({ ...d, never: [...d.never, origin] });
      return true;
    },
    removeNever(origin) {
      const d = data();
      if (!d.never.includes(origin)) return false;
      write({ ...d, never: d.never.filter((o) => o !== origin) });
      return true;
    },

    counts() {
      const d = data();
      return { passwords: d.entries.length, neverSaved: d.never.length };
    },
  };
}

// ------------------------------ address book ------------------------------ //
// Names, street addresses and phone numbers the owner typed in on purpose. Stored the same
// way (one ciphertext blob per profile), filled only on a click, into the top page only.

const ADDRESS_FIELDS = ["label", "name", "email", "phone", "line1", "line2", "city", "state", "zip", "country"];
const MAX_PROFILES = 5;
const MAX_FIELD = 200;

function cleanProfile(raw) {
  const out = {};
  const src = raw && typeof raw === "object" ? raw : {};
  for (const f of ADDRESS_FIELDS) {
    out[f] = String(src[f] === undefined || src[f] === null ? "" : src[f]).replace(/[\u0000-\u001f\u007f]/g, " ").trim().slice(0, MAX_FIELD);
  }
  return out;
}

function createAddressBook({ store, crypto, now = Date.now, newId }) {
  const data = () => {
    const d = store.get();
    if (!Array.isArray(d.profiles)) d.profiles = [];
    return d;
  };
  const open = (p) => {
    try {
      const raw = JSON.parse(crypto.decrypt(p.blob));
      return cleanProfile(raw);
    } catch {
      return null;
    }
  };
  return {
    available: () => Boolean(crypto.available()),
    list() {
      return data().profiles.map((p) => {
        const f = open(p);
        return { id: p.id, readable: Boolean(f), updated: p.updated, ...(f || cleanProfile({})) };
      });
    },
    get(id) {
      const p = data().profiles.find((x) => x.id === id);
      return p ? open(p) : null;
    },
    save(raw, id) {
      if (!crypto.available()) return { ok: false, error: "encryption is not available, so nothing is saved" };
      const fields = cleanProfile(raw);
      if (!ADDRESS_FIELDS.some((f) => f !== "label" && fields[f])) return { ok: false, error: "type at least one detail" };
      if (!fields.label) fields.label = fields.name || fields.line1 || "Address";
      const d = data();
      const blob = crypto.encrypt(JSON.stringify(fields));
      if (id) {
        if (!d.profiles.some((p) => p.id === id)) return { ok: false, error: "no such address" };
        store.set({ profiles: d.profiles.map((p) => (p.id === id ? { ...p, blob, updated: now() } : p)) });
        return { ok: true, id };
      }
      if (d.profiles.length >= MAX_PROFILES) return { ok: false, error: `at most ${MAX_PROFILES} addresses` };
      const entry = { id: newId(), blob, updated: now() };
      store.set({ profiles: [...d.profiles, entry] });
      return { ok: true, id: entry.id };
    },
    remove(id) {
      const d = data();
      const profiles = d.profiles.filter((p) => p.id !== id);
      if (profiles.length === d.profiles.length) return false;
      store.set({ profiles });
      return true;
    },
    count: () => data().profiles.length,
  };
}

module.exports = {
  MAX_ENTRIES, MAX_USERNAME, MAX_PASSWORD, ADDRESS_FIELDS, MAX_PROFILES,
  webOrigin, isLoopbackHost, savableOrigin, cleanUsername, cleanProfile, createVault, createAddressBook,
};
