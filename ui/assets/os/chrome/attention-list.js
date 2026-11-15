/* "Waiting for you" and "Recent" in the inspector's alerts tab (plan 1.8). Rows reuse the Notification
   Centre's own classes so the two read as one list. Go opens the item's place; the cross dismisses it. */

import { html, setHtml } from '../kit/html.js';
import { icon } from '../kit/icons.js';
import { ago } from '../kit/format.js';

export function createAttentionList({ root, attention }) {
  function row(it) {
    const stroke = it.kind === 'needs_you' ? 'var(--os-warn)' : 'currentColor';
    return html`<div class="nc-item${it.read ? '' : ' unread'}" data-attn="${it.id}" data-kind="${it.kind}"><div class="ic">${icon(it.kind === 'finished' ? 'CHECK' : 'BELL', '', { stroke, width: 1.6 })}</div><div class="bd"><div class="nt">${it.title}</div>${it.detail ? html`<div class="nm">${it.detail}</div>` : ''}</div><div><div class="tm">${ago(it.created * 1000)}</div><button type="button" class="nc-clear" data-attn-go="${it.id}" data-spec="Opens the place this item is about.">Go</button><button type="button" class="nx" data-attn-dismiss="${it.id}" aria-label="Dismiss: ${it.title}">&times;</button></div></div>`;
  }

  function paint() {
    const { items, loaded, error } = attention.state;
    const need = items.filter((i) => i.kind === 'needs_you');
    const rest = items.filter((i) => i.kind !== 'needs_you');
    const parts = [];
    if (error) parts.push(html`<div class="st st-error" role="alert"><div class="st-t">Could not read notifications</div><div class="st-m">${error}</div></div>`);
    if (need.length) {
      parts.push(html`<div class="cc-head">Waiting for you</div>`);
      need.forEach((i) => parts.push(row(i)));
    }
    if (rest.length) {
      parts.push(html`<div class="cc-head"><span>Recent</span><button type="button" class="nc-clear" data-attn-clear data-spec="Dismisses everything here that is not waiting for you.">Clear</button></div>`);
      rest.forEach((i) => parts.push(row(i)));
    }
    if (loaded && !error && !items.length) parts.push(html``);
    setHtml(root, html`${parts}`);
  }

  root.addEventListener('click', (e) => {
    const go = e.target.closest('[data-attn-go]');
    if (go) {
      const it = attention.state.items.find((i) => String(i.id) === go.dataset.attnGo);
      if (it) attention.open(it);
      return;
    }
    const x = e.target.closest('[data-attn-dismiss]');
    if (x) {
      attention.dismiss(Number(x.dataset.attnDismiss));
      return;
    }
    if (e.target.closest('[data-attn-clear]')) attention.clear();
  });
  const off = attention.onChange(paint);
  paint();
  return { paint, dispose: off };
}
