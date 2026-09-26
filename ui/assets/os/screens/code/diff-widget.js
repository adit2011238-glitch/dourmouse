/* DiffWidget: one file's unified diff as coloured, numbered rows. Every line of
   text goes in through textContent, so a file's own content cannot become markup.

     const w = DiffWidget({ path, diff, added, deleted, status, note, onExpand, expanded });
     parent.append(w.el);   w.setDiff(newText)  repaints the rows only. */

import { parseDiff } from './helpers.js';

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

export function DiffWidget({ diff = '', label = '' } = {}) {
  const pre = el('div', 'dw');
  pre.setAttribute('role', 'region');
  pre.setAttribute('aria-label', label || 'diff');
  pre.tabIndex = 0;
  function setDiff(text) {
    const d = parseDiff(text);
    const frag = document.createDocumentFragment();
    d.rows.forEach((r) => {
      const line = el('div', 'dw-' + r.k);
      const no = el('span', 'dw-no', r.k === 'add' ? String(r.n) : r.k === 'del' ? String(r.o) : r.k === 'ctx' ? String(r.n) : '');
      no.setAttribute('aria-hidden', 'true');
      const sign = el('span', 'dw-sign', r.k === 'add' ? '+' : r.k === 'del' ? '-' : ' ');
      sign.setAttribute('aria-hidden', 'true');
      const body = el('span', 'dw-t', r.k === 'meta' || r.k === 'hunk' ? r.t : r.t);
      if (r.k === 'add' || r.k === 'del') line.setAttribute('aria-label', (r.k === 'add' ? 'added: ' : 'removed: ') + r.t);
      if (r.k === 'meta' || r.k === 'hunk') line.append(body);
      else line.append(no, sign, body);
      frag.append(line);
    });
    if (!d.rows.length) frag.append(el('div', 'dw-meta', 'No textual change (mode change or binary file).'));
    if (d.hidden) frag.append(el('div', 'dw-meta', d.hidden + ' more lines not shown.'));
    pre.replaceChildren(frag);
    return d;
  }
  const parsed = setDiff(diff);
  return { el: pre, setDiff, added: parsed.added, deleted: parsed.deleted };
}
