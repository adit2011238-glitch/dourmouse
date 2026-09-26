/* SECURITY: the defensive posture of THIS Mac. Reads /api/security_dashboard
   (the sentry's last completed scan; it never triggers one), the lockdown
   state and the analyst's last answer. Live through security_* events, and it
   re-reads everything after the event stream reconnects.

   Nothing here changes the machine on its own. SCAN NOW and FULL REPORT only
   read. LOCK asks first, in a card, exactly as the console asks before it.
   REMEDIATE shows the sentry's suggestion and can hand the finding to HOME,
   where every change goes through the real approval gate. */

import { html, setHtml } from '../../kit/html.js';
import { states } from '../../kit/states.js';
import { flowSvg } from '../../kit/flow-svg.js';
import { ring } from '../../core/ring.js';
import { agoLabel } from '../../kit/format.js';
import { isAbort } from '../../core/api.js';
import {
  SEV_TAG, SEV_WORD, RATING, findings, flowBoxes, flowLegend, macFacts, macTag,
  lockdownLine, lockPrompt, remediationDirective, activityLine, activityRow,
} from './helpers.js';

const ACTIVITY_CAP = 50;

export default {
  id: 'SECURITY',
  sub: 'defensive posture',
  css: true,
  thread: false,

  async mount(root, ctx) {
    const st = {
      dash: null, lock: null, lockError: '', analyst: null, staleMsg: '',
      open: null, scanning: false, reporting: false,
    };
    const activity = ring(ACTIVITY_CAP);
    let lastError = null;

    /* skeleton: built once, then each region repaints on its own so an open
       REMEDIATE panel or a pending LOCK card survives an event */
    root.dataset.state = 'populated';
    setHtml(root, html`
      <div class="sec-note" id="secNote" role="status" hidden></div>
      <div id="secConfirm"></div>
      <div class="grid2 sec-top">
        <div class="card"><div class="lbl">How a finding is made</div><div id="secFlow" data-region></div></div>
        <div class="card"><div class="lbl">This Mac</div><div id="secMac" data-region></div></div>
      </div>
      <div class="grid2 sec-bottom">
        <div class="card"><div class="lbl">Findings on this Mac</div><div id="secFindings" data-region></div></div>
        <div class="card"><div class="lbl">Live activity</div><div id="secActivity" data-region></div></div>
      </div>`);
    const $ = (id) => root.querySelector('#' + id);
    const noteEl = $('secNote');
    const confirmEl = $('secConfirm');
    const flowEl = $('secFlow');
    const macEl = $('secMac');
    const findEl = $('secFindings');
    const actEl = $('secActivity');

    const note = (text, tone) => {
      noteEl.hidden = !text;
      noteEl.textContent = text || '';
      noteEl.dataset.tone = tone || '';
    };

    /* ---------------- stage bar ---------------- */
    function paintActions() {
      ctx.chrome.setActions([
        {
          id: 'scan', label: st.scanning ? 'SCANNING' : 'SCAN NOW', kind: 'primary', disabled: st.scanning,
          spec: 'Runs the deterministic scan now. There is no model call anywhere in the detection path.',
          onClick: () => scan(),
        },
        {
          id: 'report', label: st.reporting ? 'BUILDING' : 'FULL REPORT', disabled: st.reporting,
          spec: 'Builds the full report from a fresh scan and saves it as a file. It states what was not checked. It reads only; it changes nothing.',
          onClick: () => fullReport(),
        },
      ]);
    }

    /* ---------------- reads ---------------- */
    async function load(reason) {
      const [dash, lock, analyst] = await Promise.allSettled([
        ctx.api.get('/api/security_dashboard'),
        ctx.api.get('/api/security/lockdown'),
        ctx.api.get('/api/security/analyst'),
      ]);
      if (ctx.signal.aborted) return;
      if (dash.status === 'fulfilled') {
        st.dash = dash.value;
        st.staleMsg = dash.value && dash.value.__stale ? 'Showing the last copy this window kept. The server did not answer.' : '';
        lastError = null;
      } else if (!isAbort(dash.reason)) {
        lastError = dash.reason;
      }
      if (lock.status === 'fulfilled') {
        st.lock = lock.value;
        st.lockError = '';
      } else if (!isAbort(lock.reason)) {
        st.lockError = lock.reason && lock.reason.message ? lock.reason.message : String(lock.reason);
      }
      if (analyst.status === 'fulfilled') st.analyst = analyst.value;
      paint(reason);
    }

    /* ---------------- painting ---------------- */
    function paint() {
      if (!st.dash && lastError) {
        /* nothing to show yet and the first read failed */
        root.dataset.state = 'error';
        states.error(flowEl, lastError, { title: 'Could not read the security dashboard', retry: () => refreshNow() });
        macEl.replaceChildren();
        findEl.replaceChildren();
        paintActivity();
        return;
      }
      if (!st.dash) return;
      if (lastError) {
        /* had data, the latest read failed: keep it, say it is old */
        states.stale(root, 'Could not refresh: ' + lastError.message + ' The figures below are from the last read.');
      } else if (st.staleMsg) {
        states.stale(root, st.staleMsg);
      } else {
        root.querySelectorAll(':scope > .st-stale').forEach((n) => n.remove());
        root.dataset.state = st.dash.scanned ? 'populated' : 'empty';
      }
      paintFlow();
      paintMac();
      paintFindings();
      paintActivity();
      ctx.chrome.setLive(Boolean(st.dash.scanned));
    }

    function paintFlow() {
      flowEl.dataset.state = 'populated';
      setHtml(flowEl, flowSvg({
        layout: 'column', idPrefix: 'sec',
        label: 'How a finding is made, from telemetry to a suggested response',
        boxes: flowBoxes(st.dash, st.analyst),
        bracket: { from: 0, to: 3, label: 'DETECTION: no model in this path' },
        legend: flowLegend(st.dash),
      }));
    }

    function paintMac() {
      const d = st.dash;
      const facts = macFacts(d);
      const tag = macTag(d);
      const ld = st.lock;
      const rows = [];
      if (facts) {
        rows.push(html`<div class="kv"><span>Firewall</span><b class="tone-${facts.firewall.tone}">${facts.firewall.word}</b></div>`);
        rows.push(html`<div class="kv"><span>Exposure</span><b class="tone-${facts.exposure.tone}">${facts.exposure.word}</b></div>`);
        rows.push(html`<div class="kv"><span>Risk score</span><b>${facts.risk}</b></div>`);
        rows.push(html`<div class="kv"><span>Known devices</span><b>${facts.devices}${facts.newDevices ? ' (' + facts.newDevices + ' new)' : ''}</b></div>`);
        rows.push(html`<div class="kv"><span>Last scan</span><b>${d.last_scan_at ? agoLabel(d.last_scan_at) : ''}</b></div>`);
      } else {
        rows.push(html`<div class="muted">No scan has finished yet. Press SCAN NOW, or wait for the sentry's next pass.</div>`);
      }
      const posture = Array.isArray(d.posture) ? d.posture : [];
      const post = posture.length ? html`<div class="lbl sec-sub">Posture by area</div>${posture.map((p) => {
        const r = RATING[p.rating] || RATING.unknown;
        return html`<div class="kv"><span>${p.label}${p.not_checked && p.not_checked.length ? html` <span class="muted">(not checked: ${p.not_checked.join(', ')})</span>` : ''}</span><b><span class="tag ${r.tag}">${r.word}</span></b></div>`;
      })}` : '';
      const lockErr = st.lockError ? html`<div class="muted sec-err">Lockdown state unavailable: ${st.lockError}</div>` : '';
      macEl.dataset.state = 'populated';
      setHtml(macEl, html`
        <div class="mach"><div class="mh">${ctx.kit.icons.icon('SECURITY')}<b>This Mac</b><span class="tag ${tag.tone}">${tag.word}</span></div>${rows}</div>
        ${post}
        <div class="sec-lock"><div class="muted" id="lockLine">${lockdownLine(ld)}</div>
          <div class="sec-btns"><button type="button" class="os-btn os-btn--danger" id="lockBtn" ${ld ? '' : 'disabled'} data-spec="${ld && ld.active ? 'Stops lockdown after asking. The blocked apps and sites are released.' : 'Starts lockdown after asking: apps on the blocklist are closed when they open, and sites are blocked in the hosts file by a root helper the owner installs once. This is a local blocklist, not a network lockdown.'}">${ld && ld.active ? 'UNLOCK' : 'LOCK'}</button></div>${lockErr}</div>`);
    }

    function paintFindings() {
      const d = st.dash;
      const fs = findings(d);
      if (!d.scanned) {
        states.empty(findEl, 'No scan has finished yet.', { hint: 'Findings appear after the first scan. This is not an all-clear.' });
        return;
      }
      if (!fs.length) {
        states.empty(findEl, 'Nothing found on the last scan.', { hint: 'A clean scan only covers what the collectors could read. See posture for what was not checked.' });
        return;
      }
      findEl.dataset.state = 'populated';
      const order = { high: 0, med: 1, low: 2 };
      const sorted = fs.slice().sort((a, b) => (order[a.severity] ?? 3) - (order[b.severity] ?? 3));
      setHtml(findEl, html`${sorted.map((f) => html`
        <div class="os-row sec-find" data-fp="${f.fingerprint}">
          <span class="tag ${SEV_TAG[f.severity] || ''}">${SEV_WORD[f.severity] || f.severity}</span>
          ${f.is_new ? html`<span class="tag warn">new</span>` : ''}
          <span class="rt">${f.title}</span>
          <span class="os-row-actions"><button type="button" class="os-btn" data-remediate="${f.fingerprint}" aria-expanded="${String(st.open === f.fingerprint)}" data-spec="Shows what the sentry suggests. Nothing is applied. You can hand the finding to HOME, where any change goes through the human approval gate.">REMEDIATE</button></span>
        </div>
        ${st.open === f.fingerprint ? html`<div class="sec-fix" data-fix="${f.fingerprint}">
          <div class="who">What was found</div><div class="muted body-text">${f.detail}</div>
          <div class="who">Suggested (not applied)</div><div class="muted body-text">${f.recommended_action || 'The sentry gave no suggestion for this finding.'}</div>
          <div class="sec-btns"><button type="button" class="os-btn os-btn--primary" data-handoff="${f.fingerprint}" data-spec="Sends this finding to HOME as a directive. The model reads it with its security tools and proposes a fix; every change it wants to make waits for your approval.">HAND TO DOURMOUSE</button></div>
          <div class="muted" data-handoff-msg></div>
        </div>` : ''}`)}`);
    }

    function paintActivity() {
      const items = activity.newestFirst();
      if (!items.length) {
        states.empty(actEl, 'No events since this page opened.', { hint: 'Scans, network changes, downloads and analyst answers show up here as they happen.' });
        return;
      }
      actEl.dataset.state = 'populated';
      setHtml(actEl, html`<div class="sec-log">${items.map((i) => html`<div>${activityRow(i)}</div>`)}</div>`);
    }

    /* ---------------- actions ---------------- */
    async function refreshNow() {
      states.loading(flowEl, 'Reading the dashboard');
      await load('manual');
    }

    async function scan() {
      st.scanning = true;
      paintActions();
      note('Scanning. This reads the machine and takes a few seconds.', 'info');
      try {
        const r = await ctx.api.post('/api/security/action', { action: 'scan' });
        note('Scan finished: ' + r.findings + ' findings, ' + r.new + ' new.', 'ok');
        await load('scan');
      } catch (err) {
        if (!isAbort(err)) note('Scan failed: ' + err.message, 'error');
      } finally {
        st.scanning = false;
        if (!ctx.signal.aborted) paintActions();
      }
    }

    async function fullReport() {
      st.reporting = true;
      paintActions();
      note('Building the report from a fresh scan.', 'info');
      try {
        const r = await ctx.api.post('/api/security/action', { action: 'report' });
        const rep = r.report || {};
        const unknowns = Array.isArray(rep.unknowns) ? rep.unknowns : [];
        note('Report saved to ' + r.saved_to + '. ' + (rep.headline || '') + (unknowns.length ? ' Not covered: ' + unknowns.join(' ') : ''), 'ok');
      } catch (err) {
        if (!isAbort(err)) note('Report failed: ' + err.message, 'error');
      } finally {
        st.reporting = false;
        if (!ctx.signal.aborted) paintActions();
      }
    }

    /* A page confirmation, as in the console: the server has no gate on this
       route, so the card is the ask. Nothing runs until APPROVE is pressed. */
    function confirmHere(prompt, run) {
      const entry = { id: 'local-' + Date.now(), prompt, email: false, autonomous: false, state: 'pending', busy: false, error: '' };
      const card = ctx.kit.approvalCard(entry, async (ok) => {
        if (!ok) {
          entry.state = 'declined';
          card.paint();
          return true;
        }
        entry.busy = true;
        entry.error = '';
        card.paint();
        try {
          await run();
          entry.state = 'approved';
        } catch (err) {
          entry.error = err && err.message ? err.message : String(err);
        }
        entry.busy = false;
        card.paint();
        return true;
      });
      confirmEl.replaceChildren(card.el);
      card.focus();
    }

    function lockToggle() {
      const ld = st.lock;
      if (!ld) return;
      const start = !ld.active;
      confirmHere(lockPrompt(ld), async () => {
        const r = await ctx.api.post('/api/security/action', { action: start ? 'lockdown_start' : 'lockdown_stop' });
        st.lock = r.lockdown || st.lock;
        paintMac();
        note(start ? 'Lockdown started.' : 'Lockdown stopped.', 'ok');
      });
    }

    root.addEventListener('click', (e) => {
      const rem = e.target.closest('[data-remediate]');
      if (rem) {
        st.open = st.open === rem.dataset.remediate ? null : rem.dataset.remediate;
        paintFindings();
        return;
      }
      const hand = e.target.closest('[data-handoff]');
      if (hand) {
        const f = findings(st.dash).find((x) => x.fingerprint === hand.dataset.handoff);
        if (!f) return;
        hand.disabled = true;
        ctx.chat.send(remediationDirective(f)).catch((err) => ctx.notify({ level: 'error', title: 'Could not hand it to HOME', detail: err && err.message }));
        const msg = hand.closest('.sec-fix').querySelector('[data-handoff-msg]');
        const a = document.createElement('a');
        a.href = '#/home';
        a.textContent = 'Open HOME to follow it';
        msg.replaceChildren(document.createTextNode('Sent. Any change it wants to make waits for your approval. '), a);
        return;
      }
      if (e.target.closest('#lockBtn')) lockToggle();
    });

    /* ---------------- live ---------------- */
    ctx.events.on('security_*', (evt) => {
      const line = activityLine(evt);
      if (line) {
        activity.push(line);
        paintActivity();
      }
      if (evt.type === 'security_scan' || evt.type === 'security_analysis') load('event');
    });
    ctx.events.onResync(() => load('resync'));
    ctx.every(30000, () => load('poll'));

    paintActions();
    states.loading(flowEl, 'Reading the dashboard');
    await load('show');
  },

  async refresh(ctx, reason) {
    /* the shell's refresh shortcut lands here; mount() owns the reads */
    if (reason === 'manual') ctx.notify({ level: 'info', title: 'SECURITY', detail: 'Use SCAN NOW to run a scan. The view refreshes itself on every scan event.', ttl: 3000 });
  },
};
