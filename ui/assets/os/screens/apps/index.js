/* APPS: which other Mac apps the model may drive, and the macOS permissions
   Dourmouse uses. Every value is a real read of GET /api/os/apps/status and
   GET /api/os/apps/allowed. A read that fails says so in the server's words;
   nothing is filled in.

   Allowing an app, removing one and resuming after a stop are owner-only
   routes. They go through ctx.api, which carries the owner cookie, and when
   the server answers 403 "owner only" this screen says exactly that instead of
   pretending it worked. Allowing and resuming open a card that says what will
   happen first; only APPROVE sends. STOP needs no card: stopping is always
   safe, and anyone may do it.

   The permissions guide opens System Settings through the host's own
   openExternal. Today that only takes web links, so when it cannot open the
   pane the screen says where to click instead. */

import { html, setHtml } from '../../kit/html.js';
import { states } from '../../kit/states.js';
import { confirmHere } from '../../kit/confirm-card.js';
import { agoLabel, clock } from '../../kit/format.js';
import { isAbort } from '../../core/api.js';
import { trustView, killView, runningRows, logRows, errorText, allowPrompt, resumePrompt } from './helpers.js';
import { PERMISSIONS, openPane, readSeen, markSeen } from './walkthrough.js';

const reloaders = new WeakMap();

export default {
  id: 'APPS',
  sub: 'app driving and permissions',
  css: true,
  thread: false,

  async mount(root, ctx) {
    const st = { status: null, statusError: null, list: null, listError: null, paneNote: '' };

    root.dataset.state = 'loading';
    setHtml(root, html`
      <div class="apps-note" id="appsNote" role="status" hidden></div>
      <div id="appsConfirm"></div>
      <div class="grid2">
        <div class="card"><div class="lbl">Accessibility permission</div><div id="appsTrust" data-region></div></div>
        <div class="card"><div class="lbl">Kill switch</div><div id="appsKill" data-region></div></div>
      </div>
      <div class="card apps-gap"><div class="lbl">Permissions guide</div><div id="appsGuide" data-region></div></div>
      <div class="card apps-gap"><div class="lbl">Allowed apps</div><div id="appsAllowed" data-region></div></div>
      <div class="card apps-gap"><div class="lbl">Running apps</div><div id="appsRunning" data-region></div></div>
      <div class="card apps-gap"><div class="lbl">Recent driving log</div><div id="appsLog" data-region></div></div>`);
    const $ = (id) => root.querySelector('#' + id);
    const noteEl = $('appsNote');
    const confirmEl = $('appsConfirm');
    const guideEl = $('appsGuide');
    const trustEl = $('appsTrust');
    const killEl = $('appsKill');
    const allowedEl = $('appsAllowed');
    const runningEl = $('appsRunning');
    const logEl = $('appsLog');

    const note = (text, tone) => {
      noteEl.hidden = !text;
      noteEl.textContent = text || '';
      noteEl.dataset.tone = tone || '';
    };

    /* ---------------- stage bar ---------------- */
    function paintActions() {
      const engaged = Boolean(st.status && st.status.kill && st.status.kill.engaged);
      ctx.chrome.setActions([
        {
          id: 'refresh', label: 'REFRESH',
          spec: 'Reads the permission, the allow list, the running apps and the log again. It changes nothing.',
          onClick: () => reload(),
        },
        {
          id: 'stop', label: 'STOP DRIVING', kind: 'danger', disabled: engaged || !st.status,
          spec: 'Engages the kill switch now: the model cannot drive any app until you resume it here. Always safe, and open to anyone.',
          onClick: () => stopDriving(),
        },
      ]);
    }

    /* ---------------- reads ---------------- */
    async function loadStatus() {
      try {
        const data = await ctx.api.get('/api/os/apps/status');
        if (ctx.signal.aborted) return;
        st.status = data;
        st.statusError = null;
      } catch (err) {
        if (isAbort(err)) return;
        st.statusError = err;
      }
      paintStatus();
    }

    async function loadList() {
      try {
        const data = await ctx.api.get('/api/os/apps/allowed');
        if (ctx.signal.aborted) return;
        st.list = data;
        st.listError = null;
      } catch (err) {
        if (isAbort(err)) return;
        st.listError = err;
      }
      paintList();
    }

    async function reload() {
      [trustEl, killEl, allowedEl, runningEl, logEl].forEach((el) => states.loading(el, 'Reading app driving'));
      await Promise.all([loadStatus(), loadList()]);
    }

    /* ---------------- painting ---------------- */
    function settle() {
      const bad = st.statusError && !st.status && st.listError && !st.list;
      root.dataset.state = bad ? 'error' : st.status || st.list ? 'populated' : 'loading';
    }

    function paintGuide() {
      guideEl.dataset.state = 'populated';
      setHtml(guideEl, html`<div class="muted apps-help">Dourmouse uses these macOS permissions and no others. Each one is asked for by macOS itself, only when a feature needs it, and you can switch any of them off in System Settings.</div>
        <div class="apps-perms">${PERMISSIONS.map((p) => html`<div class="apps-perm" data-perm="${p.id}">
          <div class="apps-perm-h"><b>${p.name}</b> <span class="tag ${p.used ? 'ok' : ''}">${p.uses}</span></div>
          <div class="muted apps-help">${p.enables}</div>
          ${p.how ? html`<div class="muted apps-help">${p.how}</div>` : ''}
          <div class="apps-btns"><button type="button" class="os-btn" data-pane="${p.id}" ${p.used ? '' : 'disabled'} data-spec="${p.used ? 'Opens the ' + p.paneName + ' page of System Settings so you can switch Dourmouse on. It changes no permission by itself.' : 'Nothing in Dourmouse uses ' + p.name + ' today, so there is nothing to switch on.'}">${p.used ? 'OPEN ' + p.name.toUpperCase() + ' SETTINGS' : 'NOT NEEDED'}</button></div>
          <div class="apps-pane-note muted" data-pane-note="${p.id}" role="status" hidden></div>
        </div>`)}</div>`);
    }

    function paintStatus() {
      if (!st.status) {
        if (st.statusError) {
          [trustEl, killEl, logEl].forEach((el) => states.error(el, st.statusError, { title: 'Could not read app driving', retry: () => reload() }));
        }
        paintActions();
        settle();
        return;
      }
      const t = trustView(st.status);
      trustEl.dataset.state = 'populated';
      setHtml(trustEl, html`<div class="kv"><span>Accessibility</span><b><span class="tag ${t.tone}">${t.word}</span></b></div>
        <div class="muted apps-help">${t.text}</div>
        ${t.backend ? html`<div class="kv"><span>Read through</span><b class="mono">${t.backend}</b></div>` : ''}
        <div class="muted apps-help">Accessibility is the macOS permission that lets one app press buttons and read text in another. Dourmouse needs it to drive or control any other app.</div>
        <div class="apps-btns"><button type="button" class="os-btn" data-pane="accessibility" data-spec="Opens the Accessibility page of System Settings so you can switch Dourmouse on. It changes no permission by itself.">OPEN ACCESSIBILITY SETTINGS</button></div>
        <div class="apps-pane-note muted" data-pane-note="accessibility-top" role="status" hidden></div>`);

      const k = killView(st.status.kill);
      killEl.dataset.state = 'populated';
      setHtml(killEl, html`<div class="kv"><span>Kill switch</span><b><span class="tag ${k.engaged ? 'bad' : 'ok'}">${k.word}</span></b></div>
        <div class="muted apps-help">${k.line}</div>
        ${k.engaged ? html`<div class="apps-btns"><button type="button" class="os-btn" data-resume data-spec="Releases the kill switch after asking. Only the owner can resume; the model has no way to.">RESUME</button></div>` : ''}
        <div class="muted apps-help">Only you can resume driving, and only from the Dourmouse app window. The model can stop itself but never resume.</div>`);

      const rows = logRows(st.status.recent);
      if (!rows.length) {
        states.empty(logEl, 'Nothing has been driven since Dourmouse started.', { hint: 'This list is kept in memory for the current run. The permanent record is in the office event log.' });
      } else {
        logEl.dataset.state = 'populated';
        setHtml(logEl, html`<div class="muted apps-help">The last ${rows.length} driving events since Dourmouse started. Typed text is never recorded.</div>
          ${rows.map((r) => html`<div class="os-row"><span class="tag ${r.tone}">${r.kind || 'event'}</span><span class="rt">${r.text}${r.detail ? html` <span class="muted">${r.detail}</span>` : ''}</span><span class="muted" title="${clock(r.at)}">${agoLabel(r.at)}</span></div>`)}`);
      }
      paintActions();
      settle();
    }

    function paintList() {
      if (!st.list) {
        if (st.listError) {
          states.error(allowedEl, st.listError, { title: 'Could not read the allow list', retry: () => reload() });
          states.error(runningEl, st.listError, { title: 'Could not read the running apps', retry: () => reload() });
        }
        settle();
        return;
      }
      const allowed = Array.isArray(st.list.allowed) ? st.list.allowed : [];
      if (!allowed.length) {
        states.empty(allowedEl, 'No app is allowed.', { hint: 'The model cannot read or operate any app until you allow one below.' });
      } else {
        allowedEl.dataset.state = 'populated';
        setHtml(allowedEl, html`${allowed.map((a, i) => html`<div class="os-row"><span class="rt"><b>${a.name || a.bundle_id}</b>${a.bundle_id ? html` <span class="muted mono">${a.bundle_id}</span>` : ''}<span class="muted apps-block">Allowed ${agoLabel(a.added_at) || 'earlier'}${a.added_by ? ' by ' + a.added_by : ''}</span></span><button type="button" class="os-btn" data-remove="${i}" data-spec="Takes ${a.name || a.bundle_id} off the allow list at once. The model can no longer read or operate it. Owner only.">REMOVE</button></div>`)}`);
      }

      const rows = runningRows(st.list.running);
      if (st.list.running_error) {
        states.error(runningEl, new Error(st.list.running_error), { title: 'Could not list the running apps' });
      } else if (!rows.length) {
        states.empty(runningEl, 'No running apps were reported.');
      } else {
        runningEl.dataset.state = 'populated';
        setHtml(runningEl, html`<div class="muted apps-help">Apps open on this Mac now. Allowing one lets the model see its text and buttons. A greyed app can never be driven, and the reason is shown.</div>
          ${rows.map((r, i) => html`<div class="os-row"><span class="rt"><b>${r.name}</b>${r.bundle_id ? html` <span class="muted mono">${r.bundle_id}</span>` : ''}${r.denied ? html`<span class="muted apps-block">Never allowed: ${r.reason}</span>` : ''}</span>${r.allowed ? html`<span class="tag ok">allowed</span>` : html`<button type="button" class="os-btn" data-allow="${i}" ${r.denied ? 'disabled' : ''} data-spec="${r.denied ? 'This app is on the hard deny list: ' + r.reason + '.' : 'Opens a card that says what allowing ' + r.name + ' lets the model do. Nothing is allowed until you approve. Owner only.'}">ALLOW</button>`}</div>`)}`);
      }
      settle();
    }

    /* ---------------- actions ---------------- */
    function showPaneNote(id, text) {
      const el = root.querySelector('[data-pane-note="' + id + '"]');
      if (!el) return;
      el.hidden = !text;
      el.textContent = text || '';
    }

    function onPane(id) {
      const perm = PERMISSIONS.find((p) => p.id === id);
      if (!perm || !perm.used) return;
      const result = openPane(ctx, perm);
      showPaneNote(id, result.text);
      if (id === 'accessibility') showPaneNote('accessibility-top', result.text);
    }

    async function stopDriving() {
      try {
        await ctx.api.post('/api/os/apps/kill', { reason: 'Stopped from the APPS screen' });
        note('App driving is stopped. Resume it here when you want it back.', 'ok');
      } catch (err) {
        if (isAbort(err)) return;
        note('Could not stop app driving: ' + errorText(err), 'error');
      }
      await loadStatus();
    }

    async function removeApp(i) {
      const entry = st.list && Array.isArray(st.list.allowed) ? st.list.allowed[i] : null;
      if (!entry) return;
      try {
        await ctx.api.post('/api/os/apps/deny', { app: entry.name || entry.bundle_id, bundle_id: entry.bundle_id || undefined });
        note((entry.name || entry.bundle_id) + ' is off the allow list. The model can no longer drive it.', 'ok');
      } catch (err) {
        if (isAbort(err)) return;
        note('Could not remove ' + (entry.name || entry.bundle_id) + ': ' + errorText(err), 'error');
      }
      await loadList();
    }

    function allowApp(i) {
      const rows = runningRows(st.list && st.list.running);
      const row = rows[i];
      if (!row || row.denied || row.allowed) return;
      confirmHere(confirmEl, allowPrompt(row), async () => {
        try {
          await ctx.api.post('/api/os/apps/allow', { app: row.name, bundle_id: row.bundle_id || undefined });
        } catch (err) {
          throw new Error(errorText(err));
        }
        note(row.name + ' is allowed. The model can now read it and, with your approval, operate it.', 'ok');
        await loadList();
        await loadStatus();
      });
    }

    function resume() {
      confirmHere(confirmEl, resumePrompt(), async () => {
        try {
          await ctx.api.post('/api/os/apps/resume', {});
        } catch (err) {
          throw new Error(errorText(err));
        }
        note('App driving is resumed.', 'ok');
        await loadStatus();
      });
    }

    root.addEventListener('click', (e) => {
      const p = e.target.closest('[data-pane]');
      if (p) return onPane(p.dataset.pane);
      const rm = e.target.closest('[data-remove]');
      if (rm) return removeApp(Number(rm.dataset.remove));
      const al = e.target.closest('[data-allow]');
      if (al) return allowApp(Number(al.dataset.allow));
      if (e.target.closest('[data-resume]')) return resume();
      return undefined;
    });

    /* the indicator event fires on every action, stop and resume: re-read the status and the log, coalesced */
    let pending = false;
    ctx.events.on('app_driver_indicator', () => {
      if (pending) return;
      pending = true;
      loadStatus().finally(() => { pending = false; });
    });
    ctx.events.onResync(() => reload());

    reloaders.set(ctx, reload);
    paintActions();
    paintGuide();
    [trustEl, killEl, allowedEl, runningEl, logEl].forEach((el) => states.loading(el, 'Reading app driving'));
    /* opening the guide counts as having seen it, so HOME stops offering it */
    readSeen(ctx).then((seen) => (seen === false ? markSeen(ctx) : ''));
    await Promise.all([loadStatus(), loadList()]);
  },

  async refresh(ctx, reason) {
    const fn = reloaders.get(ctx);
    if (fn && reason !== 'show') fn();
  },
};
