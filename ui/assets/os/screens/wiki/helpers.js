/* WIKI: pure helpers over GET /api/device_wiki entries. */

export const ROW_CAP = 200; /* rows drawn at once; the counts always cover every entry */
export const POLL_MS = 15000;

export const STATUS_TAG = { SUMMARIZED: 'ok', UNSUMMARIZED: 'warn', MISSING: '' };
export const STATUS_WORD = { SUMMARIZED: 'summarized', UNSUMMARIZED: 'unsummarized', MISSING: 'missing' };

export function counts(entries) {
  const c = { SUMMARIZED: 0, UNSUMMARIZED: 0, MISSING: 0, other: 0, total: 0 };
  (Array.isArray(entries) ? entries : []).forEach((e) => {
    const s = e && e.status;
    if (s in c && s !== 'other' && s !== 'total') c[s] += 1;
    else c.other += 1;
    c.total += 1;
  });
  return c;
}

export function baseName(path) {
  const p = String(path || '').replace(/[\\/]+$/, '');
  const i = Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\'));
  return i >= 0 ? p.slice(i + 1) || p : p;
}

export function dirName(path) {
  const p = String(path || '');
  const i = Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\'));
  return i > 0 ? p.slice(0, i) : '';
}

export function fmtSize(n) {
  if (n === null || n === undefined || n === '') return '';
  const v = Number(n);
  if (!Number.isFinite(v) || v < 0) return '';
  if (v < 1024) return v + ' B';
  if (v < 1048576) return (v / 1024).toFixed(v < 10240 ? 1 : 0) + ' KB';
  return (v / 1048576).toFixed(1) + ' MB';
}

/* Filter by status, then name. Missing rows sort last, otherwise by path. */
export function visibleRows(entries, filter, query, cap = ROW_CAP) {
  const q = String(query || '').trim().toLowerCase();
  const list = (Array.isArray(entries) ? entries : []).filter((e) => e && (!filter || filter === 'ALL' || e.status === filter) && (!q || String(e.path || '').toLowerCase().includes(q)));
  list.sort((a, b) => (a.status === 'MISSING') - (b.status === 'MISSING') || String(a.path).localeCompare(String(b.path)));
  return { rows: list.slice(0, cap), total: list.length, hidden: Math.max(0, list.length - cap) };
}

/* The newest time any scan touched a file. The store keeps last_seen per
   entry only, so this is the honest stand-in for "last scan". Seconds. */
export function lastScan(entries) {
  let best = 0;
  (Array.isArray(entries) ? entries : []).forEach((e) => {
    const t = Number(e && e.last_seen);
    if (Number.isFinite(t) && t > best) best = t;
  });
  return best || null;
}
