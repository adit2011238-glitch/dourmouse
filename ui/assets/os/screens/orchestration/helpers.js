/* Pure helpers for ORCHESTRATION and OFFICE. Both screens read the same two
   sources once: GET /api/activity (a snapshot) and the shared event stream
   (agent_activity deltas and delegate_fanout events). Nothing here touches
   the DOM, so node can test it.

   Event shapes, read from ActivityTracker in dourmouse/webui.py:
     agent_activity  { agents: { name: { status, last, concurrent } } }
     delegate_fanout { run_id, total, finished, branches: { index: { agent,
                       model, backend, local, phase, task, ok, error, elapsed_s } } }
   phase is 'start' or 'result'. A finished run is deleted server side right
   after its final event, so it is not in later snapshots. */

export const RUN_CAP = 12;
export const TASK_PREVIEW = 160;

export function seedAgents(snapshotAgents) {
  const out = {};
  const src = snapshotAgents && typeof snapshotAgents === 'object' ? snapshotAgents : {};
  for (const [name, a] of Object.entries(src)) {
    out[name] = {
      status: a && a.status ? a.status : 'idle',
      last: a ? a.last || null : null,
      concurrent: a && a.concurrent_call_ids && typeof a.concurrent_call_ids === 'object' ? Object.keys(a.concurrent_call_ids).length : 0,
      feed: a && Array.isArray(a.feed) ? a.feed.slice(-8) : [],
    };
  }
  return out;
}

/* Applies one agent_activity event. Returns the names that changed. */
export function applyAgentDelta(agents, evt) {
  const changed = [];
  const delta = evt && evt.agents && typeof evt.agents === 'object' ? evt.agents : {};
  for (const [name, patch] of Object.entries(delta)) {
    const prev = agents[name] || { status: 'idle', last: null, concurrent: 0, feed: [] };
    agents[name] = {
      status: patch && patch.status ? patch.status : prev.status,
      last: patch && patch.last !== undefined ? patch.last : prev.last,
      concurrent: patch && typeof patch.concurrent === 'number' ? patch.concurrent : prev.concurrent,
      feed: prev.feed,
    };
    changed.push(name);
  }
  return changed;
}

function normBranches(branches) {
  const out = {};
  for (const [idx, b] of Object.entries(branches && typeof branches === 'object' ? branches : {})) {
    out[idx] = { ...b };
  }
  return out;
}

/* runs: a plain object keyed by run_id. Oldest runs beyond RUN_CAP are
   dropped so a long session cannot grow it without limit. */
export function seedRuns(snapshotFanouts) {
  const runs = {};
  for (const [id, r] of Object.entries(snapshotFanouts && typeof snapshotFanouts === 'object' ? snapshotFanouts : {})) {
    runs[id] = { id, total: r.total || 0, finished: false, branches: normBranches(r.branches), order: Date.now() };
  }
  return runs;
}

export function applyFanout(runs, evt, now = Date.now()) {
  if (!evt || !evt.run_id) return null;
  const prev = runs[evt.run_id];
  runs[evt.run_id] = {
    id: evt.run_id,
    total: evt.total || (prev ? prev.total : 0),
    finished: Boolean(evt.finished),
    branches: normBranches(evt.branches),
    order: prev ? prev.order : now,
  };
  const ids = Object.keys(runs);
  if (ids.length > RUN_CAP) {
    ids.sort((a, b) => runs[a].order - runs[b].order);
    for (const id of ids.slice(0, ids.length - RUN_CAP)) delete runs[id];
  }
  return runs[evt.run_id];
}

export function branchState(b) {
  if (!b || b.phase !== 'result') return 'running';
  return b.ok === false || b.error ? 'failed' : 'done';
}

export const STATE_TAG = { running: 'warn', done: 'ok', failed: 'bad' };

export function trimTask(text, n = TASK_PREVIEW) {
  const t = String(text || '').replace(/\s+/g, ' ').trim();
  return t.length > n ? t.slice(0, n - 1) + '…' : t;
}

/* Rows of one run, in branch order. elapsed only once the branch reported
   it: a running branch has no start time in the event, so none is invented. */
export function branchRows(run) {
  if (!run) return [];
  return Object.entries(run.branches || {})
    .map(([idx, b]) => ({
      index: Number(idx),
      agent: b.agent || '',
      model: b.model || '',
      backend: b.backend || '',
      task: trimTask(b.task),
      state: branchState(b),
      elapsed: typeof b.elapsed_s === 'number' ? b.elapsed_s : null,
      error: b.error ? String(b.error) : '',
    }))
    .sort((a, b) => a.index - b.index);
}

export function runCounts(run) {
  const rows = branchRows(run);
  return {
    total: Math.max(run && run.total ? run.total : 0, rows.length),
    done: rows.filter((r) => r.state === 'done').length,
    failed: rows.filter((r) => r.state === 'failed').length,
    running: rows.filter((r) => r.state === 'running').length,
  };
}

/* agent name -> run id, for every branch still running (not finished). */
export function inMeeting(runs) {
  const map = {};
  for (const run of Object.values(runs)) {
    if (run.finished) continue;
    for (const b of Object.values(run.branches || {})) {
      if (b && b.agent && branchState(b) === 'running') map[b.agent] = run.id;
    }
  }
  return map;
}

export function liveRuns(runs) {
  return Object.values(runs).filter((r) => !r.finished).sort((a, b) => b.order - a.order);
}

/* One desk's status word: meeting beats working beats idle. The tracker only
   reports idle, computing and auth; meeting is derived from a running branch. */
export function deskStatus(name, agents, meeting) {
  if (meeting && meeting[name]) return 'meet';
  const a = agents[name];
  if (!a) return 'idle';
  if (a.status === 'computing') return 'work';
  if (a.status === 'auth') return 'auth';
  return 'idle';
}

export const DESK_WORD = { meet: 'in meeting', work: 'working', auth: 'needs sign-in', idle: 'idle' };

/* The lines of one branch out of GET /api/office_log?meeting=<run>. */
export function branchLines(meeting, callId) {
  const lines = meeting && Array.isArray(meeting.lines) ? meeting.lines : [];
  return callId ? lines.filter((l) => l.call_id === callId) : lines;
}

/* A running branch's call_id from the persisted meeting, matched by index. */
export function callIdFor(meeting, index) {
  const b = meeting && Array.isArray(meeting.branches) ? meeting.branches.find((x) => x.index === index) : null;
  return b ? b.call_id || '' : '';
}

export function runLabel(id) {
  const s = String(id || '');
  return s.length > 10 ? s.slice(0, 8) : s;
}
