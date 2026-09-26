/* Pure helpers for PROJECTS (no DOM at import time, so node can test them). */

import { toMs, plural } from '../../kit/format.js';

export const STALE_DAYS = 14;
export const ACTIVE_CAP = 60;
export const ARCHIVED_CAP = 200;
export const NAME_MAX = 120;
export const DESCRIPTION_MAX = 2000;
const DAY = 86400000;

/* A project with no last_active, or none for two weeks, goes to the archived
   list. Same rule and same 14 days as the console's bookshelf. */
export function isStale(project, now = Date.now()) {
  const ms = toMs(project && project.last_active);
  return !Number.isFinite(ms) || now - ms > STALE_DAYS * DAY;
}

/* newest first; the server already sorts, this keeps a re-read stable */
function byRecent(a, b) {
  const x = toMs(a.last_active);
  const y = toMs(b.last_active);
  return (Number.isFinite(y) ? y : -Infinity) - (Number.isFinite(x) ? x : -Infinity);
}

export function splitProjects(list, now = Date.now()) {
  const items = (Array.isArray(list) ? list : []).filter((p) => p && typeof p.path === 'string' && p.path);
  const active = [];
  const archived = [];
  items.forEach((p) => (isStale(p, now) ? archived : active).push(p));
  active.sort(byRecent);
  archived.sort(byRecent);
  return { active, archived, total: items.length };
}

const SOURCE_WORD = { claude_code: 'Claude Code', codex_cli: 'Codex', manual: 'created here' };

export function sourceWord(key) {
  return SOURCE_WORD[key] || String(key);
}

/* "3 Claude Code, 1 Codex" from the real per-source counts; '' when there are none */
export function countsLine(project) {
  const counts = project && project.session_counts && typeof project.session_counts === 'object' ? project.session_counts : {};
  return Object.keys(counts)
    .filter((k) => Number(counts[k]) > 0)
    .map((k) => Number(counts[k]) + ' ' + sourceWord(k))
    .join(', ');
}

export function sessionCount(project) {
  const n = Number(project && project.session_count);
  return Number.isFinite(n) && n >= 0 ? n : 0;
}

export function sessionsWord(project) {
  const n = sessionCount(project);
  return n === 1 ? 'session' : 'sessions';
}

export function originLine(project) {
  const s = Array.isArray(project && project.sources) ? project.sources : [];
  return s.map(sourceWord).join(' and ');
}

export function excerpt(text, max = 180) {
  const t = String(text || '').replace(/\s+/g, ' ').trim();
  return t.length > max ? t.slice(0, max - 1) + '…' : t;
}

/* the scope record every chat and confirm will carry */
export function scopeFrom(project, opened) {
  const src = opened && opened.tab_id ? opened : project;
  return { tab_id: String(src.tab_id), name: String(src.name || project.name || src.tab_id), path: String(project.path) };
}

export function isActiveScope(project, scope) {
  return Boolean(scope && scope.tab_id && project && project.tab_id && String(scope.tab_id) === String(project.tab_id));
}

export function stopPrompt(project) {
  return 'Stop tracking "' + project.name + '"? It leaves this list. The folder and the Claude Code and Codex history stay exactly where they are, and nothing is deleted.';
}

export function createPrompt(plan, description) {
  return 'Create the project "' + plan.name + '"? This makes a new folder at ' + plan.path + ' and adds it to this list' +
    (description ? ' with your description as its context.' : '.') + ' Nothing existing is changed.';
}

export function validateName(raw) {
  const name = String(raw || '').trim();
  if (!name) return { ok: false, message: 'Give the project a name.' };
  if (name.length > NAME_MAX) return { ok: false, message: 'The name is ' + name.length + ' characters. The limit is ' + NAME_MAX + '.' };
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001f\u007f]/.test(name)) return { ok: false, message: 'The name cannot contain control characters.' };
  return { ok: true, name };
}

export function sourceStatusLines(view) {
  const lines = [];
  const cc = view && view.claude_code;
  const cx = view && view.codex_cli;
  lines.push({ label: 'Claude Code history', found: Boolean(cc && cc.configured), where: cc && cc.root ? String(cc.root) : '' });
  lines.push({ label: 'Codex history', found: Boolean(cx && cx.configured), where: cx && cx.db ? String(cx.db) : '' });
  return lines;
}

export { plural };
