/* HOME: the central dispatch. One thread per tab, streamed from POST /api/chat
   through core/chat.js and drawn by the shared thread view (kit/thread-view.js).
   Around the thread HOME adds the project scope bar and the attention list.
   Every figure on the page is read from the server or derived from what it
   sent. Nothing here is a sample. */

import { states } from '../../kit/states.js';
import { ago } from '../../kit/format.js';
import { mountThreadView } from '../../kit/thread-view.js';
import { isAbort } from '../../core/api.js';

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

export default {
  id: 'HOME',
  sub: 'central agent dispatch',
  css: true,
  thread: true,

  async mount(root, ctx) {
    const attnEl = el('div', 'home-attn');
    attnEl.dataset.region = '';
    const scopeEl = el('div', 'home-scope');
    const threadEl = el('div', 'home-thread');
    root.replaceChildren(scopeEl, attnEl, threadEl);
    root.dataset.state = 'populated';

    /* ---------------- scope (project) ---------------- */
    const paintScope = () => {
      const p = ctx.scope.project();
      if (!p) {
        scopeEl.hidden = true;
        scopeEl.replaceChildren();
        return;
      }
      scopeEl.hidden = false;
      const label = el('span', 'muted', 'Project scope: ');
      const name = el('b', '', p.name || p.tab_id);
      const leave = el('button', 'os-btn', 'LEAVE PROJECT');
      leave.type = 'button';
      leave.dataset.spec = 'Returns this tab to its own general conversation. The project keeps its own thread on the server.';
      leave.addEventListener('click', () => ctx.scope.leaveProject());
      scopeEl.replaceChildren(label, name, leave);
    };

    /* ---------------- attention ---------------- */
    let attnItems = null;
    async function refreshAttention() {
      try {
        const d = await ctx.api.get('/api/attention');
        attnItems = Array.isArray(d.items) ? d.items : [];
        states.clearStale(attnEl);
        paintAttention();
      } catch (err) {
        if (isAbort(err)) return;
        if (attnItems && attnItems.length) states.stale(attnEl, 'Could not refresh this list: ' + err.message);
        else if (attnItems === null) states.error(attnEl, err, { title: 'Could not read the attention list', retry: () => refreshAttention() });
      }
    }
    function paintAttention() {
      const items = attnItems || [];
      if (!items.length) {
        attnEl.replaceChildren();
        attnEl.dataset.state = 'empty';
        attnEl.hidden = true;
        return;
      }
      attnEl.hidden = false;
      attnEl.dataset.state = 'populated';
      const card = el('div', 'card');
      card.append(el('div', 'lbl', 'Needs attention (' + items.length + ')'));
      items.slice(0, 6).forEach((it) => {
        const row = el('div', 'os-row');
        row.append(el('span', 'tag warn', String(it.kind || 'note').replace(/_/g, ' ')));
        const t = el('span', 'rt', it.summary || '');
        t.title = it.detail || '';
        row.append(t, el('span', 'muted', (it.screen || '') + (ago(it.at) ? ' · ' + ago(it.at) : '')));
        const b = el('button', 'os-btn', 'DISMISS');
        b.type = 'button';
        b.dataset.spec = 'Clears this one card. The same pattern can fire again on the next turn.';
        b.addEventListener('click', async () => {
          b.disabled = true;
          try {
            await ctx.api.post('/api/attention/dismiss', { id: it.id });
            await refreshAttention();
          } catch (err) {
            b.disabled = false;
            ctx.notify({ level: 'error', title: 'Could not dismiss', detail: err && err.message });
          }
        });
        row.append(b);
        card.append(row);
      });
      if (items.length > 6) card.append(el('div', 'muted', items.length - 6 + ' more not shown.'));
      attnEl.replaceChildren(card);
    }

    /* ---------------- the thread ---------------- */
    const view = mountThreadView(threadEl, ctx, {
      scroller: root.parentElement,
      onSent: () => refreshAttention(),
      onScope: paintScope,
    });
    ctx.events.onStatus((s) => {
      if (s === 'error') states.stale(attnEl, 'Live updates paused. Reconnecting.');
      else states.clearStale(attnEl);
    });
    ctx.events.onResync(() => refreshAttention());

    paintScope();
    await view.start();
    await refreshAttention();
    ctx.chrome.focusComposer();
  },

  async refresh(ctx, reason) {
    if (reason === 'manual') {
      ctx.notify({ level: 'info', title: 'HOME', detail: 'The thread is live; nothing to refresh.', ttl: 2000 });
    }
  },
};
