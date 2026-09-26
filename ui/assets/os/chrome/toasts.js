/* In-app toasts. Do Not Disturb silences exactly these and nothing else:
   Electron raises native notifications from its own /api/events listener
   (electron/main.js), which this page cannot reach. Every notification is
   still recorded (local ring, cap 50) so the Notification Centre shows it even
   while toasts are silenced. Auto-dismiss rides one permanent sweep timer. */

import { html, toFragment } from '../kit/html.js';
import { ring } from '../core/ring.js';

const MAX_VISIBLE = 4;
const LOCAL_CAP = 50;

export function createToasts({ mount, prefs, timers }) {
  const localItems = ring(LOCAL_CAP);
  const visible = [];
  const listeners = new Set();
  let seq = 0;

  const emit = () => listeners.forEach((fn) => {
    try {
      fn();
    } catch (err) {
      console.error(err);
    }
  });

  function paint() {
    mount.replaceChildren(
      ...visible.map((t) => {
        const el = toFragment(html`<div class="toast" data-level="${t.level}" data-toast="${t.id}"><div class="bd"><div class="tt">${t.title}</div>${t.detail ? html`<div class="td">${t.detail}</div>` : ''}</div><button type="button" class="nx" aria-label="Dismiss notification">&times;</button></div>`).firstElementChild;
        el.querySelector('.nx').addEventListener('click', () => api.dismiss(t.id));
        return el;
      }),
    );
  }

  const api = {
    /* level: info | ok | warn | error */
    show({ level = 'info', title = '', detail = '', ttl = 6000 } = {}) {
      seq += 1;
      const item = { id: 'n' + seq, level, title: String(title), detail: String(detail), at: Date.now() };
      localItems.push(item);
      const silenced = prefs.dnd();
      if (!silenced) {
        visible.push({ ...item, expires: Date.now() + ttl });
        while (visible.length > MAX_VISIBLE) visible.shift();
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
    /* this session's notifications, newest first (for the Notification Centre) */
    local: () => localItems.newestFirst(),
    clearLocal() {
      localItems.clear();
      emit();
    },
    onLocal(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    visibleCount: () => visible.length,
    count: () => listeners.size,
    sweep() {
      const now = Date.now();
      const keep = visible.filter((t) => t.expires > now);
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
