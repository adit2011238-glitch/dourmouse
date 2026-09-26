/* COMMS helpers: pure functions, no DOM at import time, so node can test them.
   Nothing here invents a mail fact: a value the server did not send comes back
   as an empty string, never a guess. */

import { toMs } from '../../kit/format.js';

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const pad = (n) => String(n).padStart(2, '0');

/* "Name <a@b.c>" gives "Name"; a bare address gives the address. */
export function senderName(from) {
  const s = String(from || '').trim();
  if (!s) return '';
  const m = /^\s*"?([^"<]{0,160}?)"?\s*<[^<>]{0,200}>\s*$/.exec(s);
  const name = m ? m[1].trim() : '';
  return name || senderAddress(s) || s;
}

export function senderAddress(from) {
  const s = String(from || '');
  const m = /<([^<>\s]{1,200})>/.exec(s);
  if (m) return m[1];
  const t = s.trim();
  return /^[^\s@<>]{1,100}@[^\s@<>]{1,100}$/.test(t) ? t : '';
}

/* The time shown on a row: a clock time today, "Sep 24" this year, a full date
   before that. Gmail's own timestamp is used when the row has one. */
export function whenLabel(row, now = Date.now()) {
  if (!row) return '';
  let ms = row.ts > 0 ? toMs(row.ts) : NaN;
  if (!Number.isFinite(ms) && row.date) ms = Date.parse(String(row.date).replace(' ', 'T'));
  if (!Number.isFinite(ms)) return String(row.date || '').slice(0, 16);
  const d = new Date(ms);
  const n = new Date(now);
  if (d.toDateString() === n.toDateString()) return pad(d.getHours()) + ':' + pad(d.getMinutes());
  if (d.getFullYear() === n.getFullYear()) return MONTHS[d.getMonth()] + ' ' + d.getDate();
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
}

/* How old the list is: says "cached" only when the server served its cache. */
export function listAge(payload, now = Date.now()) {
  if (!payload || !payload.cached_at) return '';
  const s = Math.max(0, Math.round((now - toMs(payload.cached_at)) / 1000));
  const age = s < 5 ? 'just now' : s < 60 ? s + 's ago' : Math.floor(s / 60) + 'm ago';
  return 'Read ' + age + (payload.cached ? ' (cached, refreshes after ' + Math.round((payload.ttl || 0) / 60 * 10) / 10 + ' min)' : '');
}

const ADDRESS = /^[^\s@<>,;"]{1,64}@[^\s@<>,;"]{1,190}\.[^\s@<>,;"]{2,}$/;

/* What the compose form may send. One recipient, a subject on one line without
   a double quote (the directive quotes it), and a body. The mail agent's
   approval card shows the recipient and the start of the body; the form and the
   turn above it show all of it. */
export function composeCheck({ to, subject, body }) {
  const t = String(to || '').trim();
  const s = String(subject || '').trim();
  if (!t) return { ok: false, error: 'Enter who it goes to.' };
  if (!ADDRESS.test(t)) return { ok: false, error: 'To must be one email address, for example name@example.com.' };
  if (!s) return { ok: false, error: 'Enter a subject.' };
  if (/[\r\n]/.test(s)) return { ok: false, error: 'The subject must be one line.' };
  if (s.includes('"')) return { ok: false, error: 'The subject cannot contain a double quote, because the directive quotes it.' };
  if (!String(body || '').trim()) return { ok: false, error: 'Write the message.' };
  return { ok: true, error: '' };
}

/* The console's fixed directive, word for word: it is aimed at the mail agent's
   gmail_send tool, whose approval gate is what actually asks before anything
   leaves. */
export function composeDirective({ to, subject, body }) {
  return `Send an email to ${String(to).trim()} with subject "${String(subject).trim()}" and this exact body:\n\n${body}`;
}

export function replyDraft(row, bodyOf) {
  const subject = String((bodyOf && bodyOf.subject) || (row && row.subject) || '').trim();
  return {
    to: senderAddress((bodyOf && bodyOf.from) || (row && row.from)),
    subject: /^re:/i.test(subject) ? subject : 'Re: ' + subject,
    body: '',
  };
}

const VERB = {
  archive: (what) => `Archive ${what}? It leaves the inbox and stays in All Mail, fully searchable. Nothing is deleted.`,
  trash: (what) => `Move ${what} to Trash? Gmail keeps it for 30 days and you can restore it from Gmail's Trash. It is not deleted permanently.`,
  flag: (what) => `Flag ${what}? This stars it in Gmail. Nothing moves.`,
  unflag: (what) => `Remove the flag from ${what}? This un-stars it in Gmail. Nothing moves.`,
};

export function actionPrompt(kind, row) {
  const f = VERB[kind];
  if (!f) return '';
  const subject = String((row && row.subject) || '(no subject)').slice(0, 120);
  const who = senderName(row && row.from);
  return f(`"${subject}"` + (who ? ' from ' + who : ''));
}

export const ACTION_DONE = {
  archive: 'Archived. It is still in All Mail.',
  trash: 'Moved to Trash. Gmail keeps it for 30 days.',
  flag: 'Flagged.',
  unflag: 'Flag removed.',
};

/* The result line a mutation reports: the server's own sentence when it sent
   one, our fixed sentence otherwise. */
export function doneLine(kind, result) {
  const m = result && typeof result.message === 'string' ? result.message.trim() : '';
  return m || ACTION_DONE[kind] || 'Done.';
}

/* The row after a flag change, without waiting for the next read. */
export function withFlag(rows, id, flagged) {
  return (rows || []).map((r) => (r.id === id ? { ...r, flagged } : r));
}

export function withoutRow(rows, id) {
  return (rows || []).filter((r) => r.id !== id);
}

/* A short honest reason for the unavailable state, from the mode the server saw. */
export function unavailableHint(mode) {
  if (mode === 'oauth') return 'Your Google session is signed in but Gmail did not answer.';
  if (mode === 'imap') return 'Mail is set up with an App Password, but Gmail did not answer.';
  return 'Sign in with Google at /login, or set GOOGLE_GMAIL_USER and GOOGLE_GMAIL_APP_PASSWORD, then refresh.';
}
