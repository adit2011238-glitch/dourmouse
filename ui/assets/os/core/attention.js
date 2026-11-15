/* The page's side of the attention feed (plan 1.8, finding 204). The server owns the items, the prefs and the
   rules for who is told (toast, native, quiet hours); this file reads the feed, keeps it current from the
   `attention` event, and turns "go" into a navigation. Nothing here decides whether to interrupt: the event
   carries `toast` already. */

import { NAV_HREF } from './router.js';

export function createAttention({ api, events, toasts, go }) {
  const st = { items: [], counts: { needs_you: 0, finished: 0, heads_up: 0, unread: 0, open: 0, screens: {} }, prefs: null, loaded: false, error: '' };
  const listeners = new Set();
  const emit = () => listeners.forEach((fn) => {
    try {
      fn(st);
    } catch (err) {
      console.error(err);
    }
  });

  async function load() {
    try {
      const d = await api.get('/api/os/attention');
      st.items = Array.isArray(d.items) ? d.items : [];
      st.counts = d.counts || st.counts;
      st.prefs = d.prefs || null;
      st.error = '';
    } catch (err) {
      st.error = err && err.message ? err.message : String(err);
    }
    st.loaded = true;
    emit();
  }

  /* only a hash the server built from validated parts is ever navigated to */
  function open(item) {
    if (!item || typeof item.href !== 'string' || !NAV_HREF.test(item.href)) return false;
    go(item.href);
    if (!item.read) markRead([item.id]);
    return true;
  }

  async function post(path, body) {
    try {
      const r = await api.post(path, body || {});
      if (r && r.counts) st.counts = r.counts;
      return r;
    } catch (err) {
      toasts.show({ level: 'warn', title: 'Could not update notifications', detail: err && err.message ? err.message : String(err) });
      return null;
    }
  }

  async function markRead(ids) {
    const r = await post('/api/os/attention/read', ids ? { ids } : {});
    if (r) await load();
    return r;
  }

  async function dismiss(id) {
    const r = await post('/api/os/attention/dismiss', { id });
    if (r) await load();
    return r;
  }

  async function clear() {
    const r = await post('/api/os/attention/clear');
    if (r) await load();
    return r;
  }

  const KIND_LEVEL = { needs_you: 'warn', finished: 'ok', heads_up: 'info' };

  events.on('attention', (evt) => {
    if (evt.action === 'added' && evt.item) {
      const item = evt.item;
      if (evt.toast) {
        toasts.show({
          level: KIND_LEVEL[item.kind] || 'info',
          title: item.title,
          detail: item.detail,
          ttl: item.kind === 'needs_you' ? 12000 : 7000,
          action: { label: 'Go', spec: 'Opens the place this notice is about.', onClick: () => open(item) },
        });
      }
    }
    load();
  });
  events.onResync(() => load());

  return {
    state: st,
    load,
    open,
    markRead,
    dismiss,
    clear,
    onChange(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    listenerCount: () => listeners.size,
    async setPrefs(change) {
      const r = await post('/api/os/attention/prefs', change);
      if (r && r.prefs) {
        st.prefs = r.prefs;
        emit();
      }
      return r;
    },
    test: () => post('/api/os/attention/test'),
  };
}
