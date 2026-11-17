/* Pure helpers for the Automations screen (plan 2.2, finding 207). Every claim the screen makes about timing comes from
   the server's own fields: the next run is the schedule's next moment, the result word is the last recorded run, and
   nothing here guesses a result that was not recorded. */

import { whenShort } from '../../kit/format.js';

/* "2026-09-26 21:00" from the server is local time; anything else is unknown. */
export function parseNext(text) {
  const m = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})$/.exec(String(text || ''));
  if (!m) return NaN;
  return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]), Number(m[4]), Number(m[5])).getTime();
}

/* The tone of a run's result word. A step that waits for the owner or stopped at its limit is a warning, not a failure. */
export function resultTone(status) {
  if (status === 'ok') return 'ok';
  if (status === 'failed') return 'bad';
  if (status === 'parked' || status === 'capped') return 'warn';
  return '';
}

/* The second line of a list row. A paused one says why: switched off by three failures, or by the owner. */
export function rowSub(row, now = Date.now()) {
  if (!row) return '';
  if (row.auto_disabled) return row.trigger + ' · Switched off after 3 failures';
  if (!row.enabled) return row.trigger + ' · Paused';
  if (row.watch_error) return row.trigger + ' · ' + row.watch_error;
  const at = parseNext(row.next_run);
  if (Number.isFinite(at)) return row.trigger + ' · Next ' + whenShort(at, now);
  return row.trigger;
}

/* The rows of the parsed card the owner approves. */
export function cardRows(card) {
  if (!card) return [];
  return [
    ['When', card.trigger],
    ['Does', card.action],
    ['Can use', card.tools],
    ['Tells you', card.delivery],
    ['Limits', card.caps_text],
  ];
}

export function durationText(ms) {
  const n = Number(ms);
  if (!Number.isFinite(n) || n <= 0) return '';
  return n < 1000 ? n + ' ms' : (n / 1000).toFixed(1) + ' s';
}

/* One history row: the time and what started it, then what it used. */
export function historyLine(run) {
  const when = whenShort(Date.parse(run.at));
  const parts = [when || run.at, run.trigger].filter(Boolean);
  return parts.join(' · ');
}

export function historyNote(run) {
  const bits = [];
  if (run.tools && run.tools.length) bits.push(run.tools.map((t) => String(t).replace(/_/g, ' ')).join(', '));
  const d = durationText(run.duration_ms);
  if (d) bits.push(d);
  if (run.manual) bits.push('Run by you');
  return bits.join(' · ');
}

/* Runs that wait for the owner on a tool step (a prompt job's own approvals stay on the Jobs board). */
export function parkedRuns(history) {
  return (Array.isArray(history) ? history : []).filter((r) => r && r.status === 'parked' && !r.job_id);
}

export function countsText(rows) {
  const list = Array.isArray(rows) ? rows : [];
  const on = list.filter((r) => r.enabled).length;
  if (!list.length) return '';
  return list.length + (list.length === 1 ? ' automation' : ' automations') + ' · ' + on + ' on';
}

export const EXAMPLE = 'Every weekday at 8 check my security';
