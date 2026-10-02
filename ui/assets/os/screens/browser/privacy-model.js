/* BROWSER (Phase B2): pure helpers for site permissions, saved passwords and address
   autofill. No DOM at import time, so node can test them.

   Everything here reads what the Electron shell sent over its console-only channel and makes
   it safe to draw. None of it ever holds a password: the shell keeps those, and the one call
   that returns one (reveal) is handled in privacy-ui.js and goes through a native dialog. */

/* Click-ambush guard (B3, from the B2 review). A bar that appears under the pointer a moment
   before a click meant for the page would take that click as an answer. Its buttons stay
   disabled until this long after the prompt FIRST appeared. */
export const CLICK_DELAY_MS = 600;

/* How many milliseconds a prompt first seen at `firstSeen` must still wait. Never negative, and
   a clock that went backwards (firstSeen later than now) is treated as "just appeared". */
export function clickDelayLeft(firstSeen, now, delay = CLICK_DELAY_MS) {
  if (!Number.isFinite(firstSeen) || !Number.isFinite(now)) return delay;
  const waited = Math.max(0, now - firstSeen);
  return Math.max(0, delay - waited);
}

export const ADDRESS_FIELDS = [
  { key: 'label', label: 'Label', hint: 'Home, Work' },
  { key: 'name', label: 'Full name', hint: '' },
  { key: 'email', label: 'Email', hint: '' },
  { key: 'phone', label: 'Phone', hint: '' },
  { key: 'line1', label: 'Street address', hint: '' },
  { key: 'line2', label: 'Apartment, suite', hint: '' },
  { key: 'city', label: 'City', hint: '' },
  { key: 'state', label: 'State or region', hint: '' },
  { key: 'zip', label: 'Postal code', hint: '' },
  { key: 'country', label: 'Country', hint: '' },
];

const PERMISSION_KEYS = ['camera', 'microphone', 'geolocation', 'notifications', 'clipboard-read', 'fullscreen'];

function str(v, max) {
  return typeof v === 'string' ? v.slice(0, max) : '';
}

function count(v) {
  return Number.isInteger(v) && v >= 0 ? v : 0;
}

/* The shell's privacy state, with anything odd made safe. A field the shell did not send is
   absent, never invented. */
export function privacyModel(raw) {
  const s = raw && typeof raw === 'object' ? raw : {};
  const perm = s.perm && typeof s.perm === 'object' && typeof s.perm.id === 'string'
    ? {
        id: s.perm.id,
        origin: str(s.perm.origin, 300),
        host: str(s.perm.host, 200),
        keys: Array.isArray(s.perm.keys) ? s.perm.keys.filter((k) => PERMISSION_KEYS.includes(k)) : [],
        text: str(s.perm.text, 300),
      }
    : null;
  const save = s.save && typeof s.save === 'object' && typeof s.save.id === 'string'
    ? { id: s.save.id, origin: str(s.save.origin, 300), host: str(s.save.host, 200), username: str(s.save.username, 256), update: s.save.update === true }
    : null;
  const fill = s.fill && typeof s.fill === 'object' && Array.isArray(s.fill.entries)
    ? {
        origin: str(s.fill.origin, 300),
        host: str(s.fill.host, 200),
        entries: s.fill.entries.filter((e) => e && typeof e.id === 'string').slice(0, 50).map((e) => ({ id: e.id, username: str(e.username, 256) })),
      }
    : null;
  const fillAddress = s.fillAddress && typeof s.fillAddress === 'object' && Array.isArray(s.fillAddress.profiles)
    ? { profiles: s.fillAddress.profiles.filter((p) => p && typeof p.id === 'string').slice(0, 10).map((p) => ({ id: p.id, label: str(p.label, 200) || 'Address' })) }
    : null;
  const vault = s.vault && typeof s.vault === 'object' ? s.vault : {};
  const sites = s.sites && typeof s.sites === 'object' ? s.sites : {};
  return {
    perm,
    pendingPerms: count(s.pendingPerms),
    save,
    fill: fill && fill.entries.length ? fill : null,
    fillAddress: fillAddress && fillAddress.profiles.length ? fillAddress : null,
    notice: str(s.notice, 400),
    vault: { available: vault.available === true ? true : vault.available === false ? false : null, passwords: count(vault.passwords), neverSaved: count(vault.neverSaved) },
    addresses: count(s.addresses),
    sites: { origins: count(sites.origins), decisions: count(sites.decisions) },
  };
}

/* One string that changes when anything the bars draw changes, so they are rebuilt only then
   (and a button the owner is about to press is not replaced under the pointer). */
export function barsKey(m, noticeHidden) {
  return [
    m.perm ? [m.perm.id, m.perm.text].join(':') : '-',
    m.pendingPerms,
    m.save ? [m.save.id, m.save.username, m.save.update ? 1 : 0].join(':') : '-',
    m.fill ? m.fill.entries.map((e) => e.id + e.username).join(',') + m.fill.host : '-',
    m.fillAddress ? m.fillAddress.profiles.map((p) => p.id + p.label).join(',') : '-',
    m.notice && !noticeHidden ? m.notice : '-',
  ].join('|');
}

/* "2 more waiting" beside the prompt, when other tabs also asked. */
export function waitingLine(m) {
  const others = Math.max(0, m.pendingPerms - (m.perm ? 1 : 0));
  if (!others) return '';
  return others === 1 ? '1 more request is waiting in another tab' : others + ' more requests are waiting in other tabs';
}

export function saveLine(save) {
  const who = save.username ? ' for ' + save.username : '';
  return (save.update ? 'Update the password' : 'Save the password') + who + ' on ' + save.host + '?';
}

const DECISION_WORDS = { allow: 'Allow', block: 'Block' };

/* The stored decisions grouped by site, for the Site settings list. */
export function groupSites(rows) {
  const groups = [];
  const by = new Map();
  for (const r of Array.isArray(rows) ? rows : []) {
    if (!r || typeof r.origin !== 'string' || !PERMISSION_KEYS.includes(r.permission) || !DECISION_WORDS[r.decision]) continue;
    let g = by.get(r.origin);
    if (!g) {
      let host = r.origin;
      try {
        host = new URL(r.origin).host;
      } catch (err) {
        /* shown as it is */
      }
      g = { origin: r.origin.slice(0, 300), host: host.slice(0, 200), items: [] };
      by.set(r.origin, g);
      groups.push(g);
    }
    g.items.push({ permission: r.permission, label: str(r.label, 60) || r.permission, decision: r.decision, at: Number.isFinite(r.at) ? r.at : 0 });
  }
  return groups;
}

/* The saved logins as the shell lists them (usernames and sites; never a password). */
export function passwordRows(raw) {
  return (Array.isArray(raw) ? raw : [])
    .filter((e) => e && typeof e.id === 'string' && typeof e.origin === 'string')
    .slice(0, 2000)
    .map((e) => {
      let host = e.origin;
      try {
        host = new URL(e.origin).host;
      } catch (err) {
        /* shown as it is */
      }
      return {
        id: e.id,
        origin: e.origin.slice(0, 300),
        host: host.slice(0, 200),
        username: str(e.username, 256),
        readable: e.readable !== false,
        updated: Number.isFinite(e.updated) ? e.updated : 0,
        lastUsed: Number.isFinite(e.lastUsed) ? e.lastUsed : 0,
      };
    });
}

export function neverRows(raw) {
  return (Array.isArray(raw) ? raw : []).filter((o) => typeof o === 'string').slice(0, 1000).map((o) => o.slice(0, 300));
}

export function profileRows(raw) {
  return (Array.isArray(raw) ? raw : [])
    .filter((p) => p && typeof p.id === 'string')
    .slice(0, 10)
    .map((p) => {
      const out = { id: p.id, readable: p.readable !== false };
      for (const f of ADDRESS_FIELDS) out[f.key] = str(p[f.key], 200);
      return out;
    });
}

/* What the address form holds, or why it cannot be saved. At least one real detail is needed. */
export function addressFromForm(values) {
  const out = {};
  for (const f of ADDRESS_FIELDS) out[f.key] = str(values && values[f.key], 200).trim();
  if (!ADDRESS_FIELDS.some((f) => f.key !== 'label' && out[f.key])) return { ok: false, reason: 'Type at least one detail, such as a name or a street address.' };
  return { ok: true, profile: out };
}

/* The sentence above the saved-passwords list. */
export function vaultLine(vault) {
  if (!vault.available) return 'This Mac\'s secure storage is not available, so no password can be saved. Nothing is ever stored without encryption.';
  return 'Encrypted with a key in this Mac\'s Keychain. Saved passwords stay on this Mac: they are never sent to the server, the AI or the cloud, and no tool of the AI can list or open them. Once you press Fill, though, the password sits in that page\'s field, and the browser agent can read field values on a page it is attached to.';
}

/* A host or site for a sort key or label. */
export function siteHost(origin) {
  try {
    return new URL(origin).host;
  } catch (err) {
    return String(origin || '');
  }
}
