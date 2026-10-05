/* In-app toasts. Do Not Disturb silences exactly these and nothing else:
   Electron raises native notifications from its own /api/events listener
   (electron/main.js), which this page cannot reach. Every notification is
   still recorded (local ring, cap 50) so the Notification Centre shows it even
   while toasts are silenced. Auto-dismiss rides one permanent sweep timer.

   S5 and S33: a toast can carry one action button ({ label, onClick }). An
   action marked undo: true is also run by Command Z while that toast is on
   screen (toasts.undoLast). A toast waits while the pointer is on it or focus is
   inside it, and the stack is capped at three, with a "N more in Notifications"
   line when older ones were pushed off. */

import { html, toFragment } from '../kit/html.js';
import { ring } from '../core/ring.js';

const MAX_VISIBLE = 3;
const LOCAL_CAP = 50;
const HOLD_AFTER_LEAVE_MS = 3000;
const ACTION_TTL_MS = 10000;

export function createToasts({ mount, prefs, timers }) {
  const localItems = ring(LOCAL_CAP);
  const visible = [];
  const listeners = new Set();
  const visibleListeners = new Set();
  let seq = 0;
  let pushedOff = 0;

  const emit = () => listeners.forEach((fn) => {
    try {
      fn();
    } catch (err) {
      console.error(err);
    }
  });

  /* fires whenever the on-screen stack changes (shown, dismissed, expired) */
  const emitVisible = () => visibleListeners.forEach((fn) => {
    try {
      fn(visible.length);
    } catch (err) {
      console.error(err);
    }
  });

  function runAction(t) {
    api.dismiss(t.id);
    try {
      const r = t.action.onClick();
      if (r && typeof r.catch === 'function') r.catch((err) => console.error(err));
    } catch (err) {
      console.error(err);
    }
  }

  function paint() {
    emitVisible();
    if (!visible.length) pushedOff = 0;
    const nodes = visible.map((t) => {
      const el = toFragment(html`<div class="toast" data-level="${t.level}" data-toast="${t.id}"><div class="bd"><div class="tt">${t.title}</div>${t.detail ? html`<div class="td">${t.detail}</div>` : ''}</div>${t.action ? html`<button type="button" class="ta" data-spec="${t.action.spec || 'Runs the action this notice offers.'}">${t.action.label}</button>` : ''}<button type="button" class="nx" aria-label="Dismiss notification">&times;</button></div>`).firstElementChild;
      el.querySelector('.nx').addEventListener('click', () => api.dismiss(t.id));
      if (t.action) el.querySelector('.ta').addEventListener('click', () => runAction(t));
      /* a toast waits while it is being read: the pointer on it, or focus inside it */
      const hold = () => { t.held = true; };
      const release = () => {
        t.held = false;
        t.expires = Math.max(t.expires, Date.now() + HOLD_AFTER_LEAVE_MS);
      };
      el.addEventListener('mouseenter', hold);
      el.addEventListener('mouseleave', release);
      el.addEventListener('focusin', hold);
      el.addEventListener('focusout', release);
      return el;
    });
    if (pushedOff && visible.length) {
      const more = document.createElement('div');
      more.className = 'toast-more';
      more.textContent = '+' + pushedOff + ' more in Notifications';
      nodes.push(more);
    }
    mount.replaceChildren(...nodes);
  }

  const api = {
    /* level: info | ok | warn | error. action: { label, onClick, undo?, spec? } */
    show({ level = 'info', title = '', detail = '', ttl, action = null } = {}) {
      seq += 1;
      const life = Number.isFinite(ttl) ? ttl : action ? ACTION_TTL_MS : 6000;
      const item = { id: 'n' + seq, level, title: String(title), detail: String(detail), at: Date.now() };
      localItems.push(item);
      const silenced = prefs.dnd();
      /* a silenced toast with an action is never shown, so the action cannot be run by accident later */
      if (!silenced) {
        visible.push({ ...item, expires: Date.now() + life, held: false, action: action && typeof action.onClick === 'function' ? action : null });
        while (visible.length > MAX_VISIBLE) {
          visible.shift();
          pushedOff += 1;
        }
        paint();
      }
      emit();
      return { id: item.id, silenced };
    },
    dismiss(id) {
      const i = visible.findIndex((t) => t.id === id);
      if (i >= 0) {
        visible.splice(i, 1);
        paint();
      }
    },
    /* Command Z: run the newest on-screen action marked undo. Returns whether one ran. */
    undoLast() {
      for (let i = visible.length - 1; i >= 0; i -= 1) {
        const t = visible[i];
        if (t.action && t.action.undo) {
          runAction(t);
          return true;
        }
      }
      return false;
    },
    hasUndo: () => visible.some((t) => t.action && t.action.undo),
    /* this session's notifications, newest first (for the Notification Centre) */
    local: () => localItems.newestFirst(),
    clearLocal() {
      localItems.clear();
      emit();
    },
    /* put back what clearLocal removed (the Undo on Clear). Items are oldest first. */
    restoreLocal(items) {
      localItems.clear();
      (items || []).forEach((it) => localItems.push(it));
      emit();
    },
    onLocal(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    visibleCount: () => visible.length,
    onVisible(fn) {
      visibleListeners.add(fn);
      return () => visibleListeners.delete(fn);
    },
    visibleListenerCount: () => visibleListeners.size,
    count: () => listeners.size,
    sweep() {
      const now = Date.now();
      const keep = visible.filter((t) => t.held || t.expires > now);
      if (keep.length !== visible.length) {
        visible.splice(0, visible.length, ...keep);
        paint();
      }
    },
    /* Silencing on mid-flight clears what is on screen. */
    hideAll() {
      visible.length = 0;
      paint();
    },
  };
  timers.every(1000, () => api.sweep(), 'chrome');
  return api;
}
