/* Pure helpers for the conversation view every thread screen shares, kept
   DOM-free so node can test them. */

import { seconds } from './format.js';

const LIVE = new Set(['thinking', 'generating', 'working', 'waiting']);

export function toolSteps(turn) {
  return (turn.steps || []).filter((s) => s.kind === 'tool');
}

export function isLive(turn) {
  return LIVE.has(turn.status);
}

/* "Dourmouse . 2 steps . 2.4s", and the state while a turn is not finished.
   The step count is the real number of tool calls the turn made. */
export function headerText(turn, now = Date.now()) {
  const n = toolSteps(turn).length;
  const parts = ['Dourmouse'];
  if (n) parts.push(n + (n === 1 ? ' step' : ' steps'));
  const ms = turn.elapsedMs !== null && turn.elapsedMs !== undefined ? turn.elapsedMs : isLive(turn) ? Math.max(0, now - turn.startedAt) : null;
  const t = seconds(ms);
  if (t) parts.push(t);
  if (isLive(turn)) parts.push(turn.status);
  else if (turn.status === 'stopped') parts.push('stopped');
  else if (turn.status === 'error') parts.push('failed');
  if (turn.model) parts.push(turn.model);
  return parts.join(' · ');
}

export function chipTone(step) {
  if (step.running) return 'warn';
  if (step.ok === false || /^ERROR:/.test(step.result || '')) return 'bad';
  return '';
}

/* the text COPY puts on the clipboard: the raw reply, never a summary */
export function copyText(turn) {
  return turn.reply || '';
}

/* keep the view pinned to the newest text only when the reader is already
   near the bottom, so scrolling up to read is never yanked away */
export function shouldFollow(el, slack = 80) {
  return el.scrollHeight - el.scrollTop - el.clientHeight <= slack;
}

/* Only this thread's own turns belong on this screen. A record with no screen
   field predates per-screen threads and was HOME by definition. */
export function recordsFor(turns, key) {
  return (turns || []).filter((t) => (t.screen || 'HOME') === key);
}

export function homeRecords(turns) {
  return recordsFor(turns, 'HOME');
}
