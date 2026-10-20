/* COMMAND CENTER (finding #186, owner request 2026-10-21: "see the full
   chain of thought of the AI sentries live, live updates, command centre,
   everything"). Two real, live feeds on one screen, both already pushed by
   the server -- this screen adds no polling of its own beyond one seed read
   each:

   LEFT -- every agent's full live trace. Seeded from GET /api/activity (who
   is doing something right now), then grown live from the shared event
   stream's "agent_trace" events (dispatch.py's _broadcast_live_trace):
   the actual reasoning (thinking_delta), every tool call and its real
   result, plans, confirmation asks and answers -- uncollapsed, nothing
   truncated beyond a generous per-lane cap (helpers.js's LANE_TEXT_CAP),
   for EVERY run on the machine, not just the one chat tab you have open.
   This is new: before #186, a background goal or routine, or a chat on a
   tab you were not looking at, produced no live trace anywhere.

   RIGHT -- the security sentry, full text. Seeded from GET
   /api/security_dashboard, then grown live from "security_*" events
   (already real, already pushed -- see security/index.js). Unlike the
   SECURITY screen's own activity line (deliberately short, 160 characters,
   for a compact history list), this renders the analyst's FULL summary and
   every point's full meaning and first step, every time a new analysis
   arrives, plus the sentry's tick count and every finding as it lands.

   Nothing on this screen sends anything or changes anything; it only
   reads. No model text here is quoted into an action -- it is rendered as
   escaped text throughout (kit/html.js's html``), same as every other
   screen. */

import { html, setHtml } from '../../kit/html.js';
import { states } from '../../kit/states.js';
import { clock, agoLabel } from '../../kit/format.js';
import { isAbort } from '../../core/api.js';
import { applyTrace, laneSummary } from './helpers.js';

const KIND_TAG = { thinking: 'cc-think', answer: 'cc-answer', tool: 'cc-tool', result: 'cc-result', plan: 'cc-plan', gate: 'cc-gate', status: 'cc-status', other: 'cc-other' };

export default {
  id: 'COMMAND_CENTER',
  sub: 'live trace of every agent and the security sentry',
  css: true,
  thread: false,

  async mount(root, ctx) {
    const lanes = {};
    const order = [];
    const st = { dash: null, dashError: null, analyses: [], paused: false };

    root.dataset.state = 'populated';
    setHtml(root, html`
      <div class="cc-note" id="ccNote" role="status" hidden></div>
      <div class="cc-grid">
        <div class="card cc-col">
          <div class="cc-colhead"><b>LIVE AGENT TRACE</b><span class="muted" id="ccLaneCount"></span></div>
          <div id="ccLanes" data-region></div>
        </div>
        <div class="card cc-col">
          <div class="cc-colhead"><b>SECURITY SENTRY</b><span class="muted" id="ccSecSub"></span></div>
          <div id="ccSec" data-region></div>
        </div>
      </div>`);
    const $ = (id) => root.querySelector('#' + id);
    const lanesEl = $('ccLanes');
    const secEl = $('ccSec');
    const noteEl = $('ccNote');
    const laneCountEl = $('ccLaneCount');
    const secSubEl = $('ccSecSub');

    /* ---------------- left: agent lanes ---------------- */
    function paintLanes() {
      laneCountEl.textContent = order.length ? order.length + (order.length === 1 ? ' lane' : ' lanes') : '';
      if (!order.length) {
        states.empty(lanesEl, 'Nothing is running right now.', { hint: 'A new chat turn, goal, routine or delegated task appears here the moment it starts.' });
        return;
      }
      lanesEl.dataset.state = 'populated';
      setHtml(lanesEl, html`${order.map((key) => {
        const lane = lanes[key];
        const sum = laneSummary(lane);
        return html`<div class="cc-lane ${lane.done ? 'done' : ''}">
          <div class="cc-laneh"><b>${lane.agent}</b><span class="muted">${lane.call_id || 'no call id'}</span><span class="muted">${agoLabel(lane.lastAt)}</span>${lane.done ? html`<span class="tag ok">done</span>` : html`<span class="tag warn">live</span>`}</div>
          <div class="cc-lanebody mono">${lane.lines.map((l) => html`<div class="cc-line ${KIND_TAG[l.kind] || 'cc-other'}">${l.text}</div>`)}</div>
        </div>`;
      })}`);
      /* keep the newest lane's body scrolled to its own latest line */
      const first = lanesEl.querySelector('.cc-lane .cc-lanebody');
      if (first && !st.paused) first.scrollTop = first.scrollHeight;
    }

    /* ---------------- right: security sentry, full text ---------------- */
    function analysisBlock(a, at) {
      if (!a) return '';
      if (!a.ok) {
        return html`<div class="cc-analysis bad">${agoLabel(at)} -- analyst could not run: ${a.error || a.skipped && 'skipped (privacy)' || 'unknown'}</div>`;
      }
      return html`<div class="cc-analysis">
        <div class="cc-ahead">${agoLabel(at)} -- worry: <b>${a.worry || 'unknown'}</b>${a.model_worry && a.model_worry !== a.worry ? html` (model said ${a.model_worry}, raised)` : ''}</div>
        <div class="cc-summary">${a.summary || ''}</div>
        ${(a.points || []).map((p) => html`<div class="cc-point">
          <div class="cc-ptitle">${(p.titles || []).join(', ')}</div>
          <div>${p.meaning}</div>
          ${p.first_step ? html`<div class="muted">First step: ${p.first_step}</div>` : ''}
        </div>`)}
      </div>`;
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
      secSubEl.textContent = d.scanned ? 'tick ' + d.tick_count + ' - risk ' + (d.risk_score || 0).toFixed(1) : 'no scan yet';
      secEl.dataset.state = 'populated';
      const findings = (d.findings || []).slice().sort((a, b) => sevRank(b.severity) - sevRank(a.severity));
      setHtml(secEl, html`
        <div class="cc-secsum">${d.scanned ? d.findings.length + ' findings on the last scan' : 'No scan has finished yet.'}</div>
        <div class="cc-findings mono">${findings.map((f) => html`<div class="cc-line cc-f-${f.severity}">[${f.severity}] ${f.title}${f.is_new ? ' (new)' : ''}</div>`)}</div>
        <div class="cc-colhead2"><b>ANALYST, FULL TEXT</b></div>
        <div id="ccAnalyses">${st.analyses.length ? st.analyses.map((x) => analysisBlock(x.analysis, x.at)) : html`<div class="muted">No analysis has run yet.</div>`}</div>`);
    }
    function sevRank(s) { return s === 'high' ? 3 : s === 'medium' || s === 'med' ? 2 : s === 'low' ? 1 : 0; }

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

    /* ---------------- seed the agent lanes from the activity snapshot ---------------- */
    async function seedLanes() {
      try {
        const snap = await ctx.api.get('/api/activity');
        if (ctx.signal.aborted) return;
        const agents = (snap && snap.agents) || {};
        for (const [name, a] of Object.entries(agents)) {
          if (a && a.status && a.status !== 'idle' && a.last) {
            applyTrace(lanes, order, { type: 'function', agent: name, text: 'currently ' + a.status + (a.last.tool ? ': ' + a.last.tool : '') });
          }
        }
        paintLanes();
      } catch (err) {
        if (!isAbort(err)) { /* a missed seed is not fatal -- live events still arrive */ }
      }
    }

    /* ---------------- live ---------------- */
    ctx.events.on('agent_trace', (evt) => {
      if (st.paused) return;
      applyTrace(lanes, order, evt.entry || {});
      paintLanes();
    });
    ctx.events.on('security_*', (evt) => {
      if (evt.type === 'security_scan' || evt.type === 'security_analysis') loadDash();
      if (evt.type === 'security_analysis' && evt.analysis) {
        st.analyses.unshift({ analysis: evt.analysis, at: Date.now() });
        if (st.analyses.length > 20) st.analyses.length = 20;
        paintSec();
      }
    });
    ctx.events.onResync(() => { loadDash(); seedLanes(); });
    ctx.events.onStatus((s) => {
      noteEl.hidden = s !== 'error';
      noteEl.textContent = s === 'error' ? 'The live event stream dropped. This screen is behind until it reconnects.' : '';
    });

    function paintActions() {
      ctx.chrome.setActions([
        {
          id: 'pause', label: st.paused ? 'RESUME' : 'PAUSE', pressed: st.paused,
          spec: 'Freezes the agent trace so you can read a fast-moving run. Security events keep updating either way; nothing stops running on the machine.',
          onClick: () => {
            st.paused = !st.paused;
            paintActions();
          },
        },
      ]);
    }

    paintActions();
    paintLanes();
    await Promise.all([seedLanes(), loadDash()]);
  },

  async refresh(ctx, reason) {
    if (reason === 'manual') ctx.notify({ level: 'info', title: 'COMMAND CENTER', detail: 'Both feeds follow live events by themselves.', ttl: 3000 });
  },
};
