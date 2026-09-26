/* The conversation view every thread screen shares (HOME, COMMS, RESEARCH,
   MEDIA, CODE, NEWS). One thread per screen, streamed from POST /api/chat
   through core/chat.js. A turn shows the real tool calls (click a chip for the
   real arguments and result), the model's reply, and any approval the server
   asked for as a card. Nothing here is a sample.

   A screen gives this a region to live in and calls start():

     const view = mountThreadView(regionEl, ctx, { placeholder: '...' });
     await view.start();

   The directive box is the shell's own (ctx.chrome.setComposer), so a thread
   screen owns exactly one region and the composer follows it. If the screen has
   stage-bar actions of its own, pass them as opts.actions (an array, or a
   function returning one) and call view.paintChrome() when they change. */

import { setHtml } from './html.js';
import { states } from './states.js';
import { md } from './md.js';
import { isAbort } from '../core/api.js';
import { headerText, toolSteps, chipTone, copyText, shouldFollow, recordsFor, isLive } from './thread-helpers.js';

/* Thread keys whose ledger was already read back this page load: the thread
   survives leaving and re-entering a screen (core/chat.js keeps it), so the
   ledger is read at most once per load. */
const restored = new Set();

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

export function mountThreadView(region, ctx, opts = {}) {
  const o = {
    scroller: null,
    placeholder: 'Ask anything, or /all <goal> to put every resource on it',
    emptyMessage: 'No conversation yet.',
    emptyHint: 'Type a directive below. Enter sends, Shift+Enter starts a new line.',
    newThread: true,
    autonomous: true,
    actions: [],
    onSent: null,
    onScope: null,
    ...opts,
  };
  const thread = ctx.chat;
  const scroller = o.scroller || region.closest('#body') || region.parentElement;
  const views = new Map(); /* turn id -> { el, paint } */
  const cards = new Map(); /* approval id -> card */
  const frames = new Map(); /* turn id -> pending repaint */
  region.dataset.region = '';

  /* ---------------- stage bar and composer ---------------- */
  function paintChrome() {
    const busy = thread.busy();
    const auto = ctx.autonomous.get();
    const own = typeof o.actions === 'function' ? o.actions() : o.actions;
    const actions = [...(own || [])];
    if (o.newThread) {
      actions.push({
        id: 'new', label: 'NEW THREAD', disabled: busy,
        title: busy ? 'Stop the run first' : '',
        spec: 'Starts a fresh conversation on the server for this tab. The old session file stays on disk. Refused while a run is in flight.',
        onClick: () => newThread(),
      });
    }
    if (o.autonomous) {
      actions.push({
        id: 'auto', label: 'AUTONOMOUS', pressed: auto,
        spec: 'Opt-in: raises how many steps a run may take from 8 to 24 for this tab. Still bounded by the governance budget. It is not auto-approve: every gated action still asks.',
        onClick: () => {
          ctx.autonomous.set(!ctx.autonomous.get());
          paintChrome();
        },
      });
    }
    ctx.chrome.setActions(actions);
    ctx.chrome.setComposer({
      placeholder: o.placeholder,
      busy,
      queued: thread.queue().length,
      onSend: (text) => {
        thread.send(text)
          .then(() => (o.onSent ? o.onSent() : undefined))
          .catch((err) => ctx.notify({ level: 'error', title: 'Send failed', detail: err && err.message }));
      },
      onStop: () => thread.stop().catch((err) => ctx.notify({ level: 'error', title: 'Stop failed', detail: err && err.message })),
    });
    ctx.chrome.setLive(busy);
  }

  /* ---------------- turns ---------------- */
  function buildTurn(turn) {
    const wrap = el('div', 'turn');
    wrap.dataset.turn = turn.id;
    const you = el('div', 'turn-you');
    you.append(el('div', 'who', 'You'), el('div', 'you', turn.text));
    const bot = el('div', 'turn-bot');
    const head = el('div', 'who');
    head.setAttribute('role', 'status');
    const chips = el('div', 'chips');
    const detail = el('div', 'chipdetail');
    detail.hidden = true;
    const think = document.createElement('details');
    think.className = 'think';
    think.hidden = true;
    const tsum = el('summary', '', 'THINKING');
    const tpre = el('pre', 'think-text');
    think.append(tsum, tpre);
    const aprs = el('div', 'aprs');
    const reply = el('div', 'reply md body-text');
    const terr = el('div', 'terr');
    terr.setAttribute('role', 'alert');
    const acts = el('div', 'turn-actions');
    const copy = el('button', 'os-btn', 'COPY');
    copy.type = 'button';
    copy.dataset.spec = 'Copies the raw reply text to the clipboard.';
    const regen = el('button', 'os-btn', 'REGENERATE');
    regen.type = 'button';
    regen.dataset.spec = 'Re-runs the same directive as a fresh turn. The previous answer stays in the transcript.';
    acts.append(copy, regen);
    bot.append(head, chips, detail, think, aprs, reply, terr, acts);
    wrap.append(you, bot);
    if (turn.restored) wrap.dataset.restored = 'true';

    const open = new Set();
    let lastReply = null;
    let lastThink = -1;
    let lastSteps = '';

    function paintChips() {
      const steps = toolSteps(turn);
      const sig = steps.map((s) => s.name + (s.running ? '*' : '') + (chipTone(s) || '') + (open.has(s) ? '+' : '')).join('|');
      if (sig === lastSteps) return;
      lastSteps = sig;
      chips.replaceChildren(...steps.map((s) => {
        const b = el('button', 'tag ' + chipTone(s), '◆ ' + s.name);
        b.type = 'button';
        b.setAttribute('aria-expanded', String(open.has(s)));
        b.dataset.spec = 'Tool chip. Click expands the real arguments and the real result. Never a summary.';
        b.addEventListener('click', () => {
          if (open.has(s)) open.delete(s);
          else {
            open.clear();
            open.add(s);
          }
          lastSteps = '';
          paint();
        });
        return b;
      }));
      const shown = steps.find((s) => open.has(s));
      detail.hidden = !shown;
      if (shown) {
        detail.replaceChildren(
          el('div', 'who', 'Arguments'), el('pre', 'chip-pre', shown.args || '(none)'),
          el('div', 'who', shown.running ? 'Result (running)' : 'Result'), el('pre', 'chip-pre', shown.result || (shown.running ? '' : '(empty)')),
        );
      }
    }

    function paintApprovals() {
      (turn.approvals || []).forEach((id) => {
        if (cards.has(id)) return;
        const entry = ctx.approvals.get(id);
        if (!entry) return;
        const card = ctx.approvals.render(aprs, { id: entry.id, prompt: entry.prompt, tool: entry.tool, autonomous: entry.autonomous });
        if (!card) return;
        cards.set(id, card);
        if (entry.state === 'pending' && document.activeElement === document.body) card.focus();
      });
    }

    function paint() {
      head.textContent = headerText(turn);
      paintChips();
      if (turn.think) {
        think.hidden = false;
        if (turn.think.length !== lastThink) {
          tpre.textContent = turn.think;
          lastThink = turn.think.length;
        }
      }
      paintApprovals();
      if (turn.reply !== lastReply) {
        lastReply = turn.reply;
        setHtml(reply, md(turn.reply));
      }
      terr.textContent = turn.error && turn.status === 'error' ? turn.error : '';
      const finished = !isLive(turn);
      acts.hidden = !finished || (!turn.reply && turn.status !== 'stopped' && turn.status !== 'error');
      copy.disabled = !turn.reply;
      regen.disabled = thread.busy();
    }
    copy.addEventListener('click', () => {
      const write = navigator.clipboard && navigator.clipboard.writeText ? navigator.clipboard.writeText(copyText(turn)) : Promise.reject(new Error('This browser does not allow clipboard access here.'));
      write.then(() => ctx.notify({ level: 'ok', title: 'Copied', detail: 'The reply is on the clipboard.', ttl: 2500 }), (err) => ctx.notify({ level: 'warn', title: 'Could not copy', detail: err && err.message }));
    });
    regen.addEventListener('click', () => {
      thread.send(turn.text).catch((err) => ctx.notify({ level: 'error', title: 'Send failed', detail: err && err.message }));
    });
    reply.addEventListener('click', (e) => {
      const a = e.target.closest('a[data-ext]');
      if (!a) return;
      e.preventDefault();
      if (!ctx.host.openExternal(a.getAttribute('href'))) ctx.notify({ level: 'warn', title: 'Link not opened', detail: 'This window cannot open external links.' });
    });
    paint();
    return { el: wrap, paint };
  }

  function follow(fn) {
    const stick = shouldFollow(scroller);
    fn();
    if (stick) scroller.scrollTop = scroller.scrollHeight;
  }

  function paintTurn(turn) {
    let v = views.get(turn.id);
    if (!v) {
      if (region.dataset.state !== 'populated') {
        region.replaceChildren();
        region.dataset.state = 'populated';
      }
      v = buildTurn(turn);
      views.set(turn.id, v);
      follow(() => region.append(v.el));
      return;
    }
    if (frames.has(turn.id)) return;
    /* coalesce token bursts into one repaint per animation frame */
    const run = () => {
      frames.delete(turn.id);
      if (ctx.signal.aborted) return;
      follow(() => v.paint());
    };
    frames.set(turn.id, true);
    if (typeof requestAnimationFrame === 'function') requestAnimationFrame(run);
    else run();
  }

  function paintAll() {
    views.clear();
    cards.clear();
    const turns = thread.turns();
    if (!turns.length) {
      states.empty(region, o.emptyMessage, { hint: o.emptyHint });
      return;
    }
    region.replaceChildren();
    region.dataset.state = 'populated';
    turns.forEach((t) => {
      const v = buildTurn(t);
      views.set(t.id, v);
      region.append(v.el);
    });
    scroller.scrollTop = scroller.scrollHeight;
  }

  /* ---------------- new thread and restore ---------------- */
  async function newThread() {
    if (thread.busy()) return;
    try {
      await ctx.api.post('/api/os/session/new', { tab_id: ctx.scope.tabId() });
      thread.clear();
    } catch (err) {
      if (isAbort(err)) return;
      ctx.notify({ level: 'error', title: 'Could not start a new thread', detail: err && err.message });
    }
  }

  async function restore() {
    states.loading(region, 'Restoring this thread');
    region.dataset.region = '';
    try {
      const d = await ctx.api.get('/api/os/session?tab_id=' + encodeURIComponent(ctx.scope.tabId()));
      thread.restore(recordsFor(d.turns, thread.key));
    } catch (err) {
      if (isAbort(err)) return;
      if (err && err.status === 404) {
        thread.clear();
      } else {
        states.error(region, err, { title: 'Could not restore this thread', retry: () => restore() });
        return;
      }
    }
    restored.add(thread.key);
    paintAll();
  }

  /* forget what is shown and read the ledger again (a project switch) */
  function reload() {
    thread.clear();
    restored.delete(thread.key);
    return restore();
  }

  /* ---------------- wiring ---------------- */
  thread.subscribe((e) => {
    if (e.kind === 'turn' && e.turn) paintTurn(e.turn);
    else if (e.kind === 'restore') paintAll();
    if (e.kind === 'busy' || e.kind === 'queue' || e.kind === 'restore') {
      paintChrome();
      views.forEach((v) => v.paint());
    }
  });
  ctx.scope.onChange(() => {
    if (o.onScope) o.onScope();
    reload();
  });
  /* the elapsed time of a running turn ticks once a second */
  ctx.every(1000, () => {
    const cur = thread.current();
    const v = cur && views.get(cur.id);
    if (v) v.paint();
  });

  return {
    paintChrome,
    paintAll,
    reload,
    restore,
    /* first paint: the chrome, then the thread as it stands or from the ledger */
    async start() {
      paintChrome();
      if (thread.turns().length || restored.has(thread.key)) paintAll();
      else await restore();
    },
  };
}
