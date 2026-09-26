/* Pure helpers for TIMETABLE. The store keeps only the last run time of each
   routine (no run log and no missed record), so the only timing claims made
   here are the two that can be derived from it: when the next run is due, and
   whether that moment has already passed (the runner then fires it once on its
   next check, a catch-up). */

/* "2026-09-26 21:00" from the server is local time; anything else is unknown. */
export function parseNext(text) {
  const m = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})$/.exec(String(text || ''));
  if (!m) return NaN;
  return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]), Number(m[4]), Number(m[5])).getTime();
}

export function isOverdue(entry, now = Date.now()) {
  if (!entry || !entry.enabled) return false;
  const at = parseNext(entry.next_run);
  return Number.isFinite(at) && at <= now;
}

export function argsPreview(args, n = 140) {
  let text = '';
  try {
    text = JSON.stringify(args || {});
  } catch (_e) {
    text = '';
  }
  if (text === '{}') return 'no arguments';
  return text.length > n ? text.slice(0, n - 1) + '…' : text;
}

/* tool name -> permission word ('regular', 'requires_confirmation', ...) from /api/roster */
export function toolTiers(roster) {
  const out = {};
  for (const s of roster && Array.isArray(roster.subagents) ? roster.subagents : []) {
    for (const t of s.tools || []) out[t.name] = t.permission;
  }
  return out;
}

/* The runner fires unattended, so it only runs regular tier tools. */
export function cannotRunUnattended(entry, tiers) {
  const tier = tiers[entry.tool];
  return tier !== undefined && tier !== 'regular';
}

export function sortEntries(list) {
  return (Array.isArray(list) ? list : []).slice().sort((a, b) => Number(Boolean(b.enabled)) - Number(Boolean(a.enabled)) || String(a.id).localeCompare(String(b.id)));
}

export function handoffText(what) {
  return 'Create a recurring routine for me with the schedule_recurring tool. What it should do and when, in my words: ' + String(what).trim() +
    '\n\nShow me the tool, its arguments and the schedule before you create it, and only use a tool that can run unattended.';
}
