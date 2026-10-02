/* BROWSER (Phase B3): pure helpers for extensions, profiles, import from Chrome and the DRM line.
   No DOM at import time, so node can test them.

   Everything here reads what the Electron shell sent over its console-only channel and makes it
   safe to draw. None of it ever holds a password or a path: the shell keeps those, and the
   folders and files an import reads are chosen in a NATIVE dialog, never typed here. */

const PROFILE_NAME = /^[a-z0-9][a-z0-9_-]{0,23}$/;

function str(v, max) {
  return typeof v === 'string' ? v.slice(0, max) : '';
}

function count(v) {
  return Number.isInteger(v) && v >= 0 ? v : 0;
}

/* ---------------- extensions ---------------- */

export function extensionRows(raw) {
  const list = raw && Array.isArray(raw.extensions) ? raw.extensions : [];
  return list
    .filter((e) => e && typeof e === 'object' && typeof e.id === 'string')
    .slice(0, 20)
    .map((e) => ({
      id: e.id,
      name: str(e.name, 80) || '(unnamed)',
      version: str(e.version, 40),
      enabled: e.enabled === true,
      loaded: e.loaded === true,
      risk: e.risk === 'high' ? 'high' : 'normal',
      summary: Array.isArray(e.summary) ? e.summary.filter((l) => typeof l === 'string').slice(0, 20).map((l) => l.slice(0, 300)) : [],
      error: str(e.error, 300),
      addedAt: Number.isFinite(e.addedAt) ? e.addedAt : 0,
    }));
}

/* The one line that says what state an extension is in. */
export function extensionState(row) {
  if (!row.enabled) return 'Off';
  if (row.error) return 'On, but not running';
  if (row.loaded) return 'On';
  return 'Loading';
}

/* ---------------- profiles ---------------- */

export function profileList(raw) {
  const s = raw && typeof raw === 'object' ? raw : {};
  const rows = (Array.isArray(s.profiles) ? s.profiles : [])
    .filter((p) => p && typeof p.name === 'string' && (p.name === 'default' || PROFILE_NAME.test(p.name)))
    .slice(0, 12)
    .map((p) => ({ name: p.name, active: p.active === true, isDefault: p.name === 'default' }));
  const active = rows.find((r) => r.active);
  return { rows, active: active ? active.name : typeof s.active === 'string' ? s.active : 'default', max: count(s.max) || 8 };
}

/* Why a typed profile name cannot be used, or '' when it can. The shell checks again. */
export function profileNameProblem(typed, existing) {
  const n = String(typed || '').trim().toLowerCase();
  if (!n) return 'Type a name for the profile.';
  if (n === 'default') return 'That name is already used by the default profile.';
  if (!PROFILE_NAME.test(n)) return 'Use lower case letters, digits, - and _ (up to 24 characters).';
  if ((existing || []).some((r) => r.name === n)) return 'A profile with that name already exists.';
  return '';
}

/* The profile named on the toolbar button. Nothing for the default one: it is the normal browser. */
export function profileLabel(name) {
  return name && name !== 'default' && PROFILE_NAME.test(name) ? name : '';
}

/* ---------------- import summaries ---------------- */

function n(v, one, many) {
  const c = count(v);
  return c + ' ' + (c === 1 ? one : many);
}

/* What an import did, in words, from the counts the shell sends (never a value). */
export function importLines(raw) {
  const r = raw && typeof raw === 'object' ? raw : {};
  const lines = [];
  const b = r.bookmarks && typeof r.bookmarks === 'object' ? r.bookmarks : null;
  const h = r.history && typeof r.history === 'object' ? r.history : null;
  if (b) {
    lines.push('Bookmarks: ' + n(b.added, 'added', 'added') + ' of ' + n(b.found, 'found', 'found') + ', ' + n(b.existing, 'was already here', 'were already here') + (count(b.skipped) ? ', ' + n(b.skipped, 'skipped', 'skipped') : '') + '.');
  }
  if (h) {
    lines.push('History: ' + n(h.added, 'added', 'added') + ' of ' + n(h.found, 'found', 'found') + ', ' + n(h.existing, 'was already here', 'were already here') + (count(h.skipped) ? ', ' + n(h.skipped, 'skipped', 'skipped') : '') + (count(h.dropped) ? ', ' + n(h.dropped, 'older one cut to keep the list at its limit', 'older ones cut to keep the list at its limit') : '') + '.');
  }
  if (Number.isInteger(r.usable)) {
    lines.push('Passwords: ' + n(r.added, 'added', 'added') + ', ' + n(r.updated, 'updated', 'updated') + ', ' + n(r.same, 'already saved', 'already saved') + (count(r.skipped) ? ', ' + n(r.skipped, 'row skipped', 'rows skipped') : '') + (count(r.refused) + count(r.never) ? ', ' + n(count(r.refused) + count(r.never), 'not saved', 'not saved') : '') + '.');
    lines.push('Delete the CSV file now: it holds your passwords in plain text. Dourmouse did not copy it.');
  }
  const notes = Array.isArray(r.notes) ? r.notes.filter((x) => typeof x === 'string').slice(0, 5) : [];
  notes.forEach((t) => lines.push(t.slice(0, 300)));
  return lines;
}

/* ---------------- DRM ---------------- */

/* The status line in Site settings, from what the shell found. Honest when it is not there. */
export function drmLine(raw) {
  const s = raw && typeof raw === 'object' ? raw : null;
  if (!s || s.ok === false) return 'DRM status could not be read.';
  const line = str(s.line, 600);
  return line || 'DRM status could not be read.';
}

export function drmAvailable(raw) {
  return Boolean(raw && typeof raw === 'object' && raw.ready === true && raw.widevine === 'available');
}
