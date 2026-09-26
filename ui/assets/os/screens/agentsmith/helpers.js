/* AGENTSMITH helpers: pure functions, no DOM at import time. What the screen
   claims about a draft is worked out here from the fields the server sent, so
   node can test that a status never reads better than the facts allow. */

const PY_TYPE = { string: 'str', integer: 'int', number: 'float', boolean: 'bool', object: 'dict', array: 'list', null: 'None' };

function pyType(spec) {
  if (!spec || typeof spec !== 'object') return 'Any';
  const t = Array.isArray(spec.type) ? spec.type[0] : spec.type;
  if (t === 'array' && spec.items && typeof spec.items === 'object' && PY_TYPE[spec.items.type]) {
    return 'list[' + PY_TYPE[spec.items.type] + ']';
  }
  return PY_TYPE[t] || 'Any';
}

/* "tool_name(a: str, b: int = ...) -> str". The return type is always str by
   the tool contract (handle(arguments: dict) -> str). An optional parameter
   shows "= ..." because the schema does not say its default. */
export function signature(name, schema) {
  const props = schema && typeof schema === 'object' && schema.properties && typeof schema.properties === 'object' ? schema.properties : {};
  const required = new Set(Array.isArray(schema && schema.required) ? schema.required : []);
  const args = Object.keys(props).slice(0, 12).map((k) => k + ': ' + pyType(props[k]) + (required.has(k) ? '' : ' = ...'));
  return String(name || '') + '(' + args.join(', ') + ') -> str';
}

/* Status words and tones. APPROVED is not "live": the tool is callable only
   after a restart, so the two are separate words and only a real registry check
   (live === true) earns "live". */
export function statusInfo(d) {
  const s = d && d.status;
  if (s === 'DRAFTED') return { tone: 'warn', word: 'awaiting review', group: 'pending' };
  if (s === 'APPROVED') {
    if (d.live === true) return { tone: 'ok', word: 'approved and live', group: 'approved' };
    if (d.live === false) return { tone: 'warn', word: 'approved, restart required', group: 'approved' };
    return { tone: 'warn', word: 'approved, not confirmed live', group: 'approved' };
  }
  if (s === 'APPROVAL_FAILED') return { tone: 'bad', word: 'approval failed', group: 'failed' };
  if (s === 'REJECTED') return { tone: '', word: 'rejected', group: 'rejected' };
  return { tone: '', word: String(s || 'unknown').toLowerCase(), group: 'other' };
}

export const GROUPS = [
  { key: 'pending', label: 'Awaiting your review' },
  { key: 'approved', label: 'Approved' },
  { key: 'failed', label: 'Approval failed' },
  { key: 'rejected', label: 'Rejected' },
  { key: 'other', label: 'Other' },
];

export function grouped(drafts) {
  const out = {};
  for (const d of drafts || []) {
    const g = statusInfo(d).group;
    (out[g] = out[g] || []).push(d);
  }
  return out;
}

export function ledeLine(board) {
  if (!board) return '';
  const pending = (board.counts && board.counts.DRAFTED) || 0;
  const total = board.total || 0;
  const shown = board.shown || 0;
  const bits = [total + (total === 1 ? ' draft' : ' drafts'), pending + ' awaiting your review'];
  if (board.restart_needed) bits.push(board.restart_needed + ' approved, not live until a restart');
  if (shown < total) bits.push('showing the newest ' + shown);
  return bits.join('. ') + '.';
}

export function restartLine(board) {
  const n = board && board.restart_needed;
  if (!n) return '';
  return (n === 1 ? 'One approved tool is' : n + ' approved tools are') + ' not live yet. Quit and reopen the app (restart the server) for '
    + (n === 1 ? 'it' : 'them') + ' to become callable. Every call will still ask for your confirmation.';
}

/* A change in the board that should repaint the list. Comparing this and not
   the whole payload means the ten second read never redraws a code block the
   owner is reading. */
export function boardSignature(board) {
  return JSON.stringify(((board && board.drafts) || []).map((d) => [d.id, d.status, d.live, d.decided_at, d.decision_reason]));
}

export function shortHash(sha) {
  return /^[0-9a-f]{64}$/.test(String(sha || '')) ? sha.slice(0, 12) : '';
}

export function approvePrompt(d) {
  const name = d.tool_name;
  return 'Approve "' + name + '"? You are approving the ' + d.module_lines + '-line module shown above (sha256 ' + shortHash(d.preview_sha256) + '...) and its own test. '
    + 'Approving writes the module to this workspace and runs the draft\'s test now. It is not callable until the server restarts, '
    + 'and after that every call still asks for your confirmation.';
}

export function rejectPrompt(d) {
  return 'Reject "' + d.tool_name + '"? It is kept in the list as rejected and can never be approved.';
}

/* The directive handed to Agent Smith. It states the limit so the model is not
   invited to try anything else: it may draft, never approve. */
export function draftDirective(text) {
  const gap = String(text || '').trim();
  return 'Use your draft_tool to draft a new tool for this capability gap. Only draft it: a human reviews the code and approves or rejects it, and you cannot approve. Capability gap: ' + gap;
}

export function draftCheck(text) {
  const t = String(text || '').trim();
  if (t.length < 10) return { ok: false, error: 'Say what the tool should do, in at least a sentence.' };
  if (t.length > 1500) return { ok: false, error: 'That is longer than 1,500 characters. Shorten it.' };
  return { ok: true, error: '' };
}

/* What the approved file on disk says about itself. */
export function diskLine(d) {
  if (!d || d.status !== 'APPROVED') return '';
  if (d.on_disk_matches === true) return 'The module file on disk matches the hash recorded at approval.';
  if (d.on_disk_matches === false) return 'WARNING: the module file on disk does not match the hash recorded at approval, or is missing. The server refuses to load it.';
  return '';
}

export function whyNotApprovable(d) {
  if (!d) return 'The draft could not be read.';
  if (d.status !== 'DRAFTED') return '';
  if (d.too_large_to_review) return 'This draft is too large to read here, so it cannot be approved here.';
  if (!d.module_preview) return 'There is no module text to review.';
  return '';
}
