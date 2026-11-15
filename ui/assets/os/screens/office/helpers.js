/* Pure helpers for the OFFICE screen (plan 1.7). Nothing here touches the DOM, so node can test it.

   Sources, each real: GET /api/os/office/floor (rooms, desks, state, agent-to-agent lines), GET /api/os/agents
   (profiles, aliases, capability tests) and the shared event stream (agent_activity, delegate_fanout). A desk is
   working, needs you or idle. There is no rate-limited state because no source reports one. */

import { toMs } from '../../kit/format.js';

export const STATUS_WORD = { working: 'Working', needs_you: 'Needs you', idle: 'Idle' };
export const LINE_WINDOW_MS = 90000;
export const TICKER_CAP = 30;
export const TICKER_SHOWN = 6;

export function initials(name) {
  const parts = String(name || '').split(/[\s_]+/).filter(Boolean);
  if (!parts.length) return '?';
  return (parts.length === 1 ? parts[0].slice(0, 2) : parts[0][0] + parts[1][0]).toUpperCase();
}

export function summaryLine(counts) {
  const c = counts || {};
  const bits = [];
  if (c.working) bits.push(c.working + ' working');
  if (c.needs_you) bits.push(c.needs_you + ' need you');
  return bits.length ? bits.join(', ') : 'Everyone is idle';
}

/** Lines between two desks that are still fresh enough to draw. Newest message per pair only. */
export function visibleLines(lines, now = Date.now(), windowMs = LINE_WINDOW_MS) {
  const seen = new Set();
  const out = [];
  for (const l of Array.isArray(lines) ? lines : []) {
    const at = toMs(l.at);
    if (!Number.isFinite(at) || now - at > windowMs) continue;
    const key = l.from + '>' + l.to;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ ...l, atMs: at });
  }
  return out;
}

export function bubbleText(subject, n = 44) {
  const t = String(subject || '').replace(/\s+/g, ' ').trim();
  return t.length > n ? t.slice(0, n - 1) + '…' : t;
}

/** skill or agent id -> the agent's display name. */
export function nameResolver(agents, aliases) {
  const byId = {};
  for (const a of agents || []) byId[a.id] = a.name;
  return (key) => byId[key] || byId[(aliases || {})[key]] || key || 'An agent';
}

/* ---------------- ticker ---------------- */

export function addTicker(list, item) {
  if (!item || !item.text) return list;
  const prev = list[0];
  if (prev && prev.text === item.text && Math.abs(prev.at - item.at) < 2000) return list;
  list.unshift(item);
  if (list.length > TICKER_CAP) list.length = TICKER_CAP;
  return list;
}

/** One agent_activity event -> ticker items. Feed polls ("live") are not agent activity and say nothing here. */
export function tickerFromActivity(evt, seen, name, now = Date.now()) {
  const out = [];
  const agents = evt && evt.agents && typeof evt.agents === 'object' ? evt.agents : {};
  for (const [skill, a] of Object.entries(agents)) {
    const status = a && a.status;
    const tool = a && a.last && a.last.tool;
    const prev = seen[skill] || { status: 'idle', tool: '' };
    if (status === 'computing' && tool && (prev.status !== 'computing' || prev.tool !== tool)) out.push({ at: now, text: name(skill) + ' is using ' + tool });
    else if (status === 'auth' && prev.status !== 'auth') out.push({ at: now, text: name(skill) + ' needs you' });
    else if (status === 'idle' && (prev.status === 'computing' || prev.status === 'auth')) out.push({ at: now, text: name(skill) + ' finished' });
    if (status) seen[skill] = { status, tool: tool || '' };
  }
  return out;
}

export function tickerFromFanout(evt, runs, now = Date.now()) {
  if (!evt || !evt.run_id) return [];
  const out = [];
  if (!runs[evt.run_id]) {
    runs[evt.run_id] = true;
    out.push({ at: now, text: 'A task was split across ' + (evt.total || 'several') + ' agents' });
  }
  if (evt.finished) out.push({ at: now, text: 'The split task finished' });
  return out;
}

export function tickerFromLines(lines, name, known) {
  const out = [];
  for (const l of Array.isArray(lines) ? lines : []) {
    const key = l.from + '>' + l.to + '@' + l.at;
    if (known.has(key)) continue;
    known.add(key);
    const at = toMs(l.at);
    if (!Number.isFinite(at)) continue;
    out.push({ at, text: name(l.from) + ' asked ' + name(l.to) + (l.subject ? ': ' + bubbleText(l.subject, 60) : '') });
  }
  return out.sort((a, b) => a.at - b.at);
}

/* ---------------- team ---------------- */

export function testsText(profile) {
  const t = profile && profile.tests;
  if (!t || !t.total) return 'Not tested yet';
  return t.passed + ' of ' + t.total + ' tests passed';
}

export function teamRows(profiles, floorAgents) {
  const state = {};
  for (const a of floorAgents || []) state[a.id] = a;
  return (profiles || []).map((p) => ({
    id: p.id, name: p.name, job: p.job, status: (state[p.id] || {}).status || 'idle', tests: testsText(p),
    availability: p.availability || 'untested',
  }));
}

export function doingText(agent, name) {
  const d = agent && agent.doing;
  if (!d) return '';
  if (d.kind === 'job') return (agent.status === 'needs_you' ? 'Waiting for you on ' : 'Working on ') + (d.text || 'a job');
  return d.tool ? 'Using ' + d.tool : '';
}
