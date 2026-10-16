/* BROWSER split view: the layout.

   The real browser takes about 80 percent of the screen on one side and a chat column about 20 percent on the
   other (right by default; the side is a preference). A draggable divider sets the share (minimum 280 pixels,
   maximum 45 percent, double click goes back to 20 percent), a button hides the column, and under 900 pixels of
   screen width the column is a button-toggled overlay instead of squeezing the page. The ratio, the side and the
   collapsed state are kept in the prefs store.

   The Electron view is composited ABOVE this page's DOM, so the one duty that matters here is clip(): the
   rectangle the native view may cover is the page's own region (.bw-left) and, while the overlay is open, never
   the overlay. index.js hands that clip to viewBounds(), follows every layout change through onLayout(), and
   hides the view while the divider is dragged (a native view would swallow the pointer). */

import { setHtml, html } from '../../kit/html.js';
import { createChatColumn, ICON } from './split-chat.js';
import {
  DEFAULT_RATIO, MIN_CHAT_PX, MAX_RATIO, RAIL_PX, parseStoredRatio, parseStoredSide, parseStoredFlag,
  layoutFor, toggled, forcedOpen, dragPx, keyPx, paneClip, ratioFromPx,
} from './split-model.js';

const CSS_HREF = '/assets/os/screens/browser/split.css';
const CSS_WAIT_MS = 1500;

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

/* The split's stylesheet is its own file (the router loads only browser.css). Resolves when it is applied, or
   after a short wait, so the first measurement is made with the layout in place. */
export function loadSplitStyles() {
  const have = document.querySelector('link[data-split-css]');
  if (have) return Promise.resolve();
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = CSS_HREF;
  link.dataset.splitCss = '1';
  return new Promise((resolve) => {
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      resolve();
    };
    link.addEventListener('load', finish);
    link.addEventListener('error', finish);
    setTimeout(finish, CSS_WAIT_MS);
    document.head.append(link);
  });
}

export function removeSplitStyles() {
  document.querySelectorAll('link[data-split-css]').forEach((n) => n.remove());
}

/* An Alt+J typed in a text field (the keymap ignores those) */
function isFocusChatKey(e) {
  return Boolean(e.altKey) && !e.ctrlKey && !e.metaKey && !e.shiftKey && (String(e.key).toLowerCase() === 'j' || e.code === 'KeyJ');
}

/* ctx: the screen's ctx. root: the screen's root. wrap and foot: the page's frame and its footnote row, moved
   into the left region. fields: text fields that should answer Alt+J. control, note, shared: see split-chat.js.
   onLayout(): the layout changed, re-place the native view. onDragging(on): the divider is being dragged. */
export function createSplit({ ctx, root, wrap, foot, fields = [], control = null, note, shared, onLayout, onDragging }) {
  let side = parseStoredSide(ctx.prefs.get('splitSide'));
  let ratio = parseStoredRatio(ctx.prefs.get('splitRatio'));
  let collapsed = parseStoredFlag(ctx.prefs.get('splitCollapsed'));
  let overlayOpen = false;
  let liveRatio = null; /* while the divider is dragged */
  let layout = layoutFor({ total: 0, ratio, collapsed, overlayOpen });
  let total = 0;
  let disposed = false;
  let offEsc = null;
  let lastSig = '';

  /* ---------------- structure ---------------- */
  const splitEl = el('div', 'bw-split');
  const leftEl = el('div', 'bw-left');
  const div = el('div', 'bw-sdiv');
  div.setAttribute('role', 'separator');
  div.setAttribute('aria-orientation', 'vertical');
  div.setAttribute('aria-label', 'Width of the chat column');
  div.tabIndex = 0;
  div.dataset.spec = 'Drag to change how much of the screen the chat column takes (at least 280 pixels, at most 45 percent). Double click to go back to 20 percent. The left and right arrow keys move it too (Shift for bigger steps).';
  const chatEl = el('aside', 'bw-chat');
  chatEl.setAttribute('aria-label', 'AI chat');
  const rail = el('button', 'bw-rail');
  rail.type = 'button';
  const railT = el('span', 't', 'Ask the AI');
  const railDot = el('span', 'd');
  railDot.setAttribute('aria-hidden', 'true');

  const col = createChatColumn({
    ctx, control, note, shared,
    onHide: () => toggle(),
    onSwapSide: () => swapSide(),
    onActivity: (s) => {
      railDot.dataset.busy = s.busy ? '1' : '0';
      railDot.dataset.attn = s.attention ? '1' : '0';
      if (s.attention) ensureVisible();
    },
  });
  setHtml(rail, html`${ICON.chat}`);
  rail.append(railT, railDot);
  chatEl.append(rail, col.root);

  root.insertBefore(splitEl, wrap);
  leftEl.append(wrap, foot);
  splitEl.append(leftEl, div, chatEl);
  col.setSide(side);

  /* ---------------- layout ---------------- */
  function apply() {
    if (disposed) return;
    total = Math.floor(splitEl.getBoundingClientRect().width);
    layout = layoutFor({ total, ratio: liveRatio === null ? ratio : liveRatio, collapsed, overlayOpen });
    const visible = layout.mode === 'split' || layout.mode === 'overlay-open';
    splitEl.dataset.mode = layout.mode;
    splitEl.dataset.side = side;
    splitEl.dataset.chatVisible = visible ? '1' : '0';
    splitEl.style.setProperty('--bw-chat-w', layout.chatPx + 'px');
    const open = layout.mode === 'overlay-open' || layout.mode === 'split';
    rail.setAttribute('aria-expanded', String(open));
    const railLabel = open ? 'Hide the AI chat' : 'Show the AI chat';
    rail.setAttribute('aria-label', railLabel);
    rail.title = railLabel;
    rail.dataset.spec = open
      ? 'Closes the AI chat over the page.'
      : 'Opens the chat column where you tell the AI what to do in this browser. A question that needs your answer opens it by itself.';
    div.setAttribute('aria-valuemin', String(MIN_CHAT_PX));
    div.setAttribute('aria-valuemax', String(Math.floor(total * MAX_RATIO)));
    div.setAttribute('aria-valuenow', String(layout.chatPx));
    div.setAttribute('aria-valuetext', total ? Math.round((layout.chatPx / total) * 100) + ' percent of the screen' : '');
    const sig = [layout.mode, side, layout.chatPx, total].join('|');
    if (sig !== lastSig) {
      lastSig = sig;
      if (visible) col.shown();
    }
    if (layout.mode === 'overlay-open' && !offEsc) offEsc = ctx.keys.pushEsc(() => closeOverlay());
    if (layout.mode !== 'overlay-open' && offEsc) {
      offEsc();
      offEsc = null;
    }
    if (onLayout) onLayout();
  }

  function state() {
    return { total, collapsed, overlayOpen };
  }

  function closeOverlay() {
    overlayOpen = false;
    apply();
  }

  function toggle() {
    const n = toggled(state());
    const wasHidden = layout.mode === 'rail' || layout.mode === 'overlay-closed';
    collapsed = n.collapsed;
    overlayOpen = n.overlayOpen;
    if (total >= 900) ctx.prefs.set('splitCollapsed', collapsed ? '1' : '0');
    apply();
    if (wasHidden) col.focusInput();
  }

  /* a question for the owner, or the shortcut: show the column without rewriting what the owner chose */
  function ensureVisible() {
    const n = forcedOpen(state());
    if (!n) return false;
    collapsed = n.collapsed;
    overlayOpen = n.overlayOpen;
    apply();
    return true;
  }

  function swapSide() {
    side = side === 'right' ? 'left' : 'right';
    ctx.prefs.set('splitSide', side);
    col.setSide(side);
    apply();
  }

  function focusChat() {
    ensureVisible();
    col.focusInput();
  }

  /* ---------------- the divider ---------------- */
  let drag = null;
  function liveFrom(e) {
    const r = splitEl.getBoundingClientRect();
    const px = dragPx({ pointer: e.clientX, rectLeft: r.left, rectRight: r.right, total: Math.floor(r.width), side });
    return px / Math.max(1, Math.floor(r.width));
  }
  div.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 || layout.mode !== 'split') return;
    try {
      div.setPointerCapture(e.pointerId);
    } catch (err) {
      /* a synthetic pointer cannot be captured; the drag still works */
    }
    drag = { id: e.pointerId };
    div.classList.add('drag');
    splitEl.dataset.dragging = '1';
    if (onDragging) onDragging(true);
    e.preventDefault();
  });
  div.addEventListener('pointermove', (e) => {
    if (!drag) return;
    liveRatio = liveFrom(e);
    apply();
  });
  const endDrag = () => {
    if (!drag) return;
    drag = null;
    div.classList.remove('drag');
    delete splitEl.dataset.dragging;
    if (liveRatio !== null) {
      ratio = parseStoredRatio(ratioFromPx(Math.round(liveRatio * total), total));
      ctx.prefs.set('splitRatio', ratio);
      liveRatio = null;
    }
    apply();
    if (onDragging) onDragging(false);
  };
  div.addEventListener('pointerup', endDrag);
  div.addEventListener('pointercancel', endDrag);
  div.addEventListener('dblclick', () => {
    ratio = DEFAULT_RATIO;
    ctx.prefs.set('splitRatio', ratio);
    apply();
  });
  div.addEventListener('keydown', (e) => {
    if (e.key === 'Home') {
      ratio = DEFAULT_RATIO;
      ctx.prefs.set('splitRatio', ratio);
      apply();
      e.preventDefault();
      return;
    }
    const px = keyPx({ key: e.key, px: layout.chatPx, total, side, shift: e.shiftKey });
    if (px === null) return;
    ratio = parseStoredRatio(ratioFromPx(px, total));
    ctx.prefs.set('splitRatio', ratio);
    apply();
    e.preventDefault();
  });

  rail.addEventListener('click', () => toggle());

  /* ---------------- keyboard ---------------- */
  ctx.keys.bind('Alt+j', () => focusChat());
  fields.forEach((f) => {
    f.addEventListener('keydown', (e) => {
      if (!isFocusChatKey(e)) return;
      e.preventDefault();
      focusChat();
    });
  });

  const ro = new ResizeObserver(() => apply());
  ro.observe(splitEl);
  apply();
  col.start().catch(() => {
    /* the thread view reports its own failure in the column */
  });

  return {
    /* what the native view may cover: base is the visible scroll area of the screen */
    clip(base) {
      const left = leftEl.getBoundingClientRect();
      const overlay = layout.mode === 'overlay-open' ? col.root.getBoundingClientRect() : null;
      return paneClip({ base, left, overlay, side });
    },
    /* the width the page frame can use (for its own width presets) */
    availWidth: () => leftEl.clientWidth,
    setContext: (v) => col.setContext(v),
    focusChat,
    mode: () => layout.mode,
    dispose() {
      if (disposed) return;
      disposed = true;
      ro.disconnect();
      if (offEsc) offEsc();
      offEsc = null;
      col.dispose();
      removeSplitStyles();
    },
  };
}
