/* BROWSER split view: the chat column.

   A real conversation with the model, drawn beside the page so the owner can tell the AI what to do in the
   browser while watching it. It is the same thread BROWSER's ctx.chat gives every screen that has no thread of
   its own (the HOME conversation), shown with the shell's own thread view (turns, tool chips, replies and the
   approval cards), and every message is routed to the browser agent (focusAgent 'browser'). The browser agent
   follows the owner's active tab, reads the page by element ids, and asks before a risky click.

   The shell's bottom directive box is NOT used on this screen: the thread view is handed a copy of ctx whose
   chrome swallows setComposer (the busy, queued and send state land in this column's own box instead), so the
   page has exactly one place to type. The column header carries the same "who has the browser" bar as the page
   chrome (Model is acting, Stop, Take control). */

import { html, setHtml, raw } from '../../kit/html.js';
import { isImperativeAffirm } from '../../core/chat.js';
import { mountThreadView } from '../../kit/thread-view.js';
import { createControl } from './control-ui.js';
import { STARTERS, contextLine } from './split-model.js';

const SVG = (body) => raw('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + body + '</svg>');
export const ICON = {
  chat: SVG('<path d="M4 5h16v11H9l-5 4z"/>'),
  toRight: SVG('<path d="M9 6l6 6-6 6"/>'),
  toLeft: SVG('<path d="M15 6l-6 6 6 6"/>'),
  swap: SVG('<path d="M7 7h12M15 3l4 4-4 4M17 17H5M9 13l-4 4 4 4"/>'),
};

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

/* ctx: the screen's ctx. control: window.dourmouseShell.pane.control or null. note(text, tone): the screen's
   message line. shared: true when the page is the AI's own browser (the Electron pane).
   onHide(), onSwapSide(): the two header buttons. onActivity({ busy, attention }): the thread changed. */
export function createChatColumn({ ctx, control, note, shared, onHide, onSwapSide, onActivity }) {
  const body = el('div', 'bw-chat-body');
  body.setAttribute('role', 'region');
  body.setAttribute('aria-label', 'Chat with the AI about this page');

  /* ---------------- header ---------------- */
  const head = el('div', 'bw-chat-head');
  const top = el('div', 'bw-chat-top');
  const title = el('span', 'bw-chat-title', 'Tell the AI what to do');
  const sideBtn = el('button', 'cb-ico bw-chat-side');
  sideBtn.type = 'button';
  sideBtn.dataset.spec = 'Moves this chat column to the other side of the page. Your choice is remembered.';
  setHtml(sideBtn, html`${ICON.swap}`);
  const hideBtn = el('button', 'cb-ico bw-chat-hide');
  hideBtn.type = 'button';
  top.append(title, sideBtn, hideBtn);
  const ctlSlot = el('div', 'bw-chat-ctl');
  const ctxLine = el('div', 'bw-chat-ctx');
  ctxLine.setAttribute('role', 'status');
  head.append(top, ctlSlot, ctxLine);
  sideBtn.addEventListener('click', () => onSwapSide());
  hideBtn.addEventListener('click', () => onHide());

  let controlUi = null;
  let offControl = null;
  let disposed = false;
  if (control) {
    controlUi = createControl({ control, note });
    ctlSlot.append(controlUi.root);
    offControl = control.onUpdate((s) => {
      if (!disposed && controlUi) controlUi.update(s);
    });
    Promise.resolve(control.state())
      .then((s) => {
        if (!disposed && controlUi && s) controlUi.update(s);
      })
      .catch((err) => note('The browser did not answer: ' + (err && err.message ? err.message : String(err)), 'error'));
  }

  /* ---------------- thread ---------------- */
  const scroll = el('div', 'bw-chat-scroll');
  const threadRegion = el('div', 'bw-chat-thread');
  scroll.append(threadRegion);

  /* ---------------- composer ---------------- */
  const form = el('form', 'bw-chat-compose');
  form.noValidate = true;
  const label = el('label', 'sr-only', 'Message to the AI');
  const ta = el('textarea', 'os-field bw-chat-input');
  ta.id = 'bwChatInput';
  label.setAttribute('for', ta.id);
  ta.rows = 1;
  ta.placeholder = 'Tell the AI what to do here';
  ta.dataset.spec = 'Type what you want done in the browser. Enter sends, Shift+Enter starts a new line. The AI works in the tab you are looking at and asks you before anything risky. Alt+J jumps here.';
  const row = el('div', 'bw-chat-row');
  const queued = el('span', 'bw-chat-q');
  queued.hidden = true;
  const stopBtn = el('button', 'os-btn os-btn--danger bw-chat-stop', 'STOP');
  stopBtn.type = 'button';
  stopBtn.hidden = true;
  stopBtn.dataset.spec = 'Stops the AI: it ends the run and declines any question waiting for you, so nothing is left hanging.';
  const sendBtn = el('button', 'os-btn os-btn--primary bw-chat-send', 'SEND');
  sendBtn.type = 'submit';
  sendBtn.dataset.spec = 'Sends this to the AI. While it is still working, your message waits in line instead.';
  row.append(queued, stopBtn, sendBtn);
  form.append(label, ta, row);

  body.append(head, scroll, form);

  /* the composer state the thread view would have given the shell's box */
  let comp = null;
  /* Lines typed while a run is in flight. The shell's own queue forgets which agent a line was meant for (it sends
     the next one without focusAgent), so this column keeps its own and sends each to the browser agent in turn. */
  const waiting = [];
  function paintComposer() {
    const c = comp || {};
    sendBtn.textContent = c.busy ? 'QUEUE' : 'SEND';
    stopBtn.hidden = !c.busy;
    const q = (c.queued || 0) + waiting.length;
    queued.hidden = !q;
    queued.textContent = q ? q + ' waiting' : '';
  }
  function fit() {
    ta.style.height = 'auto';
    ta.style.height = Math.min(ta.scrollHeight, 130) + 'px';
  }
  function submit() {
    const text = ta.value.trim();
    if (!text) return;
    ta.value = '';
    fit();
    if (comp && comp.onSend) comp.onSend(text);
    else ctx.chat.send(text, { focusAgent: 'browser' }).catch((err) => ctx.notify({ level: 'error', title: 'Send failed', detail: err && err.message }));
  }
  ta.addEventListener('input', fit);
  ta.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      submit();
    }
  });
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    submit();
  });
  stopBtn.addEventListener('click', () => {
    waiting.length = 0; /* stopping ends what was waiting too, as the shell's own queue does */
    paintComposer();
    if (comp && comp.onStop) comp.onStop();
  });

  /* The thread view talks to the shell's directive box through ctx.chrome. Hand it a ctx whose chrome sends the
     composer state here, leaves the stage bar and the sidebar dot alone, and whose chat adds the browser agent
     to every message (a starter, a regenerate, a typed line). */
  const aiChat = Object.create(ctx.chat);
  aiChat.send = (text, opts) => {
    const t = String(text || '').trim();
    if (!t) return Promise.resolve(null);
    /* "yes" while a question is open answers that question (the thread does it); anything else waits its turn */
    if (ctx.chat.busy() && !(ctx.approvals.pending() && isImperativeAffirm(t))) {
      waiting.push(t);
      paintComposer();
      return Promise.resolve(null);
    }
    return ctx.chat.send(t, { focusAgent: 'browser', ...(opts || {}) });
  };
  function drainWaiting() {
    if (disposed || ctx.chat.busy() || !waiting.length) return;
    const next = waiting.shift();
    paintComposer();
    aiChat.send(next).catch((err) => ctx.notify({ level: 'error', title: 'Send failed', detail: err && err.message }));
  }
  const tctx = Object.create(ctx);
  tctx.chat = aiChat;
  tctx.chrome = {
    ...ctx.chrome,
    setComposer(o) {
      if (!o) return;
      comp = o;
      paintComposer();
    },
    setActions() {},
    setLive() {},
    focusComposer: () => ta.focus(),
  };
  ctx.chrome.setComposer(null); /* the shell's own box stays hidden here */

  const view = mountThreadView(threadRegion, tctx, {
    scroller: scroll,
    placeholder: ta.placeholder,
    emptyMessage: 'Tell the AI what to do in this browser.',
    emptyHint: 'It works in the tab you are looking at and asks you before anything risky. Pick a start below or type your own.',
    emptyActions: STARTERS.map((s) => ({ label: s.label, spec: s.spec, send: s.send })),
    newThread: false,
    autonomous: false,
    private: false,
    actions: [],
  });

  const announce = () => {
    if (disposed) return;
    drainWaiting();
    if (onActivity) onActivity({ busy: aiChat.busy(), attention: Boolean(ctx.approvals.pending()) });
  };
  aiChat.subscribe(announce);

  /* ---------------- what the header says ---------------- */
  let ctxSig = '';
  function setContext(v) {
    const c = contextLine(v, shared);
    const sig = c.kind + '|' + c.title + '|' + c.host + '|' + c.shared;
    if (sig === ctxSig) return;
    ctxSig = sig;
    ctxLine.dataset.kind = c.kind;
    const nodes = [el('span', 'k', c.kind === 'none' ? 'Tab' : 'On this tab'), el('span', 't', c.title)];
    if (c.host && c.host !== c.title) nodes.push(el('span', 'h', c.host));
    if (!c.shared) nodes.push(el('span', 'u', 'This view is not shared with the AI, so it cannot see this page.'));
    ctxLine.replaceChildren(...nodes);
    ctxLine.title = c.title + (c.host ? ' (' + c.host + ')' : '');
  }

  function setSide(side) {
    const hideLabel = 'Hide the chat column';
    hideBtn.setAttribute('aria-label', hideLabel);
    hideBtn.title = hideLabel;
    hideBtn.dataset.spec = 'Hides this column so the page takes the width. A slim button stays at the edge to bring it back. The AI keeps working; a question for you opens it again.';
    setHtml(hideBtn, html`${side === 'left' ? ICON.toLeft : ICON.toRight}`);
    const swapLabel = side === 'left' ? 'Move the chat to the right' : 'Move the chat to the left';
    sideBtn.setAttribute('aria-label', swapLabel);
    sideBtn.title = swapLabel;
  }

  return {
    root: body,
    input: ta,
    setContext,
    setSide,
    focusInput() {
      ta.focus();
    },
    /* the column became visible: the thread follows the newest line */
    shown() {
      scroll.scrollTop = scroll.scrollHeight;
    },
    start() {
      return view.start();
    },
    dispose() {
      disposed = true;
      if (typeof offControl === 'function') offControl();
      offControl = null;
      if (controlUi) controlUi.dispose();
      controlUi = null;
    },
  };
}
