/* ACTIVITY (the Command Center screen, plan 1.7, CC-1).

   Lanes are real runs only. A lane opens in two ways and no other:
     1. GET /api/os/office/activity lists the runs that are live right now: a skill that is computing or waiting
        for approval AND holds a call id.
     2. An "agent_trace" event that carries a call id (dispatch.py _broadcast_live_trace) opens or grows a lane.
   The model-free feed polls (news, markets, rnd, mail, tasks) are not runs. They get one plain line, never a lane,
   so a fresh workspace shows no lanes until something really runs.

   Below the lanes: recent meetings (a task split across agents), and the security sentry's full text behind a
   disclosure. Nothing here sends or changes anything. All model text is shown as escaped text. */

import { html, setHtml } from '../../kit/html.js';
import { states } from '../../kit/states.js';
import { agoLabel, whenShort, plural } from '../../kit/format.js';
import { isAbort } from '../../core/api.js';
import { applyTrace, seedLane, applyFeeds, seedFeeds, feedsLine } from './helpers.js';

const KIND_TAG = { thinking: 'cc-think', answer: 'cc-answer', tool: 'cc-tool', result: 'cc-result', plan: 'cc-plan', gate: 'cc-gate', status: 'cc-status', other: 'cc-other' };

function sevRank(s) { return s === 'high' ? 3 : s === 'medium' || s === 'med' ? 2 : s === 'low' ? 1 : 0; }

export default {
  id: 'COMMAND_CENTER',
  sub: 'runs that are live now',
  css: true,
  thread: false,

  async mount(root, ctx) {
    const lanes = {};
    const order = [];
    const st = { feeds: {}, names: {}, meetings: [], open: new Set(), paused: false, dash: null, dashError: null, analyses: [], err: null };

    root.dataset.state = 'populated';
    setHtml(root, html`<div class="cc">
      <div class="cc-note" id="ccNote" role="status" hidden></div>
      <section class="cc-sec"><h2 class="cc-h">Live now</h2><div class="cc-box" id="ccLanes" data-region></div>
        <p class="cc-line-feeds" id="ccFeeds"></p></section>
      <section class="cc-sec" id="ccMeetSec" hidden><h2 class="cc-h">Recent meetings</h2><div class="cc-box" id="ccMeet"></div></section>
      <section class="cc-sec"><details class="cc-details" id="ccSecDet"><summary>Security sentry <span class="cc-sub" id="ccSecSub"></span></summary>
        <div class="cc-box cc-sentry" id="ccSec" data-region></div></details></section></div>`);
    const $ = (id) => root.querySelector('#' + id);
    const lanesEl = $('ccLanes');
    const feedsEl = $('ccFeeds');
    const meetSec = $('ccMeetSec');
    const meetEl = $('ccMeet');
    const secEl = $('ccSec');
    const secSub = $('ccSecSub');
    const noteEl = $('ccNote');
    const nameOf = (a) => st.names[a] || a || 'Agent';

    function paintFeeds() {
      feedsEl.textContent = feedsLine(st.feeds);
      feedsEl.hidden = !feedsEl.textContent;
    }

    function laneRow(key) {
      const lane = lanes[key];
      const open = st.open.has(key);
      const last = lane.lines[lane.lines.length - 1];
      const state = lane.done ? 'Done' : lane.waiting ? 'Waiting for you' : 'Running';
      return html`<div class="cc-lane" data-state-lane="${lane.done ? 'done' : lane.waiting ? 'wait' : 'run'}">
        <button type="button" class="cc-lane-h" data-lane="${key}" aria-expanded="${String(open)}">
          <i class="cc-dot" aria-hidden="true"></i>
          <span class="cc-lane-t"><b>${nameOf(lane.agent)}</b><span class="cc-lane-s">${last ? last.text.slice(0, 140) : lane.tool ? 'Using ' + lane.tool : 'Started'}</span></span>
          <span class="cc-lane-m">${state} · ${agoLabel(lane.lastAt)}</span></button>
        ${open ? html`<div class="cc-trace">${lane.lines.map((l) => html`<div class="cc-tl ${KIND_TAG[l.kind] || 'cc-other'}">${l.text}</div>`)}</div>` : ''}
      </div>`;
    }

    function paintLanes() {
      if (!order.length) {
        states.empty(lanesEl, 'Nothing is running.', { hint: 'A chat turn, job, automation or delegated task shows here the moment it starts.' });
        return;
      }
      lanesEl.dataset.state = 'populated';
      setHtml(lanesEl, html`${order.map((k) => laneRow(k))}`);
      const body = lanesEl.querySelector('.cc-trace');
      if (body && !st.paused) body.scrollTop = body.scrollHeight;
      ctx.chrome.setSub(plural(order.filter((k) => !lanes[k].done).length, 'run') + ' live');
    }

    function paintMeetings() {
      meetSec.hidden = !st.meetings.length;
      if (!st.meetings.length) return;
      setHtml(meetEl, html`${st.meetings.map((m) => html`<div class="cc-meet"><span class="cc-meet-t">${(m.agents || []).map(nameOf).join(', ')}</span>
        <span class="cc-lane-m">${String(m.succeeded)} of ${String(m.branches)} done · ${whenShort(m.ended)}</span></div>`)}`);
    }

    /* ---------------- security sentry (kept from the old screen, collapsed) ---------------- */
    function analysisBlock(a, at) {
      if (!a) return '';
      if (!a.ok) return html`<div class="cc-analysis">${agoLabel(at)}: the analyst could not run. ${a.error || (a.skipped && 'Skipped (privacy).') || ''}</div>`;
      return html`<div class="cc-analysis"><div class="cc-ahead">${agoLabel(at)}: worry <b>${a.worry || 'unknown'}</b></div>
        <div>${a.summary || ''}</div>
        ${(a.points || []).map((p) => html`<div class="cc-point"><div class="cc-ptitle">${(p.titles || []).join(', ')}</div><div>${p.meaning}</div>${p.first_step ? html`<div class="cc-lane-m">First step: ${p.first_step}</div>` : ''}</div>`)}</div>`;
    }

    function paintSec() {
      if (st.dashError && !st.dash) {
        states.error(secEl, st.dashError, { title: 'Could not read the sentry', retry: () => loadDash() });
        return;
      }
      if (!st.dash) {
        states.loading(secEl, 'Reading the sentry');
        return;
      }
      const d = st.dash;
      secSub.textContent = d.scanned ? 'tick ' + d.tick_count : 'no scan yet';
      secEl.dataset.state = 'populated';
      const findings = (d.findings || []).slice().sort((a, b) => sevRank(b.severity) - sevRank(a.severity));
      setHtml(secEl, html`<div class="cc-lane-m">${d.scanned ? plural(findings.length, 'finding') + ' on the last scan' : 'No scan has finished yet.'}</div>
        ${findings.map((f) => html`<div class="cc-find cc-f-${f.severity}">${f.title}${f.is_new ? ' (new)' : ''}</div>`)}
        ${st.analyses.length ? st.analyses.map((x) => analysisBlock(x.analysis, x.at)) : html`<div class="cc-lane-m">No analysis has run yet.</div>`}`);
    }

    async function loadDash() {
      try {
        const d = await ctx.api.get('/api/security_dashboard');
        if (ctx.signal.aborted) return;
        st.dash = d;
        st.dashError = null;
      } catch (err) {
        if (isAbort(err)) return;
        st.dashError = err;
      }
      paintSec();
    }

    async function seed() {
      try {
        const a = await ctx.api.get('/api/os/office/activity');
        if (ctx.signal.aborted) return;
        st.names = a.names || {};
        st.feeds = seedFeeds(a.feeds);
        st.meetings = a.meetings || [];
        st.err = null;
        for (const lane of a.lanes || []) seedLane(lanes, order, lane);
      } catch (err) {
        if (isAbort(err)) return;
        st.err = err;
      }
      if (st.err && !order.length) states.error(lanesEl, st.err, { title: 'Could not read what is running', retry: () => seed() });
      else paintLanes();
      paintFeeds();
      paintMeetings();
    }

    /* ---------------- live ---------------- */
    ctx.events.on('agent_trace', (evt) => {
      if (st.paused) return;
      if (applyTrace(lanes, order, evt.entry || {})) paintLanes();
    });
    ctx.events.on('agent_activity', (evt) => {
      applyFeeds(st.feeds, evt);
      paintFeeds();
    });
    ctx.events.on('security_*', (evt) => {
      if (evt.type === 'security_scan' || evt.type === 'security_analysis') loadDash();
      if (evt.type === 'security_analysis' && evt.analysis) {
        st.analyses.unshift({ analysis: evt.analysis, at: Date.now() });
        if (st.analyses.length > 20) st.analyses.length = 20;
        paintSec();
      }
    });
    ctx.events.onResync(() => { loadDash(); seed(); });
    ctx.events.onStatus((s) => {
      noteEl.hidden = s !== 'error';
      noteEl.textContent = s === 'error' ? 'The live event stream dropped. This screen is behind until it reconnects.' : '';
    });

    root.addEventListener('click', (e) => {
      const b = e.target.closest('[data-lane]');
      if (!b) return;
      const key = b.dataset.lane;
      if (st.open.has(key)) st.open.delete(key); else st.open.add(key);
      paintLanes();
    });

    function paintActions() {
      ctx.chrome.setActions([{
        id: 'pause', label: st.paused ? 'Resume' : 'Pause', pressed: st.paused,
        spec: 'Freezes the trace so you can read a fast run. Nothing stops running on the machine.',
        onClick: () => { st.paused = !st.paused; paintActions(); },
      }]);
    }

    paintActions();
    paintLanes();
    await Promise.all([seed(), loadDash()]);
  },

  async refresh(ctx, reason) {
    if (reason === 'manual') ctx.notify({ level: 'info', title: 'Activity', detail: 'This screen follows live events by itself.', ttl: 3000 });
  },
};
