/* ALL HANDS runs (the /all <goal> command): pure helpers, no DOM at import
   time. One run sends one goal to several brains at once and merges the
   answers. Read from GET /api/allhands (runs, newest first) and followed off
   the shared stream as {type: 'allhands', run_id, status | brain, ...}.

   The start event carries no goal and no roster, so a run first seen from an
   event is a "partial" skeleton and the screen reads the list again. */

export const RUN_KEEP = 5;
export const PREVIEW = 200;

const TAG = { pending: '', running: 'warn', done: 'ok', error: 'bad' };
export const brainTag = (status) => (status in TAG ? TAG[status] : '');

export function seedAllHands(list) {
  const out = {};
  (Array.isArray(list) ? list : []).forEach((r) => {
    if (r && r.id) out[r.id] = { ...r, brains: r.brains && typeof r.brains === 'object' ? r.brains : {} };
  });
  return out;
}

/* Applies one allhands event. Returns the run it touched, or null. */
export function applyAllHands(runs, evt, now = Date.now() / 1000) {
  if (!evt || !evt.run_id) return null;
  let run = runs[evt.run_id];
  if (!run) {
    run = { id: evt.run_id, goal: '', started: now, status: 'running', brains: {}, synthesis: null, error: null, partial: true };
    runs[evt.run_id] = run;
  }
  if (evt.brain) {
    const b = run.brains[evt.brain] || { label: String(evt.brain).toUpperCase(), status: 'pending', result: null, error: null, elapsed: null };
    run.brains[evt.brain] = {
      ...b,
      status: evt.status || b.status,
      result: evt.status === 'done' && evt.summary ? (b.result || String(evt.summary)) : b.result,
      error: evt.status === 'error' ? String(evt.error || 'failed') : b.error,
    };
  } else if (evt.status === 'done') {
    run.status = 'done';
    if (evt.synthesis) run.synthesis = String(evt.synthesis);
    if (evt.error) run.error = String(evt.error);
    run.finished = run.finished || now;
  }
  return run;
}

export function counts(run) {
  const bs = Object.values((run && run.brains) || {});
  return {
    total: bs.length,
    done: bs.filter((b) => b.status === 'done').length,
    failed: bs.filter((b) => b.status === 'error').length,
    working: bs.filter((b) => b.status === 'running' || b.status === 'pending').length,
  };
}

export function newestRuns(runs, n = RUN_KEEP) {
  return Object.values(runs)
    .sort((a, b) => Number(b.started || 0) - Number(a.started || 0))
    .slice(0, n);
}

export function activeCount(runs) {
  return Object.values(runs).filter((r) => r.status === 'running').length;
}

export function runWord(run) {
  if (run.status === 'running') return 'running';
  if (run.error) return 'merge failed';
  const c = counts(run);
  return c.failed && c.failed === c.total ? 'all failed' : c.failed ? 'partly failed' : 'done';
}

export function runTone(run) {
  const w = runWord(run);
  return w === 'running' ? 'warn' : w === 'done' ? 'ok' : 'bad';
}

export function preview(text, n = PREVIEW) {
  const t = String(text || '').replace(/\s+/g, ' ').trim();
  return t.length > n ? t.slice(0, n - 1) + '…' : t;
}

export function goalLine(run) {
  return run && run.goal ? preview(run.goal, 160) : 'Goal not read yet';
}
