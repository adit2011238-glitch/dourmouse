/* HOME: the central dispatch. One thread per tab, streamed from POST /api/chat
   through core/chat.js and drawn by the shared thread view (kit/thread-view.js).
   Around the thread HOME adds the project scope bar and the attention list.
   Every figure on the page is read from the server or derived from what it
   sent. Nothing here is a sample. */

import { states } from '../../kit/states.js';
import { ago } from '../../kit/format.js';
import { mountThreadView } from '../../kit/thread-view.js';
import { isAbort } from '../../core/api.js';
import { readSeen, markSeen } from '../apps/walkthrough.js';
import { seedAllHands, applyAllHands, counts as ahCounts, newestRuns, runWord, goalLine } from '../orchestration/allhands.js';

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
    const ahEl = el('div', 'home-ah');
    ahEl.hidden = true;
    const threadEl = el('div', 'home-thread');
    const walkEl = el('div', 'home-walk');
    walkEl.hidden = true;
    root.replaceChildren(scopeEl, walkEl, ahEl, attnEl, threadEl);
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
    let showAll = false;
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
      (showAll ? items : items.slice(0, 6)).forEach((it) => {
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
            /* keep keyboard focus in the list instead of dropping it to the page */
            const next = attnEl.querySelector('.os-btn');
            if (next) next.focus();
            else ctx.chrome.focusComposer();
          } catch (err) {
            b.disabled = false;
            ctx.notify({ level: 'error', title: 'Could not dismiss', detail: err && err.message });
          }
        });
        row.append(b);
        card.append(row);
      });
      if (items.length > 6) {
        const more = el('button', 'os-btn home-more', showAll ? 'SHOW FEWER' : 'SHOW ALL ' + items.length);
        more.type = 'button';
        more.dataset.spec = 'Shows every item that needs attention, or only the newest six. It changes nothing.';
        more.addEventListener('click', () => { showAll = !showAll; paintAttention(); const again = attnEl.querySelector('.home-more'); if (again) again.focus(); });
        card.append(more);
      }
      attnEl.replaceChildren(card);
    }

    /* ---------------- first-run permissions guide ----------------
       Offered once, ever: it is recorded as seen the moment it is shown (in the
       server's settings store), so it never comes back. The guide itself stays
       on the APPS screen. A read that fails or comes from a stale copy offers
       nothing rather than guessing. */
    async function offerWalkthrough() {
      const seen = await readSeen(ctx);
      if (seen !== false || ctx.signal.aborted) return;
      const text = el('span', 'home-walk-t', 'New here? See which macOS permissions Dourmouse uses and what each one lets it do. macOS only asks when a feature needs one.');
      const go = el('a', 'os-btn', 'OPEN THE PERMISSIONS GUIDE');
      go.href = '#/apps';
      go.dataset.spec = 'Opens APPS, where each macOS permission Dourmouse uses is explained with a button to its System Settings page. It changes no permission.';
      const later = el('button', 'os-btn', 'NOT NOW');
      later.type = 'button';
      later.dataset.spec = 'Hides this for good. The guide stays on the APPS screen.';
      later.addEventListener('click', () => { walkEl.hidden = true; });
      walkEl.replaceChildren(text, go, later);
      walkEl.hidden = false;
      const err = await markSeen(ctx);
      if (err) ctx.notify({ level: 'warn', title: 'Permissions guide', detail: 'Could not save that this was shown, so it may appear again: ' + err });
    }

    /* ---------------- all hands (/all) ----------------
       /all <goal> starts a run that works in the background. The server's
       reply says it streams in a window the shell does not have, so HOME
       shows the run itself and links to where it is drawn. */
    let runs = {};
    let ahReload = null;
    async function readRuns() {
      try {
        const r = await ctx.api.get('/api/allhands');
        if (ctx.signal.aborted) return;
        runs = seedAllHands(r && r.runs);
      } catch (err) {
        if (isAbort(err)) return;
      }
      paintAllHands();
    }
    function paintAllHands() {
      const now = Date.now() / 1000;
      /* the newest run, only while it works or for ten minutes after it finished */
      const run = newestRuns(runs, 1).find((r) => r.status === 'running' || (r.finished && now - Number(r.finished) < 600));
      if (!run) {
        ahEl.hidden = true;
        ahEl.replaceChildren();
        return;
      }
      const c = ahCounts(run);
      const line = el('span', 'home-ah-t');
      const tag = el('span', 'tag ' + (run.status === 'running' ? 'warn' : runWord(run) === 'done' ? 'ok' : 'bad'), 'ALL HANDS ' + runWord(run));
      const what = el('span', 'home-ah-goal', goalLine(run));
      const prog = el('span', 'muted', c.total ? c.done + ' of ' + c.total + ' models answered' + (c.failed ? ', ' + c.failed + ' failed' : '') : 'starting');
      line.append(tag, what, prog);
      const go = el('a', 'os-btn', run.status === 'running' ? 'WATCH IN ORCHESTRATION' : 'SEE THE ANSWER');
      go.href = '#/orchestration';
      go.dataset.spec = 'Opens ORCHESTRATION, where each model of this all hands run and the merged answer are shown. It changes nothing.';
      ahEl.hidden = false;
      ahEl.replaceChildren(line, go);
    }
    ctx.events.on('allhands', (evt) => {
      const run = applyAllHands(runs, evt);
      if (!run) return;
      paintAllHands();
      if (run.partial && !ahReload) {
        /* the start event has no goal: read the list once, coalescing a burst */
        ahReload = true;
        readRuns().finally(() => { ahReload = null; });
      }
    });

    /* ---------------- the thread ---------------- */
    const view = mountThreadView(threadEl, ctx, {
      scroller: root.parentElement,
      emptyHint: 'Type a directive below. Enter sends, Shift+Enter starts a new line. Start with /all and a goal to put every model on it at once; the run appears above the conversation and in ORCHESTRATION.',
      emptyActions: [
        { label: 'What needs my attention?', send: 'What needs my attention right now? Check mail, security alerts and anything still running, and answer in a few short lines.', spec: 'Sends this question to the companion on HOME. It uses the model and may call read-only tools.' },
        { label: 'What can you do here?', send: 'List what you can do on this Mac in five short lines, using only the tools you really have.', spec: 'Sends this question to the companion on HOME. It uses the model.' },
        { label: 'Open SECURITY', go: 'security', spec: 'Opens the SECURITY screen: the latest scan and what needs a decision.' },
        { label: 'Open COMMS', go: 'comms', spec: 'Opens COMMS: your Gmail inbox, read only until you approve a send.' },
      ],
      onSent: () => refreshAttention(),
      onScope: paintScope,
    });
    ctx.events.onStatus((s) => {
      if (s === 'error') states.stale(attnEl, 'Live updates paused. Reconnecting.');
      else states.clearStale(attnEl);
    });
    ctx.events.onResync(() => { refreshAttention(); readRuns(); });

    paintScope();
    await view.start();
    await refreshAttention();
    readRuns();
    offerWalkthrough();
    /* the shell moves focus to the stage title once a screen has mounted; ask again after that so typing starts at once */
    ctx.chrome.focusComposer();
    setTimeout(() => { if (!ctx.signal.aborted) ctx.chrome.focusComposer(); }, 120);
  },

  async refresh(ctx, reason) {
    if (reason === 'manual') {
      ctx.notify({ level: 'info', title: 'HOME', detail: 'The thread is live; nothing to refresh.', ttl: 2000 });
    }
  },
};
