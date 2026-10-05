/* The driving strip (phase F2). While the model is driving another Mac app,
   a strip across the top of every screen says so and offers STOP. It is not a
   screen: it lives in the chrome so no screen can hide it or forget it.

   The truth comes from the server's app_driver indicator. It is read once on
   start and after every reconnect (GET /api/os/apps/status), and then it is
   pushed as an "app_driver_indicator" event on the one shared event stream, so
   nothing here polls. The server sends one more event when the ten second
   linger after the last action runs out, which is what hides the strip.

   STOP is POST /api/os/apps/kill. Anyone can engage the kill switch; only the
   owner can resume, and that button lives on the APPS screen. */

import { html, setHtml } from '../kit/html.js';

const ACTION_WORDS = { click: 'clicking', type: 'typing', press_key: 'pressing a key', scroll: 'scrolling' };

/* Pure: what the strip shows for an indicator event or status payload. */
export function stripView(evt) {
  const e = evt && typeof evt === 'object' ? evt : {};
  const app = typeof e.app === 'string' ? e.app.trim() : '';
  if (e.killed || !e.driving || !app) return { show: false, text: '', detail: '' };
  const word = e.active && e.action ? ACTION_WORDS[e.action] || '' : '';
  return { show: true, text: 'Model is driving ' + app, detail: word };
}

/* Pure: the status route's payload as an indicator event. */
export function eventFromStatus(status) {
  const s = status && typeof status === 'object' ? status : {};
  const ind = s.indicator && typeof s.indicator === 'object' ? s.indicator : {};
  const killed = Boolean(s.kill && s.kill.engaged);
  return { driving: Boolean(ind.driving) && !killed, killed, app: ind.app || null, action: ind.action || null, active: Boolean(ind.active) };
}

export function createDrivingStrip({ after, api, events, toasts, doc = globalThis.document }) {
  const el = doc.createElement('div');
  el.id = 'drivingstrip';
  el.setAttribute('role', 'status');
  el.setAttribute('aria-live', 'polite');
  el.hidden = true;
  after.after(el);
  let view = { show: false, text: '', detail: '' };
  let stopping = false;

  function paint() {
    el.hidden = !view.show;
    doc.body.dataset.driving = view.show ? 'true' : 'false';
    if (!view.show) {
      el.replaceChildren();
      return;
    }
    setHtml(el, html`<span class="ds-dot" aria-hidden="true"></span><span class="ds-text"><b>${view.text}</b>${view.detail ? html` <span class="ds-detail">${view.detail}</span>` : ''}</span><button type="button" class="os-btn os-btn--danger ds-stop" ${stopping ? 'disabled' : ''} data-spec="Stops the model from driving any app, now. The kill switch stays on until you resume it on the APPS screen.">${stopping ? 'STOPPING' : 'STOP'}</button>`);
  }

  function apply(evt) {
    view = stripView(evt);
    if (!view.show) stopping = false;
    paint();
  }

  async function stop() {
    if (stopping) return;
    stopping = true;
    paint();
    try {
      await api.post('/api/os/apps/kill', { reason: 'Stopped from the driving strip' });
      apply({ killed: true });
      toasts.show({ level: 'ok', title: 'App driving stopped', detail: 'The model cannot drive any app until you resume it on the APPS screen.' });
    } catch (err) {
      stopping = false;
      paint();
      toasts.show({ level: 'error', title: 'Could not stop app driving', detail: err && err.message ? err.message : String(err), ttl: 12000 });
    }
  }

  async function read() {
    try {
      apply(eventFromStatus(await api.get('/api/os/apps/status')));
    } catch (err) {
      /* a failed read says nothing: the strip only ever claims what the server told it */
      console.warn('driving strip status', err && err.message);
    }
  }

  el.addEventListener('click', (e) => {
    if (e.target.closest('.ds-stop')) stop();
  });
  events.on('app_driver_indicator', (evt) => apply(evt));
  events.onResync(() => read());

  return { el, read, apply, view: () => view };
}
