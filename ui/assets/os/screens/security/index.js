/* SECURITY: the defensive posture of THIS Mac. Reads /api/security_dashboard
   (the sentry's last completed scan; it never triggers one), the lockdown
   state and the analyst's last answer. Live through security_* events, and it
   re-reads everything after the event stream reconnects.

   Plan 1.9: the page opens on a Simple view (one sentence, one row per area, the findings and what
   the Wi-Fi can see) and has a Detailed view with the facts, the live feed, the analyst and the
   cloud ledger. The choice is remembered on this Mac.

   Nothing here changes the machine on its own. Scan now and Full report only
   read. Lock down asks first, in a card, exactly as the console asks before it.
   Review shows the sentry's suggestion and can hand the finding to Home,
   where every change goes through the real approval gate. */

import { html, setHtml } from '../../kit/html.js';
import { states } from '../../kit/states.js';
import { flowSvg } from '../../kit/flow-svg.js';
import { confirmHere as kitConfirm } from '../../kit/confirm-card.js';
import { ring } from '../../core/ring.js';
import { agoLabel } from '../../kit/format.js';
import { isAbort } from '../../core/api.js';
import {
  SEV_TAG, SEV_WORD, findings, flowBoxes, flowLegend, macFacts, macTag,
  lockdownLine, lockPrompt, remediationDirective, activityLine, activityRow, egressRow, egressSummary,
  areaRows, headline, cleanView,
} from './helpers.js';

const ACTIVITY_CAP = 50;


export default {
  id: 'SECURITY',
  sub: 'defensive posture',
  css: true,
  thread: false,

  async mount(root, ctx) {
    const st = {
      explaining: false, dash: null, lock: null, lockError: '', analyst: null, staleMsg: '',
      open: null, scanning: false, reporting: false, view: cleanView(ctx.prefs.get('view')), wifi: null,
    };
    const activity = ring(ACTIVITY_CAP);
    let lastError = null;

    /* skeleton: built once, then each region repaints on its own so an open
       REMEDIATE panel or a pending LOCK card survives an event */
    root.dataset.state = 'populated';
    setHtml(root, html`
      <div class="sec" id="secPage" data-view="${st.view}">
        <div class="sec-head">
          <div class="sec-headt"><h2 class="sec-big" id="secHeadline">Reading the dashboard</h2><p class="sec-subline" id="secSub"></p></div>
          <span class="os-seg sec-views" role="group" aria-label="Level of detail">
            <button type="button" data-view="simple" aria-selected="${String(st.view === 'simple')}" data-spec="Shows one sentence, one row per area, the findings and what your Wi-Fi can see.">Simple</button>
            <button type="button" data-view="detailed" aria-selected="${String(st.view === 'detailed')}" data-spec="Adds the facts about this Mac, the live feed, the analyst, what was sent to cloud models and how a finding is made.">Detailed</button>
          </span>
        </div>
        <div class="sec-note" id="secNote" role="status" hidden></div>
        <div id="secConfirm"></div>
        <section class="sec-group"><h3 class="sec-h">Areas</h3><div class="sec-box" id="secAreas" data-region></div></section>
        <section class="sec-group" id="secFindGroup"><h3 class="sec-h" id="secFindLbl">Findings</h3><div class="sec-box" id="secFindings" data-region></div></section>
        <section class="sec-group"><h3 class="sec-h">Wi-Fi</h3><div class="sec-box" id="secWifi" data-region></div></section>
        <section class="sec-group sec-detail"><h3 class="sec-h">This Mac</h3><div class="sec-box sec-pad" id="secMac" data-region></div></section>
        <section class="sec-group sec-detail"><h3 class="sec-h">Live activity</h3><div class="sec-box sec-pad" id="secActivity" data-region></div></section>
        <section class="sec-group sec-detail"><h3 class="sec-h">AI analyst</h3><div class="sec-box sec-pad" id="secAnalyst" data-region></div></section>
        <section class="sec-group sec-detail"><h3 class="sec-h">Sent to cloud models</h3><div class="sec-box sec-pad" id="secEgress" data-region></div></section>
        <details class="sec-group sec-detail sec-how" id="secHow"><summary class="sec-h">How a finding is made</summary><div id="secFlow" data-region></div></details>
      </div>`);
    const $ = (id) => root.querySelector('#' + id);
    const noteEl = $('secNote');
    const confirmEl = $('secConfirm');
    const flowEl = $('secFlow');
    const macEl = $('secMac');
    const findEl = $('secFindings');
    const anaEl = $('secAnalyst');
    const findLbl = $('secFindLbl');
    const pageEl = $('secPage');
    const areasEl = $('secAreas');
    const wifiEl = $('secWifi');
    const headEl = $('secHeadline');
    const subEl = $('secSub');
    const actEl = $('secActivity');
    const egressEl = $('secEgress');

    const note = (text, tone) => {
      noteEl.hidden = !text;
      noteEl.textContent = text || '';
      noteEl.dataset.tone = tone || '';
    };

    /* ---------------- stage bar ---------------- */
    function paintActions() {
      ctx.chrome.setActions([
        {
          id: 'scan', label: st.scanning ? 'Scanning' : 'Scan now', kind: 'primary', disabled: st.scanning,
          spec: 'Runs the deterministic scan now. There is no model call anywhere in the detection path.',
          onClick: () => scan(),
        },
        {
          id: 'report', label: st.reporting ? 'Building' : 'Full report', disabled: st.reporting,
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

    /* PRIV-4: the egress ledger. Metadata only: provider, size, how many
       results of each tier, which sources, how many identifiers were swapped. */
    async function loadEgress() {
      try {
        const d = await ctx.api.get('/api/os/privacy/egress?limit=50');
        if (ctx.signal.aborted) return;
        paintEgress(d);
      } catch (err) {
        if (isAbort(err)) return;
        states.error(egressEl, err, { title: 'Could not read the egress ledger', retry: () => loadEgress() });
      }
    }

    function paintEgress(d) {
      const rows = Array.isArray(d && d.rows) ? d.rows.map(egressRow) : [];
      egressEl.dataset.state = rows.length ? 'populated' : 'empty';
      setHtml(egressEl, html`<div class="muted sec-help">${egressSummary(d)} Every model Dourmouse uses runs in the cloud. This list shows the requests Dourmouse recorded: never their content, only size, a fingerprint and what kinds of data they carried. Requests the Claude or Codex program makes on its own are not listed.</div>
        ${rows.map((r) => html`<div class="os-row sec-egress"><span class="rt"><b>${r.provider}</b>${r.model ? html` <span class="muted mono">${r.model}</span>` : ''}
          <span class="muted sec-block">${r.when} &middot; ${r.size} &middot; ${r.tiers}${r.sources ? ' (' + r.sources + ')' : ''}${r.redactions ? ' · ' + r.redactions : ''}</span></span>
          ${r.sensitive ? html`<span class="tag warn">sensitive</span>` : ''}${r.private ? html`<span class="tag ok">private</span>` : ''}</div>`)}`);
    }

    /* ---------------- painting ---------------- */
    function paint() {
      if (!st.dash && lastError) {
        /* nothing to show yet and the first read failed */
        root.dataset.state = 'error';
        states.error(findEl, lastError, { title: 'Could not read the security dashboard', retry: () => refreshNow() });
        macEl.replaceChildren();
        areasEl.replaceChildren();
        headEl.textContent = 'Could not read the security dashboard';
        flowEl.replaceChildren();
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
      paintTop();
      paintFlow();
      paintMac();
      paintFindings();
      paintActivity();
      paintAnalyst();
      ctx.chrome.setLive(Boolean(st.dash.scanned));
    }

    /* the sentence at the top and one row per area: what a person reads first */
    function paintTop() {
      const d = st.dash;
      const h = headline(d);
      headEl.textContent = h.text;
      headEl.dataset.tone = h.tone;
      subEl.textContent = h.sub;
      const rows = areaRows(d);
      if (!rows.length) {
        states.empty(areasEl, d.scanned ? 'No areas were reported.' : 'No scan has finished yet.', { hint: 'Areas appear after the first scan. This is not an all-clear.' });
        return;
      }
      areasEl.dataset.state = 'populated';
      setHtml(areasEl, html`${rows.map((r) => html`<div class="sec-row" data-area="${r.key}"><span class="sec-dot" data-tone="${r.tone}" aria-hidden="true"></span><span class="sec-rt"><span>${r.label}</span>${r.note ? html`<span class="sec-rs">${r.note}</span>` : ''}</span><span class="sec-word" data-tone="${r.tone}">${r.word}</span></div>`)}`);
    }

    /* ---------------- the Wi-Fi card ---------------- */
    async function loadWifi() {
      try {
        const w = await ctx.api.get('/api/os/security/wifi');
        if (ctx.signal.aborted) return;
        st.wifi = w;
        paintWifi();
      } catch (err) {
        if (isAbort(err)) return;
        states.error(wifiEl, err, { title: 'Could not read the Wi-Fi link', retry: () => loadWifi() });
      }
    }

    function paintWifi() {
      const w = st.wifi;
      if (!w) return;
      if (w.state === 'unavailable') {
        states.unavailable(wifiEl, 'The Wi-Fi link could not be read on this Mac.', { retry: () => loadWifi() });
        return;
      }
      if (!w.connected) {
        states.empty(wifiEl, 'Not connected to Wi-Fi.', { hint: 'Nothing on this Mac is using a Wi-Fi network right now.' });
        return;
      }
      const name = w.name || (w.name_hidden ? 'Name hidden by macOS' : 'Unknown network');
      const list = (title, items, cls) => (items && items.length ? html`<div class="sec-wl ${cls}"><div class="who">${title}</div><ul>${items.map((x) => html`<li>${x}</li>`)}</ul></div>` : '');
      wifiEl.dataset.state = 'populated';
      setHtml(wifiEl, html`
        <div class="sec-row"><span class="sec-rt">Network</span><span class="sec-word">${name}</span></div>
        <div class="sec-row"><span class="sec-rt">Protection</span><span class="sec-word" data-tone="${w.weak ? 'bad' : ''}">${w.security_word}</span></div>
        <div class="sec-row"><span class="sec-rt">VPN</span><span class="sec-word">${w.vpn_connected === null ? 'Not checked' : w.vpn_connected ? 'Connected' : 'Not connected'}</span></div>
        <div class="sec-pad sec-wifi-can">
          ${list('What this network can see', w.sees, 'sec-wl-sees')}
          ${list('What it cannot see', w.hidden, 'sec-wl-hidden')}
          ${list('Could not check', w.unknown, 'sec-wl-unknown')}
        </div>`);
    }

    function paintFlow() {
      flowEl.dataset.state = 'populated';
      setHtml(flowEl, flowSvg({
        layout: 'column', idPrefix: 'sec',
        label: 'How a finding is made, from telemetry to a suggested response',
        boxes: flowBoxes(st.dash, st.analyst),
        bracket: { from: 0, to: 3, label: 'Detection: no model in this path' },
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
        rows.push(html`<div class="muted">No scan has finished yet. Press Scan now, or wait for the sentry's next pass.</div>`);
      }
      const lockErr = st.lockError ? html`<div class="muted sec-err">Lockdown state unavailable: ${st.lockError}</div>` : '';
      macEl.dataset.state = 'populated';
      setHtml(macEl, html`
        <div class="mach"><div class="mh"><b>Result</b><span class="tag ${tag.tone}">${tag.word}</span></div>${rows}</div>
        <div class="sec-lock"><div class="muted" id="lockLine">${lockdownLine(ld)}</div>
          <div class="sec-btns"><button type="button" class="os-btn os-btn--danger" id="lockBtn" ${ld ? '' : 'disabled'} data-spec="${ld && ld.active ? 'Stops lockdown after asking. The blocked apps and sites are released.' : 'Starts lockdown after asking: apps on the blocklist are closed when they open, and sites are blocked in the hosts file by a root helper the owner installs once. This is a local blocklist, not a network lockdown.'}">${ld && ld.active ? 'Unlock' : 'Lock down'}</button></div>${lockErr}</div>`);
    }

    function paintFindings() {
      const d = st.dash;
      const fs = findings(d);
      findLbl.textContent = d.scanned && fs.length ? 'Findings (' + fs.length + ')' : 'Findings';
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
        <div class="sec-row sec-find" data-fp="${f.fingerprint}">
          <span class="sec-dot" data-tone="${SEV_TAG[f.severity] || ''}" aria-hidden="true"></span>
          <span class="sec-rt"><span>${f.title}</span><span class="sec-rs">${SEV_WORD[f.severity] || f.severity}${f.is_new ? ', new' : ''}</span></span>
          <span class="os-row-actions"><button type="button" class="os-btn" data-remediate="${f.fingerprint}" aria-expanded="${String(st.open === f.fingerprint)}" data-spec="Shows what the sentry suggests. Nothing is applied. You can hand the finding to Home, where any change goes through the human approval gate.">Review</button></span>
        </div>
        ${st.open === f.fingerprint ? html`<div class="sec-fix" data-fix="${f.fingerprint}">
          <div class="who">What was found</div><div class="muted body-text">${f.detail}</div>
          <div class="who">Suggested (not applied)</div><div class="muted body-text">${f.recommended_action || 'The sentry gave no suggestion for this finding.'}</div>
          <div class="sec-btns"><button type="button" class="os-btn os-btn--primary" data-handoff="${f.fingerprint}" data-spec="Sends this finding to Home as a directive. The model reads it with its security tools and proposes a fix; every change it wants to make waits for your approval.">Hand to Dourmouse</button></div>
          <div class="muted" data-handoff-msg></div>
        </div>` : ''}`)}`);
    }

    /* The analyst's last written explanation (a cloud model reading the sentry's
       findings). It is model text, so it only goes in through the template. */
    function paintAnalyst() {
      const a = st.analyst && st.analyst.analysis;
      const busy = st.explaining;
      const btn = html`<div class="sec-btns"><button type="button" class="os-btn" id="explainBtn" ${busy ? 'disabled' : ''} data-spec="Asks first, then runs a fresh scan and sends the finding titles and details to a cloud model, which writes a plain explanation. It uses credits and changes nothing on this Mac.">${busy ? 'Thinking' : 'Explain my findings'}</button></div>`;
      if (!a) {
        anaEl.dataset.state = 'empty';
        setHtml(anaEl, html`<div class="muted">The analyst has not explained anything since it was last cleared. It writes on its own when a medium or high finding appears and the cloud analyst is switched on, or you can ask for one now.</div>${btn}`);
        return;
      }
      if (!a.ok) {
        anaEl.dataset.state = 'unavailable';
        setHtml(anaEl, html`<div class="muted">The analyst could not answer: ${String(a.error || 'no reason was given')}</div>${btn}`);
        return;
      }
      anaEl.dataset.state = 'populated';
      const pts = Array.isArray(a.points) ? a.points : [];
      setHtml(anaEl, html`
        <div class="sec-ana-sum">${a.summary}</div>
        <div class="muted">Worry level: ${a.worry}${a.at ? ' · ' + agoLabel(a.at) : ''}. Written by a cloud model from the findings; it is an explanation, not a verdict.</div>
        ${pts.map((p) => html`<div class="os-row sec-find"><span class="rt"><b>${(p.titles || []).join(' + ')}</b><div class="muted">${p.meaning}</div>${p.first_step ? html`<div class="sec-first">First step: ${p.first_step}</div>` : ''}</span></div>`)}
        ${a.dropped ? html`<div class="muted">${String(a.dropped)} point(s) were dropped because they did not name a real finding.</div>` : ''}
        ${btn}`);
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
      if (st.scanning) return; /* a second click before the bar repaints must not start a second scan */
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
      if (st.reporting) return;
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
       route, so the card is the ask. Nothing runs until APPROVE is pressed.
       Esc declines it (never approves); a finished card clears itself. */
    let offEsc = null;
    function clearConfirm() {
      if (offEsc) { offEsc(); offEsc = null; }
      confirmEl.replaceChildren();
    }
    function confirmHere(prompt, run) {
      if (offEsc) { offEsc(); offEsc = null; }
      kitConfirm(confirmEl, prompt, run, {
        onDone: (approved) => setTimeout(() => { if (!ctx.signal.aborted && confirmEl.querySelector('.approve.done')) clearConfirm(); }, approved ? 4000 : 1800),
      });
      offEsc = ctx.keys.pushEsc(() => {
        clearConfirm();
      });
    }

    function explain() {
      if (st.explaining) return;
      confirmHere('Ask the cloud analyst to explain the findings? This runs a fresh scan and sends the finding titles and details to a cloud model, which uses credits. It changes nothing on this Mac.', async () => {
        st.explaining = true;
        paintAnalyst();
        try {
          const r = await ctx.api.post('/api/security/action', { action: 'analyze' });
          if (r && r.analysis) st.analyst = { analysis: r.analysis };
        } finally {
          st.explaining = false;
          if (!ctx.signal.aborted) paintAnalyst();
        }
      });
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
      const vw = e.target.closest('.sec-views [data-view]');
      if (vw) {
        st.view = cleanView(vw.dataset.view);
        ctx.prefs.set('view', st.view);
        pageEl.dataset.view = st.view;
        root.querySelectorAll('.sec-views button').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.view === st.view)));
        return;
      }
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
        ctx.chat.send(remediationDirective(f)).catch((err) => ctx.notify({ level: 'error', title: 'Could not hand it to Home', detail: err && err.message }));
        const msg = hand.closest('.sec-fix').querySelector('[data-handoff-msg]');
        const a = document.createElement('a');
        a.href = '#/home';
        a.textContent = 'Open Home to follow it';
        msg.replaceChildren(document.createTextNode('Sent. Any change it wants to make waits for your approval. '), a);
        return;
      }
      if (e.target.closest('#lockBtn')) lockToggle();
      if (e.target.closest('#explainBtn')) explain();
    });

    /* ---------------- live ---------------- */
    ctx.events.on('security_*', (evt) => {
      const line = activityLine(evt);
      if (line) {
        activity.push(line);
        paintActivity();
      }
      if (evt.type === 'security_network_change') loadWifi();
      if (evt.type === 'security_scan' || evt.type === 'security_analysis') load('event');
    });
    ctx.events.onResync(() => load('resync'));
    ctx.every(30000, () => load('poll'));
    ctx.every(30000, () => loadEgress());
    ctx.every(60000, () => loadWifi());

    paintActions();
    states.loading(flowEl, 'Reading the dashboard');
    states.loading(egressEl, 'Reading the egress ledger');
    states.loading(wifiEl, 'Reading the Wi-Fi link');
    loadEgress();
    loadWifi();
    await load('show');
  },

  async refresh(ctx, reason) {
    /* the shell's refresh shortcut lands here; mount() owns the reads */
    if (reason === 'manual') ctx.notify({ level: 'info', title: 'Security', detail: 'Use Scan now to run a scan. The view refreshes itself on every scan event.', ttl: 3000 });
  },
};
