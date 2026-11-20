/* HOME helpers: pure, no DOM, so node can test them. Every line of text on HOME is built here from what
   GET /api/os/today sent; nothing is a sample. */

import { plural } from '../../kit/format.js';

export function greetingLine(g) {
  const part = g && g.part ? g.part : 'day';
  const base = 'Good ' + part;
  return g && g.name ? base + ', ' + g.name : base;
}

/* "Saturday 14 November. 4 agents are working, 1 thing needs you." Zero parts are left out. */
export function subLine(today) {
  const date = (today.greeting && today.greeting.date) || '';
  const working = (today.office && today.office.working) || 0;
  const needs = today.needs_total || 0;
  const bits = [];
  if (working) bits.push(working === 1 ? '1 agent is working' : working + ' agents are working');
  if (needs) bits.push(needs === 1 ? '1 thing needs you' : needs + ' things need you');
  const tail = bits.length ? bits.join(', ') : 'Nothing needs you';
  return (date ? date + '. ' : '') + tail + '.';
}

/* a project cover is "1".."6" or anything else; anything else is spread over the six cover tokens by its id */
export function coverVar(project) {
  const c = String(project.cover || '');
  if (/^[1-6]$/.test(c)) return 'var(--tk-cover-' + c + ')';
  let h = 0;
  const id = String(project.id || project.name || '');
  for (let i = 0; i < id.length; i += 1) h = (h * 31 + id.charCodeAt(i)) >>> 0;
  return 'var(--tk-cover-' + ((h % 6) + 1) + ')';
}

export function avatarCover(index) {
  return 'var(--tk-cover-' + ((index % 6) + 1) + ')';
}

export function statusWord(status) {
  return status === 'working' ? 'working' : status === 'waiting' ? 'waiting for you' : 'idle';
}

export function officeLine(office) {
  const line = office && office.line;
  return line ? { agent: line.agent, text: line.text } : null;
}

export function calendarEmpty(cal) {
  return (cal && cal.note) || 'Nothing on your calendar today.';
}

/* The tab a Home ask talks on. A quick ask gets its own throwaway tab ("quick:<id>"; the server keeps no
   memory of it). An ask inside a project talks on that project's thread ("pj:<project>:<thread>"). */
export function quickTab(rand = Math.random) {
  return 'quick:' + Math.floor(rand() * 1e9).toString(36) + Date.now().toString(36);
}

export function projectTab(projectId, threadId) {
  return 'pj:' + projectId + ':' + threadId;
}

export function threadTitle(text) {
  const t = String(text || '').replace(/\s+/g, ' ').trim();
  return t.length > 60 ? t.slice(0, 57) + '...' : t || 'New thread';
}

/* A turn is an answer only when it finished with text that is not the engine's own error line (the engine
   reports a failed model call as a reply that starts with "ERROR:"). Only an answer is kept or offered for saving. */
export function answered(turn) {
  return Boolean(turn && turn.status === 'done' && turn.reply && !/^\s*ERROR\b/.test(turn.reply));
}

export function tileLine(p) {
  return p.line || 'No activity yet';
}

export function needsCount(n) {
  return n > 0 ? String(n) : '';
}

export { plural };

/* ---------------- the Today strip (plan 4.12) ---------------- */

/* "5 min ago", "just now", "2 h ago". An unreadable stamp gives '' so the card shows no age rather than a wrong one. */
export function dataAge(at, now = Date.now()) {
  const ms = at === null || at === undefined || at === '' ? NaN : (typeof at === 'number' ? (at < 1e11 ? at * 1000 : at) : Date.parse(at));
  if (!Number.isFinite(ms)) return '';
  const s = Math.max(0, Math.round((now - ms) / 1000));
  if (s < 60) return 'just now';
  if (s < 3600) return Math.floor(s / 60) + ' min ago';
  if (s < 86400) return Math.floor(s / 3600) + ' h ago';
  return Math.floor(s / 86400) + ' d ago';
}

/* The big figure of a card: only a real number is shown; unavailable and error cards show a hyphen. */
export function cardNumber(card) {
  if (!card || card.state === 'unavailable' || card.state === 'error') return '-';
  return typeof card.number === 'number' ? String(card.number) : '-';
}

/* The cards in the plan's fixed order, whatever order the server sent them in. */
export const STRIP_ORDER = ['mail', 'calendar', 'goals', 'overnight', 'security', 'usage'];
export function orderedStrip(strip) {
  const by = new Map((Array.isArray(strip) ? strip : []).map((c) => [c.key, c]));
  return STRIP_ORDER.filter((k) => by.has(k)).map((k) => by.get(k));
}

/* "Last 24 hours: 12 requests went to cloud models, 3 sources shared, 2 redactions." Zero parts stay: zero is the news. */
export function privacyText(p) {
  if (!p) return '';
  const n = (x, one, many) => x + ' ' + (x === 1 ? one : many);
  return 'Last ' + (p.hours || 24) + ' hours: ' + n(p.requests, 'request', 'requests') + ' to cloud models, '
    + n(p.sources, 'source', 'sources') + ' shared, ' + n(p.redactions, 'redaction', 'redactions') + '.';
}

export function continueLine(item) {
  return [item.project_name, item.line].filter(Boolean).join(', ');
}
