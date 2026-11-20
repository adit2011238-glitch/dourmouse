/* Pure helpers for the Memory screen (plan 2.4, PL-06, finding 210). Every word comes from the fields the server
   returned: nothing here guesses where a fact came from or how often it was used. */

import { whenShort } from '../../kit/format.js';

export const KIND_WORD = { preference: 'Preference', fact: 'Fact', person: 'Person', project: 'Project', instruction: 'Instruction', event: 'Event', record: 'Record' };
export const ORIGIN_WORD = {
  you_said: 'You said',
  learned_from_chat: 'Learned from chat',
  from_file: 'From a file',
  from_mail: 'From mail',
  from_research: 'From research',
};

/* The filter chips. `view` is what the server is asked for; the rest of the request is built in query(). */
export const FILTERS = [
  { id: 'all', label: 'All' },
  { id: 'review', label: 'Review' },
  { id: 'pinned', label: 'Pinned' },
  { id: 'recent', label: 'Used this week' },
  { id: 'closed', label: 'Replaced' },
];

export function query(state) {
  const p = new URLSearchParams();
  if (state.q && state.q.trim()) p.set('q', state.q.trim());
  if (state.kind) p.set('kind', state.kind);
  if (state.origin) p.set('origin', state.origin);
  if (state.filter === 'review') p.set('review', '1');
  if (state.filter === 'pinned') p.set('pinned', '1');
  if (state.filter === 'recent') p.set('used_days', '7');
  if (state.filter === 'closed') p.set('closed', '1');
  p.set('limit', '200');
  const s = p.toString();
  return s ? '?' + s : '';
}

export function kindWord(kind) {
  return KIND_WORD[kind] || 'Fact';
}

export function originWord(origin) {
  return ORIGIN_WORD[origin] || 'Learned';
}

export function usesWord(n) {
  const v = Number(n) || 0;
  if (!v) return 'Not used yet';
  return v === 1 ? 'Used once' : 'Used ' + v + ' times';
}

/* The second line of a row: kind, where it came from, how old, how often used, and the owner's own marks. */
export function rowSub(fact, now = Date.now()) {
  const bits = [kindWord(fact.kind), originWord(fact.origin)];
  const when = whenShort(Date.parse(fact.created_at), now);
  if (when) bits.push(when);
  bits.push(usesWord(fact.uses));
  if (fact.pinned) bits.push('Pinned');
  if (fact.hidden) bits.push('Hidden from replies');
  if (fact.closed) bits.push('Replaced');
  return bits.join(' · ');
}

/* The line of the counts under the title. */
export function countsText(counts) {
  if (!counts) return '';
  const n = counts.facts || 0;
  const bits = [n === 1 ? '1 thing' : n + ' things'];
  if (counts.review) bits.push(counts.review + ' to review');
  return bits.join(' · ');
}

/* What the privacy line says about leaving the Mac. */
export function cloudLine(cloud) {
  if (cloud === true) return 'Relevant facts are added to your messages when a cloud model answers, with names and numbers masked.';
  if (cloud === false) return 'Facts are not added to messages while Memory is off or Private mode is on.';
  return '';
}

/* One history row. */
export function historyLine(h) {
  const when = whenShort(Date.parse(h.at));
  return [when || h.at, h.change].filter(Boolean).join(' · ');
}

/* The markdown and JSON files the Export button saves. */
export function fileName(kind, now = new Date()) {
  const p = (n) => String(n).padStart(2, '0');
  return 'dourmouse-memory-' + now.getFullYear() + p(now.getMonth() + 1) + p(now.getDate()) + (kind === 'json' ? '.json' : '.md');
}
