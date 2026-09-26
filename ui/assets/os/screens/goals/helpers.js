/* Pure helpers for GOALS. Status words, progress and the audit line come
   from the store's own fields; nothing here invents a figure. The status sets
   mirror dourmouse/goals.py and must be kept in step with it by hand. */

export const ACTIVE = new Set(['PLANNING', 'READY', 'EXECUTING', 'VERIFYING', 'RECOVERING', 'REPLANNING']);
export const TERMINAL = new Set(['COMPLETED', 'FAILED', 'CANCELLED', 'EXPIRED']);
export const LIMITS = { objective: 600, step: 500, steps: 12, criteria: 6 };

const GOAL_WORD = {
  CREATED: 'Created', PLANNING: 'Planning', READY: 'Ready', EXECUTING: 'Executing', VERIFYING: 'Verifying',
  WAITING_FOR_APPROVAL: 'Needs your approval', WAITING_FOR_AUTHENTICATION: 'Needs sign-in', BLOCKED: 'Blocked',
  PAUSED: 'Paused', RECOVERING: 'Recovering', REPLANNING: 'Replanning', COMPLETED: 'Completed', FAILED: 'Failed',
  CANCELLED: 'Cancelled', EXPIRED: 'Expired',
};

export function goalWord(g) {
  const base = GOAL_WORD[g.status] || g.status;
  return g.status === 'PAUSED' && g.paused_from ? base + ' (was ' + (GOAL_WORD[g.paused_from] || g.paused_from).toLowerCase() + ')' : base;
}

export function goalTone(status) {
  if (status === 'FAILED') return 'bad';
  if (status === 'COMPLETED') return 'ok';
  if (['BLOCKED', 'WAITING_FOR_APPROVAL', 'WAITING_FOR_AUTHENTICATION', 'PAUSED'].includes(status)) return 'warn';
  return '';
}

const TASK_WORD = {
  COMPLETED: ['done', 'ok'], RUNNING: ['running', 'warn'], VERIFYING: ['verifying', 'warn'], RETRYING: ['retrying', 'warn'],
  READY: ['ready', ''], PENDING: ['waiting', ''], WAITING_FOR_APPROVAL: ['needs approval', 'warn'],
  BLOCKED: ['blocked', 'warn'], FAILED: ['failed', 'bad'], CANCELLED: ['cancelled', ''],
};

export function taskTag(status) {
  const t = TASK_WORD[status];
  return { word: t ? t[0] : String(status).toLowerCase(), tone: t ? t[1] : '' };
}

/* done over not-cancelled, straight from the store's counts. null when the
   goal has no counted task, so the screen says so instead of drawing 0%. */
export function progress(g) {
  const total = Number(g.tasks_total) || 0;
  if (!total) return null;
  const done = Number(g.tasks_done) || 0;
  return { done, total, pct: Math.round((done / total) * 100), verified: Number(g.tasks_verified) || 0 };
}

export function splitBoard(goals) {
  const list = Array.isArray(goals) ? goals : [];
  return {
    live: list.filter((g) => !TERMINAL.has(g.status)),
    finished: list.filter((g) => TERMINAL.has(g.status)),
  };
}

export function describeAudit(e) {
  const d = (e && e.detail) || {};
  switch (e && e.type) {
    case 'goal_created': return 'Goal created: ' + (d.objective || '');
    case 'goal_status_changed':
      return 'Goal is now ' + (d.status || '?') + (d.resumed ? ' (resumed)' : '') + (d.paused_from ? ' (paused from ' + d.paused_from + ')' : '') +
        (d.held_while_paused ? ' (recorded while paused, applies on resume)' : '') + (d.blocked_reason ? ': ' + d.blocked_reason : '');
    case 'task_created': return 'Task added: ' + (d.description || '');
    case 'task_status_changed': return 'Task is now ' + (d.status || '?') + (d.error ? ': ' + d.error : '');
    case 'verification':
      if (d.scope === 'goal') return 'Goal criteria check: ' + (d.error ? 'could not run (' + d.error + ')' : d.satisfied ? 'satisfied' : 'not satisfied');
      return 'Task verification: ' + (d.error ? 'could not run (' + d.error + ')' : d.verified ? 'verified' : 'not verified');
    case 'approval_requested': return 'Approval requested';
    case 'approval_resolved': return 'Approval ' + (d.approved ? 'granted' : 'declined') + (d.reason ? ': ' + d.reason : '');
    case 'recovery_attempted': return 'Recovery attempted' + (d.reason ? ': ' + d.reason : '');
    case 'tool_call': return 'Called ' + (d.name || 'a tool');
    case 'tool_result': return 'Result from ' + (d.name || 'a tool');
    case 'note': return d.error ? 'Note: ' + d.error : 'Note';
    default: return (e && e.type) || 'event';
  }
}

/* Mirrors the server's limits so the form says no before the request does. */
export function validateForm(f) {
  const objective = String(f.objective || '').trim();
  if (!objective) return 'Write what the goal should achieve.';
  if (objective.length > LIMITS.objective) return 'The objective is longer than ' + LIMITS.objective + ' characters.';
  const steps = lines(f.steps);
  const criteria = lines(f.criteria);
  if (steps.length > LIMITS.steps) return 'At most ' + LIMITS.steps + ' steps.';
  if (criteria.length > LIMITS.criteria) return 'At most ' + LIMITS.criteria + ' success criteria.';
  if ([...steps, ...criteria].some((l) => l.length > LIMITS.step)) return 'A line is longer than ' + LIMITS.step + ' characters.';
  return '';
}

export function lines(text) {
  return String(text || '').split('\n').map((l) => l.trim()).filter(Boolean);
}

export function createBody(f) {
  return { objective: String(f.objective).trim(), steps: lines(f.steps), success_criteria: lines(f.criteria), priority: f.priority || 'normal' };
}

export function cancelPrompt(g) {
  const p = progress(g);
  return 'Cancel this goal? "' + g.objective + '". ' + (p ? p.done + ' of ' + p.total + ' tasks are already done and stay done. ' : '') +
    'Every task that has not finished is cancelled and nothing more runs for this goal.';
}

export function approvePrompt(task, approved) {
  const shown = (task.pending_prompts || []).join(' | ');
  return (approved ? 'Approve this task? ' : 'Decline this task? ') + '"' + task.description + '". ' +
    (approved ? 'It will run once with exactly the actions it asked about' + (shown ? ': ' + shown : '') + '.'
      : 'It is marked failed and the goal becomes blocked.');
}
