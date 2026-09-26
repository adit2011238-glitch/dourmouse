/* Pure helpers for RESEARCH (no DOM at import time, so node can test them). */

/* The server's stage list as flow boxes. "built" only when the server said a tool is registered. */
export function loopBoxes(stages) {
  return (stages || []).map((s) => ({ key: s.key, title: s.title, tone: s.built ? 'built' : 'todo' }));
}

export function stagesSub(loop) {
  if (!loop) return 'research pipeline';
  return 'pipeline · ' + loop.built + ' of ' + loop.total + ' stages have a tool';
}

/* A line of the stored counts, only the ones that are real numbers. */
export function countsLine(counts) {
  const c = counts || {};
  const names = [['research_question', 'question', 'questions'], ['claim', 'claim', 'claims'], ['source', 'source', 'sources'], ['hypothesis', 'hypothesis', 'hypotheses'], ['experiment', 'experiment', 'experiments'], ['experiment_run', 'run', 'runs'], ['contradiction', 'contradiction', 'contradictions']];
  const parts = [];
  names.forEach(([k, one, many]) => {
    if (typeof c[k] === 'number') parts.push(c[k] + ' ' + (c[k] === 1 ? one : many));
  });
  return parts.join(', ');
}

/* What a claim's tag says. There is no stored "verified" flag: a stored claim's quote was
   matched verbatim on the fetched page; "contested" means a contradicted_by edge exists. */
export function claimTag(c) {
  if (String(c.status || '').toUpperCase() === 'REJECTED') return { text: 'rejected', tone: 'bad' };
  if (c.contested || (c.contradicted_by && c.contradicted_by.length)) return { text: 'contested', tone: 'warn' };
  return { text: 'quote matched', tone: 'ok' };
}

/* http and https only, never anything else, so a stored URL cannot become a javascript: link */
export function safeUrl(u) {
  const s = String(u || '').trim();
  return /^https?:\/\/[^\s]{1,2000}$/i.test(s) ? s : '';
}

export function hostOf(u) {
  const s = safeUrl(u);
  if (!s) return '';
  const m = /^https?:\/\/([^/?#:]{1,255})/i.exec(s);
  return m ? m[1] : '';
}

const RANK = { hypothesis: 0, research_question: 0, claim: 2, evidence: 2, experiment: 2, decision: 3, contradiction: 3, task: 3, experiment_run: 3, result: 3, metric: 3, dataset: 3 };
const PER_ROW = 5;
export const NODE_W = 104;
export const NODE_H = 24;

/* Places the root on top and the rest in rows by kind. Pure and deterministic. */
export function layoutGraph(nodes, edges, rootId) {
  const list = (nodes || []).slice();
  const rows = new Map();
  const rankOf = (n) => {
    if (n.id === rootId) return 0;
    if (n.type === 'research_question') return 1;
    return RANK[n.type] === undefined ? 3 : Math.max(RANK[n.type], 2);
  };
  list.forEach((n) => {
    const r = rankOf(n);
    if (!rows.has(r)) rows.set(r, []);
    rows.get(r).push(n);
  });
  const width = 640;
  const placed = new Map();
  let y = 30;
  [...rows.keys()].sort((a, b) => a - b).forEach((r) => {
    const items = rows.get(r);
    for (let i = 0; i < items.length; i += PER_ROW) {
      const chunk = items.slice(i, i + PER_ROW);
      const gap = width / (chunk.length + 1);
      chunk.forEach((n, j) => placed.set(n.id + '|' + n.type, { ...n, x: Math.round(gap * (j + 1)), y }));
      y += 56;
    }
  });
  const byId = new Map();
  placed.forEach((n) => byId.set(n.type + ':' + n.id, n));
  const lines = (edges || []).map((e) => {
    const a = byId.get(e.src_type + ':' + e.src);
    const b = byId.get(e.dst_type + ':' + e.dst);
    return a && b ? { relation: e.relation, x1: a.x, y1: a.y, x2: b.x, y2: b.y } : null;
  }).filter(Boolean);
  return { width, height: Math.max(80, y - 8), nodes: [...placed.values()], lines };
}

export function edgeTone(relation) {
  if (relation === 'contradicted_by') return 'var(--dm-error)';
  if (relation === 'supported_by' || relation === 'answers') return 'var(--dm-ok)';
  if (relation === 'spawned' || relation === 'revised_by') return 'var(--dm-active)';
  return 'var(--dm-fg-dim)';
}

export function nodeTone(type) {
  if (type === 'contradiction') return 'var(--dm-error)';
  if (type === 'claim' || type === 'evidence') return 'var(--dm-ok)';
  if (type === 'hypothesis' || type === 'research_question' || type === 'task' || type === 'decision') return 'var(--dm-active)';
  return 'var(--dm-fg-dim)';
}

export function shorten(s, n) {
  const t = String(s || '');
  return t.length > n ? t.slice(0, n - 1) + '…' : t;
}

/* the file the browser saves: the server named it, this only makes sure of the shape */
export function safeFilename(name) {
  const s = String(name || '').replace(/[^A-Za-z0-9._-]/g, '-').replace(/^\.+/, '').slice(0, 80);
  return s || 'research.md';
}
