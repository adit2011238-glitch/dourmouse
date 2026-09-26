/* Pure helpers for CODE (no DOM at import time, so node can test them). */

const HUNK = /^@@ -(\d{1,9})(?:,\d{1,9})? \+(\d{1,9})(?:,\d{1,9})? @@/;

/* A unified diff for ONE file as rows: { k: 'meta'|'hunk'|'add'|'del'|'ctx', t, o, n }.
   o and n are the old and new line numbers where they apply. Linear in the input. */
export function parseDiff(text, maxRows = 1500) {
  const rows = [];
  let o = 0;
  let n = 0;
  let inHunk = false;
  let added = 0;
  let deleted = 0;
  let total = 0;
  const lines = String(text || '').split('\n');
  if (lines.length && lines[lines.length - 1] === '') lines.pop();
  for (const line of lines) {
    total += 1;
    let row;
    if (line.startsWith('@@')) {
      const m = HUNK.exec(line);
      if (m) {
        o = Number(m[1]);
        n = Number(m[2]);
        inHunk = true;
      }
      row = { k: 'hunk', t: line };
    } else if (!inHunk) {
      row = { k: 'meta', t: line };
    } else if (line.startsWith('+')) {
      row = { k: 'add', t: line.slice(1), n };
      n += 1;
      added += 1;
    } else if (line.startsWith('-')) {
      row = { k: 'del', t: line.slice(1), o };
      o += 1;
      deleted += 1;
    } else if (line.startsWith('\\')) {
      row = { k: 'meta', t: line };
    } else {
      row = { k: 'ctx', t: line.slice(1), o, n };
      o += 1;
      n += 1;
    }
    if (rows.length < maxRows) rows.push(row);
  }
  return { rows, added, deleted, hidden: Math.max(0, total - rows.length) };
}

const STATUS_WORD = { M: 'modified', A: 'added', D: 'deleted', R: 'renamed', C: 'copied', T: 'type changed', U: 'conflict', '?': 'new, untracked' };
export function statusWord(s) {
  return STATUS_WORD[s] || 'changed';
}

export function statusTone(s) {
  if (s === 'D' || s === 'U') return 'bad';
  if (s === 'A' || s === '?') return 'ok';
  return 'warn';
}

/* "3 files, +12 -4" from the server's own counts */
export function totalsLine(data) {
  if (!data) return '';
  const n = data.total || 0;
  return n + (n === 1 ? ' file' : ' files') + ', +' + (data.added || 0) + ' -' + (data.deleted || 0);
}

/* The tools whose output belongs in a terminal block. */
export const TERMINAL_TOOLS = new Set(['run_python', 'claude_code', 'codex_code', 'deploy', 'search_files', 'diff_preview', 'edit_file', 'write_file', 'read_file']);

/* The newest tool steps of the thread that ran something, newest first. */
export function terminalSteps(turns, limit = 3) {
  const out = [];
  for (let i = (turns || []).length - 1; i >= 0 && out.length < limit; i -= 1) {
    const steps = (turns[i].steps || []).filter((s) => s.kind === 'tool' && TERMINAL_TOOLS.has(s.name));
    for (let j = steps.length - 1; j >= 0 && out.length < limit; j -= 1) out.push(steps[j]);
  }
  return out;
}

/* ok is false when the step failed; "unknown" is never turned into a pass */
export function stepExit(step) {
  if (step.running) return 'running';
  if (step.ok === false || /^ERROR:/.test(step.result || '')) return 'failed';
  return 'returned';
}

/* Which project is selected: the scope project's path when it is on the list, else this app. */
export function pickProject(projects, scopeProject, saved) {
  const list = projects || [];
  if (scopeProject && scopeProject.path) {
    const hit = list.find((p) => p.path === scopeProject.path);
    if (hit) return hit.id;
  }
  if (saved && list.some((p) => p.id === saved)) return saved;
  return list.length ? list[0].id : '';
}
