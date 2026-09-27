/* Launcher search. Pure, so node can test it. A query is split into words and
   EVERY word must match; each word scores by where it matches (the start of the
   label beats the start of a word, which beats anywhere in the label, which
   beats the help text, which beats a loose letter-by-letter match). Recently
   used items get a small lift. Nothing here builds a regular expression from
   what the user typed. */

const MAX_QUERY = 120;

function words(text) {
  return String(text || '').toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
}

function wordStart(text, token) {
  return words(text).some((w) => w.startsWith(token));
}

function subsequence(text, token) {
  let i = 0;
  for (let j = 0; j < text.length && i < token.length; j += 1) {
    if (text[j] === token[i]) i += 1;
  }
  return i === token.length;
}

export function tokens(query) {
  return String(query || '').toLowerCase().slice(0, MAX_QUERY).split(/\s+/).filter(Boolean).slice(0, 6);
}

/* 0 means "does not match". */
export function score(item, toks) {
  const label = String(item.label || '').toLowerCase();
  const help = String(item.help || '').toLowerCase();
  let total = 0;
  for (const t of toks) {
    let best = 0;
    if (label.startsWith(t)) best = 100;
    else if (wordStart(label, t)) best = 80;
    else if (label.includes(t)) best = 60;
    else if (wordStart(help, t)) best = 40;
    else if (help.includes(t)) best = 25;
    else if (subsequence(label, t)) best = 10;
    if (best === 0) return 0;
    total += best;
  }
  return total;
}

/* Matching items, best first; ties keep their original order. `recent` is a
   list of item ids, newest first. */
export function rank(items, query, recent = []) {
  const toks = tokens(query);
  if (!toks.length) return items.slice();
  const lift = new Map();
  recent.forEach((id, i) => lift.set(id, Math.max(0, 15 - i * 3)));
  return items
    .map((item, order) => ({ item, order, s: score(item, toks) }))
    .filter((r) => r.s > 0)
    .map((r) => ({ ...r, s: r.s + (lift.get(r.item.id) || 0) }))
    .sort((a, b) => b.s - a.s || a.order - b.order)
    .map((r) => r.item);
}

/* With no query: the recent items first (in recency order), then the rest as given. */
export function withRecentFirst(items, recent = []) {
  const byId = new Map(items.map((i) => [i.id, i]));
  const head = recent.map((id) => byId.get(id)).filter(Boolean);
  const seen = new Set(head.map((i) => i.id));
  return { recent: head, rest: items.filter((i) => !seen.has(i.id)) };
}
