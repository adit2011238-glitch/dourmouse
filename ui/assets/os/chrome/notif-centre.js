/* The Notification Centre. Server alerts come from GET /api/alerts (the same
   store the console bell reads) and a dismiss is a real POST
   /api/state/alerts {action:'dismiss', id}: the mockup's Clear only removed a
   CSS class. This session's own notices (toasts, including ones Do Not
   Disturb silenced) sit below under their own heading. Live through
   state_change section "alerts", re-read on resync and on a slow poll. */

import { html, setHtml } from '../kit/html.js';
import { icon } from '../kit/icons.js';
import { ago, toMs } from '../kit/format.js';

const POLL_MS = 60000;
const SEEN_KEY = 'dm.os.alertsSeen';

export function createNotifCentre({ root, api, events, toasts, prefs, dock, timers }) {
  const st = { alerts: [], loaded: false, error: '', busy: false, seen: 0 };
  try {
    st.seen = Number(prefs.read(SEEN_KEY)) || 0;
  } catch (_err) {
    st.seen = 0;
  }
  let open = false;

  setHtml(root, html`
    <div class="cc-head"><span>Notifications</span><button type="button" class="nc-clear" id="ncClear" data-spec="Dismisses every alert listed here. Each one is a real dismiss on the server, not a style change.">Clear</button></div>
    <div id="nclist" aria-live="polite"></div>`);
  const list = root.querySelector('#nclist');
  const clear = root.querySelector('#ncClear');

  const active = () => st.alerts.filter((a) => !a.dismissed);

  function badge() {
    dock.setBadge(active().length);
  }

  function item({ id, title, detail, when, unread, tone, kind, dismiss }) {
    const stroke = tone === 'bad' ? 'var(--dm-error)' : tone === 'warn' ? 'var(--os-warn)' : 'currentColor';
    return html`<div class="nc-item${unread ? ' unread' : ''}" data-alert="${id}"><div class="ic">${icon(kind === 'security' ? 'SHIELD' : kind === 'agent' ? 'AGENTS' : 'BELL', '', { stroke, width: 1.6 })}</div><div class="bd"><div class="nt">${title}</div>${detail ? html`<div class="nm">${detail}</div>` : ''}</div><div><div class="tm">${when}</div>${dismiss ? html`<button type="button" class="nx" data-dismiss="${id}" aria-label="Dismiss: ${title}">&times;</button>` : ''}</div></div>`;
  }

  function paint() {
    const alerts = active();
    const local = toasts.local();
    clear.disabled = !alerts.length && !local.length;
    const parts = [];
    if (st.error) {
      parts.push(html`<div class="st st-error" role="alert"><div class="st-t">Could not read alerts</div><div class="st-m">${st.error}</div></div>`);
    } else if (!st.loaded) {
      parts.push(html`<div class="st st-loading" role="status"><span class="st-spin" aria-hidden="true"></span><span>Loading alerts</span></div>`);
    }
    alerts.forEach((a) => {
      parts.push(item({
        id: a.id, title: a.title, detail: a.detail, when: ago(a.created), kind: a.kind,
        unread: toMs(a.created) > st.seen || !Number.isFinite(toMs(a.created)),
        tone: a.severity === 'high' ? 'bad' : a.severity === 'med' ? 'warn' : '',
        dismiss: true,
      }));
    });
    if (local.length) {
      parts.push(html`<div class="cc-head">This session</div>`);
      local.slice(0, 12).forEach((n) => parts.push(item({ id: n.id, title: n.title, detail: n.detail, when: ago(n.at), unread: false, tone: n.level === 'error' ? 'bad' : n.level === 'warn' ? 'warn' : '', kind: 'note', dismiss: false })));
    }
    if (st.loaded && !st.error && !alerts.length && !local.length) {
      parts.push(html`<div class="st st-empty"><div class="st-m">No alerts.</div><div class="st-d">Security findings, risky downloads and agent notices appear here.</div></div>`);
    }
    setHtml(list, html`${parts}`);
    badge();
  }

  async function load() {
    try {
      const d = await api.get('/api/alerts');
      st.alerts = Array.isArray(d.alerts) ? d.alerts : [];
      st.loaded = true;
      st.error = '';
    } catch (err) {
      st.loaded = true;
      st.error = err && err.message ? err.message : String(err);
    }
    paint();
  }

  async function dismiss(ids) {
    st.busy = true;
    const failed = [];
    for (const id of ids) {
      try {
        const r = await api.post('/api/state/alerts', { action: 'dismiss', id: Number(id) });
        if (r && r.ok === false) failed.push(id);
      } catch (err) {
        failed.push(id);
        st.error = err && err.message ? err.message : String(err);
        break;
      }
    }
    st.busy = false;
    await load();
    if (failed.length) toasts.show({ level: 'warn', title: 'Alert not dismissed', detail: st.error || failed.length + ' alert(s) could not be dismissed on the server.' });
  }

  list.addEventListener('click', (e) => {
    const b = e.target.closest('[data-dismiss]');
    if (b) dismiss([b.dataset.dismiss]);
  });
  clear.addEventListener('click', async () => {
    const ids = active().map((a) => a.id);
    toasts.clearLocal();
    if (ids.length) await dismiss(ids);
    else paint();
  });

  events.on((e) => e.type === 'state_change' && e.section === 'alerts', () => load());
  events.onResync(() => load());
  toasts.onLocal(() => {
    if (open) paint();
  });
  timers.every(POLL_MS, () => load(), 'chrome');

  return {
    load,
    paint,
    onOpen() {
      open = true;
      load();
    },
    /* Closing the panel is what marks the alerts as seen. */
    onClose() {
      open = false;
      const newest = active().reduce((m, a) => Math.max(m, Number.isFinite(toMs(a.created)) ? toMs(a.created) : 0), st.seen);
      st.seen = newest;
      prefs.write(SEEN_KEY, String(newest));
      paint();
    },
  };
}
