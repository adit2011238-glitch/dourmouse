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
        error: s.ok === true ? '' : String(s.error || 'no answer').slice(0, 200),
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
