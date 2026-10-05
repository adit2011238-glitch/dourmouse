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

/* F4: the store stamps last_run for EVERY attempt, including one the confirmation
   gate refused and one that errored, and it records no result. So the row says
   what is actually known: a gated routine never ran, and anything else was
   attempted at that time with its result not recorded. It never says "last run"
   about an attempt it cannot show succeeded. `ago` is the formatted age or ''. */
export function lastAttemptText(entry, blocked, ago) {
  if (!entry || !entry.last_run) return blocked ? 'has never run: the gate refuses this tool' : 'never run';
  if (!ago) return blocked ? 'refused by the gate, did not run' : 'attempted, result not recorded';
  if (blocked) return 'refused by the gate ' + ago + ', did not run';
  return 'last attempt ' + ago + ', result not recorded';
}

export function sortEntries(list) {
  return (Array.isArray(list) ? list : []).slice().sort((a, b) => Number(Boolean(b.enabled)) - Number(Boolean(a.enabled)) || String(a.id).localeCompare(String(b.id)));
}

export function handoffText(what) {
  return 'Create a recurring routine for me with the schedule_recurring tool. What it should do and when, in my words: ' + String(what).trim() +
    '\n\nShow me the tool, its arguments and the schedule before you create it, and only use a tool that can run unattended.';
}


/* F5: what a person reads for a tool name. Known tools get a plain verb phrase; an unknown one is
   its own name with the underscores taken out, never an invented description. */
const TOOL_WORDS = {
  check_mail: 'Check mail', send_email: 'Send an email', web_search: 'Search the web', fetch_url: 'Read a web page',
  get_weather: 'Check the weather', scan_security: 'Scan this Mac', run_command: 'Run a command',
};
export function toolLabel(tool) {
  const t = String(tool || '');
  if (TOOL_WORDS[t]) return TOOL_WORDS[t];
  const words = t.replace(/_/g, ' ').trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : 'Unknown routine';
}

/* The arguments as a short phrase: a lone text argument reads as itself, several read as key and value. */
export function argsPhrase(tool, args) {
  const a = args && typeof args === 'object' ? args : {};
  const keys = Object.keys(a);
  if (!keys.length) return '';
  if (tool === 'web_search' && typeof a.query === 'string') return 'for "' + a.query + '"';
  if (keys.length === 1 && typeof a[keys[0]] === 'string') return '"' + a[keys[0]] + '"';
  return keys.map((k) => k.replace(/_/g, ' ') + ': ' + (typeof a[k] === 'string' ? a[k] : JSON.stringify(a[k]))).join(', ');
}

/* The server's schedule words ("DAILY AT 07:30", "EVERY 30 MINUTE(S)") as a sentence with real plurals. */
export function scheduleSentence(desc) {
  const raw = String(desc || '').trim();
  if (!raw) return 'Unknown schedule';
  const low = raw.toLowerCase();
  let m = /^every (\d+) (minute|hour|day|week)\(s\)$/.exec(low);
  if (m) {
    const n = Number(m[1]);
    return n === 1 ? 'Every ' + m[2] : 'Every ' + n + ' ' + m[2] + 's';
  }
  m = /^daily at (\d{1,2}:\d{2})$/.exec(low);
  if (m) return 'Every day at ' + m[1];
  m = /^every (\w+day) at (\d{1,2}:\d{2})$/.exec(low);
  if (m) return 'Every ' + m[1].charAt(0).toUpperCase() + m[1].slice(1) + ' at ' + m[2];
  const out = low.replace(/\(s\)/g, 's');
  return out.charAt(0).toUpperCase() + out.slice(1);
}

/* The next few runs across every enabled routine, soonest first: the "Up next" strip. */
export function upNext(entries, limit = 5) {
  return (Array.isArray(entries) ? entries : [])
    .filter((e) => e && e.enabled)
    .map((e) => ({ e, at: parseNext(e.next_run) }))
    .filter((x) => Number.isFinite(x.at))
    .sort((a, b) => a.at - b.at)
    .slice(0, limit);
}
