/* SourceWidget: one cited source as a card: the URL (opened through the host
   only when it is http or https), the passage the claim was drawn from, and what
   is stored about the claim. Everything goes in through textContent. */

import { safeUrl, hostOf } from './helpers.js';

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

export function SourceWidget(claim, { openExternal } = {}) {
  const box = el('div', 'srcw');
  const url = safeUrl(claim.source);
  const head = el('div', 'srcw-head');
  head.append(el('span', 'lbl', 'Source'));
  if (url) {
    const open = el('button', 'os-btn srcw-open', 'OPEN ' + hostOf(url));
    open.type = 'button';
    open.dataset.spec = 'Opens the source page from which this claim was quoted, in the browser. It only opens the page.';
    open.addEventListener('click', () => {
      if (openExternal) openExternal(url);
    });
    head.append(open);
  } else {
    head.append(el('span', 'muted', claim.source ? 'The stored source is not an http or https address, so it is not linked.' : 'No source URL is stored for this claim.'));
  }
  box.append(head);
  if (url) box.append(el('div', 'srcw-url mono', url));
  const q = el('blockquote', 'srcw-quote', claim.passage || 'No passage is stored for this claim.');
  if (!claim.passage) q.classList.add('muted');
  box.append(q);
  const meta = [];
  if (claim.sub_question) meta.push('answers: ' + claim.sub_question);
  meta.push('version ' + (claim.version || 1));
  if (claim.contradicted_by && claim.contradicted_by.length) meta.push('contradicted by ' + claim.contradicted_by.length + (claim.contradicted_by.length === 1 ? ' claim' : ' claims'));
  box.append(el('div', 'muted srcw-meta', meta.join(' · ')));
  return box;
}
