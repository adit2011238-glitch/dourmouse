/* Pure helpers for PROJECTS (no DOM at import time, so node can test them). Every line of text the screen shows
   about a project is built here from what /api/os/project/* sent; nothing is a sample. */

import { plural } from '../../kit/format.js';

export const NAME_MAX = 120;
export const GOAL_MAX = 300;
export const INSTRUCTIONS_MAX = 8000;
export const NOTE_MAX = 4000;
export const TITLE_MAX = 60;
export const FILE_MAX_BYTES = 5000000;
export const TEXT_SUFFIXES = ['.txt', '.md', '.markdown', '.csv', '.tsv', '.json', '.py', '.js', '.ts', '.html', '.css', '.yaml',
  '.yml', '.toml', '.rst', '.tex', '.sql', '.sh', '.swift', '.c', '.h', '.cpp', '.java', '.log'];

/* ---------------- where we are: #/projects, #/projects/<id>, #/projects/<id>/<thread> ---------------- */

const ID = /^[A-Za-z0-9_.-]{1,80}$/;

export function parseHash(hash) {
  const parts = String(hash || '').replace(/^#/, '').split('/').filter(Boolean);
  if (parts[0] !== 'projects') return { pid: '', tid: '' };
  const pid = parts[1] && ID.test(parts[1]) ? parts[1] : '';
  const tid = pid && parts[2] && ID.test(parts[2]) ? parts[2] : '';
  return { pid, tid };
}

export function hashFor(pid, tid) {
  return '#/projects' + (pid ? '/' + pid + (tid ? '/' + tid : '') : '');
}

/* The tab a project thread talks on. The server seeds a "pj:" tab with that project's own instructions, agents
   and file names; no other project's store is read. */
export function projectTab(pid, tid) {
  return 'pj:' + pid + ':' + tid;
}

export function isProjectTab(tab) {
  return /^pj:[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+$/.test(String(tab || ''));
}

/* ---------------- covers and lines ---------------- */

/* a cover is "1".."6"; anything else is spread over the six cover tokens by the project id */
export function coverVar(project) {
  const c = String(project && project.cover ? project.cover : '');
  if (/^[1-6]$/.test(c)) return 'var(--tk-cover-' + c + ')';
  let h = 0;
  const id = String(project && (project.id || project.name) ? project.id || project.name : '');
  for (let i = 0; i < id.length; i += 1) h = (h * 31 + id.charCodeAt(i)) >>> 0;
  return 'var(--tk-cover-' + ((h % 6) + 1) + ')';
}

/* "2 open, 3 threads"; a zero part is left out, and a project with nothing says nothing */
export function metaLine(p) {
  const bits = [];
  const open = Number(p && p.open_work) || 0;
  const threads = Number(p && p.threads) || 0;
  if (open) bits.push(open + ' open');
  if (threads) bits.push(plural(threads, 'thread'));
  return bits.join(', ');
}

export function filterProjects(list, query) {
  const items = Array.isArray(list) ? list.filter((p) => p && typeof p.id === 'string' && p.id) : [];
  const q = String(query || '').trim().toLowerCase();
  if (!q) return items;
  return items.filter((p) => (String(p.name) + ' ' + String(p.goal || '')).toLowerCase().includes(q));
}

/* the server keeps agent ids ("mail_calendar"); a person reads "Mail calendar" */
export function agentLabel(id) {
  const t = String(id || '').replace(/[_-]+/g, ' ').trim();
  return t ? t.charAt(0).toUpperCase() + t.slice(1) : '';
}

export function sizeLabel(bytes) {
  const n = Number(bytes);
  if (!Number.isFinite(n) || n < 0) return '';
  if (n < 1000) return n + ' B';
  if (n < 1000000) return (n / 1000).toFixed(n < 10000 ? 1 : 0) + ' KB';
  return (n / 1000000).toFixed(1) + ' MB';
}

export function threadTitle(text) {
  const t = String(text || '').replace(/\s+/g, ' ').trim();
  return t.length > TITLE_MAX ? t.slice(0, TITLE_MAX - 3) + '...' : t || 'New thread';
}

/* ---------------- checks before anything is sent ---------------- */

export function validateName(raw) {
  const name = String(raw || '').trim();
  if (!name) return { ok: false, message: 'Give the project a name.' };
  if (name.length > NAME_MAX) return { ok: false, message: 'The name is ' + name.length + ' characters. The limit is ' + NAME_MAX + '.' };
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001f\u007f]/.test(name)) return { ok: false, message: 'The name cannot contain control characters.' };
  return { ok: true, name };
}

export function validateText(raw, what, max) {
  const text = String(raw || '').trim();
  if (!text) return { ok: false, message: 'Write something first.' };
  if (text.length > max) return { ok: false, message: 'The ' + what + ' is ' + text.length + ' characters. The limit is ' + max + '.' };
  return { ok: true, text };
}

export function isTextFile(name) {
  const n = String(name || '').toLowerCase();
  return TEXT_SUFFIXES.some((s) => n.endsWith(s));
}

/* null when the file can be added, otherwise the sentence to show */
export function fileProblem(file) {
  if (!file || !file.name) return 'Pick a file.';
  if (file.size > FILE_MAX_BYTES) return file.name + ' is ' + sizeLabel(file.size) + '. The limit is ' + sizeLabel(FILE_MAX_BYTES) + '.';
  return null;
}

/* ---------------- work items ---------------- */

export function workState(item) {
  if (!item) return 'todo';
  if (item.status === 'done') return 'done';
  if (item.status === 'doing') return 'doing';
  return 'todo';
}

/* the one status change the checkbox makes: done goes back to to do, anything else is done */
export function workToggle(item) {
  return workState(item) === 'done' ? 'todo' : 'done';
}

export function workLine(item) {
  const bits = [];
  if (item.assignee) bits.push(agentLabel(item.assignee));
  if (workState(item) === 'doing') bits.push(item.job_id ? 'Running as a job' : 'In progress');
  return bits.join(', ');
}

/* ---------------- threads ---------------- */

/* A stored thread (GET /api/os/project/thread) as the records the thread view restores from: one finished turn per
   user message with the assistant message that follows it. A user message with no answer after it is dropped. */
export function turnRecords(messages) {
  const out = [];
  const list = Array.isArray(messages) ? messages : [];
  for (let i = 0; i < list.length; i += 1) {
    const m = list[i];
    if (!m || m.role !== 'user') continue;
    const next = list[i + 1];
    if (!next || next.role !== 'assistant') continue;
    const at = Number(m.at);
    out.push({
      screen: 'PROJECTS',
      display_text: String(m.content || ''),
      final_text: String(next.content || ''),
      timestamp: Number.isFinite(at) ? new Date(at < 1e11 ? at * 1000 : at).toISOString() : '',
      transcript: [],
    });
    i += 1;
  }
  return out;
}

/* A turn is an answer only when it finished with text that is not the engine's own error line. Only an answer is
   saved into the project thread. */
export function answered(turn) {
  return Boolean(turn && turn.status === 'done' && turn.reply && !/^\s*ERROR\b/.test(turn.reply));
}

/* the line under a thread's title: its summary when it has one, else when it last moved */
export function threadLine(t, whenText) {
  const s = String((t && t.summary) || '').replace(/\s+/g, ' ').trim();
  if (s) return s.length > 140 ? s.slice(0, 137) + '...' : s;
  return whenText || '';
}

export { plural };
