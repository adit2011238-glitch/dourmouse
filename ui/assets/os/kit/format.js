/* Small, DOM-free formatters. A timestamp may arrive as epoch milliseconds,
   epoch seconds (the server's time.time()) or an ISO string; anything that
   does not parse gives '' so a screen shows nothing rather than a wrong time. */

export function toMs(ts) {
  if (ts === null || ts === undefined || ts === '') return NaN;
  if (typeof ts === 'number') return ts < 1e11 ? ts * 1000 : ts;
  const n = Date.parse(String(ts));
  return Number.isFinite(n) ? n : NaN;
}

export function ago(ts, now = Date.now()) {
  const ms = toMs(ts);
  if (!Number.isFinite(ms)) return '';
  const s = Math.max(0, Math.round((now - ms) / 1000));
  if (s < 5) return 'now';
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm';
  if (s < 86400) return Math.floor(s / 3600) + 'h';
  return Math.floor(s / 86400) + 'd';
}

export function clock(ts) {
  const ms = toMs(ts);
  if (!Number.isFinite(ms)) return '';
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, '0');
  return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
}

export function seconds(ms) {
  if (typeof ms !== 'number' || !Number.isFinite(ms) || ms < 0) return '';
  return (ms / 1000).toFixed(1) + 's';
}

export function plural(n, one, many) {
  return n + ' ' + (n === 1 ? one : many || one + 's');
}

/* "just now" or "3m ago": the caller never builds "now ago" by hand */
export function agoLabel(ts, now = Date.now()) {
  const a = ago(ts, now);
  if (!a) return '';
  return a === 'now' ? 'just now' : a + ' ago';
}
