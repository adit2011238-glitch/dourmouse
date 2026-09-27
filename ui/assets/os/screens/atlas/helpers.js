/* ATLAS (the world monitor, not the quant lab): pure helpers. */

export const POLL_MS = 120000; /* matches the server's default cache lifetime, DOURMOUSE_WORLD_PULSE_TTL */

export function channelName(name) {
  return String(name || '').replace(/_/g, ' ');
}

/* One row per source the server registered, sorted by name. count is every
   item the source returned (not only the ones with coordinates). */
export function channelRows(snap) {
  const src = snap && snap.sources && typeof snap.sources === 'object' ? snap.sources : {};
  return Object.keys(src)
    .sort()
    .map((name) => {
      const s = src[name] || {};
      return {
        name,
        label: channelName(name),
        ok: s.ok === true,
        count: Number.isFinite(Number(s.count)) ? Number(s.count) : 0,
        latency: Number.isFinite(Number(s.latency_ms)) ? Number(s.latency_ms) : null,
        error: s.ok === true ? '' : String(s.error || 'no answer').replace(/\?\S{0,400}/g, '').slice(0, 200), /* the query string of a feed address is noise */
      };
    });
}

export function totals(rows) {
  const answered = rows.filter((r) => r.ok);
  return {
    feeds: rows.length,
    answered: answered.length,
    failed: rows.length - answered.length,
    events: answered.reduce((n, r) => n + r.count, 0),
    reporting: answered.filter((r) => r.count > 0).length,
  };
}

/* STABLE / ELEVATED / HEIGHTENED / CRITICAL from the server's own composite. */
export function pulseTone(label) {
  const l = String(label || '').toUpperCase();
  if (l === 'STABLE') return 'ok';
  if (l === 'ELEVATED') return 'active';
  if (l === 'HEIGHTENED') return 'warn';
  if (l === 'CRITICAL') return 'bad';
  return 'dim';
}

export const PULSE_NOTE = 'The score is a deterministic composite of only three signals: high and critical disaster alerts, high severity cyber advisories, and whether market movers are mostly up or mostly down. The other channels are information and do not move it.';

/* The items behind the counts, strongest first. Every field is what the feed
   returned; a link is offered only when it is a real http(s) address. */
const RANK = { critical: 0, high: 1, med: 2, medium: 2, moderate: 2 };
export const SIGNAL_CAP = 12;

export function safeLink(link) {
  const u = String(link || '').trim();
  return /^https?:\/\/[^\s/][^\s]{0,1999}$/i.test(u) ? u : '';
}

export function signals(snap, channel = '', cap = SIGNAL_CAP) {
  const src = snap && snap.items && typeof snap.items === 'object' ? snap.items : {};
  const out = [];
  for (const [chan, list] of Object.entries(src)) {
    if (channel && chan !== channel) continue;
    (Array.isArray(list) ? list : []).forEach((i, n) => {
      if (!i || typeof i !== 'object') return;
      const sev = String(i.severity || '').toLowerCase();
      out.push({
        chan,
        label: channelName(chan),
        title: String(i.title || '').slice(0, 200) || '(no title)',
        summary: String(i.summary || '').slice(0, 300),
        severity: sev,
        rank: sev in RANK ? RANK[sev] : 3,
        link: safeLink(i.link),
        loc: Number.isFinite(Number(i.lat)) && Number.isFinite(Number(i.lon)) && i.lat !== '' && i.lon !== '' ? Number(i.lat).toFixed(1) + ', ' + Number(i.lon).toFixed(1) : '',
        n,
      });
    });
  }
  out.sort((a, b) => a.rank - b.rank || a.chan.localeCompare(b.chan) || a.n - b.n);
  return { rows: out.slice(0, cap), total: out.length };
}
