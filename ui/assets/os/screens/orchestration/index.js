/* ORCHESTRATION: parallel fan-out, live. Reads GET /api/activity once for the
   runs in flight, then follows delegate_fanout events off the shared stream
   (no polling). Finished runs come from the persisted office log
   (GET /api/office_log?meetings=1), and TRANSCRIPT reads one run from
   GET /api/office_log?meeting=<run>.

   What the tracker does not know is not drawn: there is no per-branch tool
   count, no queued state and no worker cap in the events, so none is shown. */

import { html, setHtml } from '../../kit/html.js';
import { states } from '../../kit/states.js';
import { agoLabel, plural } from '../../kit/format.js';
import { isAbort } from '../../core/api.js';
import {
  seedRuns, applyFanout, liveRuns, branchRows, runCounts, STATE_TAG, runLabel, branchLines, callIdFor,
} from './helpers.js';
import { meetingHtml } from './meeting.js';

const RECENT_CAP = 30;

export default {
  id: 'ORCHESTRATION',
  sub: 'parallel fan-out',
  css: true,
  thread: false,

  async mount(root, ctx) {
    const st = { runs: {}, recent: null, recentError: null, snapError: null, tx: null, txBranch: '', txBusy: false, txError: '' };

    root.dataset.state = 'populated';
    setHtml(root, html`
      <div class="or-note" id="orNote" role="status" hidden></div>
      <div class="card"><div class="lbl" id="orLiveLbl">Active runs</div><div id="orLive" data-region></div></div>
      <div class="card or-gap" id="orTxCard" hidden><div class="lbl">Transcript</div><div id="orTx" data-region></div></div>
      <div class="card or-gap"><div class="lbl">Recent runs</div><div id="orRecent" data-region></div></div>
      <div class="muted or-foot">Branches of one delegate_parallel call start together, each in its own thread. The events carry no per-branch tool count and no queue, so this screen shows none. Recent runs and transcripts are read from the persisted office log, so they survive a restart; the live board does not.</div>`);
    const $ = (id) => root.querySelector('#' + id);
    const liveEl = $('orLive');
    const recentEl = $('orRecent');
    const txEl = $('orTx');
    const txCard = $('orTxCard');
    const noteEl = $('orNote');

    ctx.chrome.setActions([{
      id: 'refresh', label: 'REFRESH',
      spec: 'Reads the runs in flight and the recent runs again. The board also updates by itself as events arrive.',
      onClick: () => loadAll(),
    }]);

    /* ---------------- live board ---------------- */
    function paintLive() {
      const runs = liveRuns(st.runs);
      ctx.chrome.setLive(runs.length > 0);
      if (st.snapError && !runs.length) {
        states.error(liveEl, st.snapError, { title: 'Could not read the activity snapshot', retry: () => loadAll() });
        return;
      }
      if (!runs.length) {
        states.empty(liveEl, 'Nothing is fanned out right now.', { hint: 'A run appears here the moment a delegate_parallel call starts, and moves to Recent runs when every branch has reported.' });
        return;
      }
      liveEl.dataset.state = 'populated';
      setHtml(liveEl, html`${runs.map((run) => {
        const c = runCounts(run);
        return html`<div class="or-run" data-run="${run.id}">
          <div class="or-runh"><b>Run ${runLabel(run.id)}</b><span class="muted">${plural(c.total, 'branch', 'branches')}, ${c.done} done${c.failed ? ', ' + c.failed + ' failed' : ''}, ${c.running} running</span></div>
          ${branchRows(run).map((r) => html`<div class="os-row or-branch" data-state="${r.state}">
            <span class="tag ${STATE_TAG[r.state]}">${r.state}</span>
            <span class="rt"><b>${r.agent}</b> <span class="muted">${r.model}${r.task ? ' · ' + r.task : ''}${r.error ? ' · ' + r.error : ''}</span></span>
            ${r.elapsed !== null ? html`<span class="muted mono">${r.elapsed.toFixed(1)}s</span>` : ''}
            <span class="os-row-actions"><button type="button" class="os-btn" data-tx-run="${run.id}" data-tx-index="${String(r.index)}" data-spec="Opens this branch's own reasoning and tool calls from the office log, joined by its call_id.">TRANSCRIPT</button></span>
          </div>`)}
        </div>`;
      })}`);
    }

    /* ---------------- recent runs ---------------- */
    function paintRecent() {
      if (st.recentError) {
        states.error(recentEl, st.recentError, { title: 'Could not read recent runs', retry: () => loadRecent() });
        return;
      }
      if (st.recent === null) {
        states.loading(recentEl, 'Reading recent runs');
        return;
      }
      if (!st.recent.length) {
        states.empty(recentEl, 'No multi-agent runs recorded yet.', { hint: 'Runs are recorded when a delegate_parallel call finishes its first branch.' });
        return;
      }
      recentEl.dataset.state = 'populated';
      setHtml(recentEl, html`${st.recent.slice(0, RECENT_CAP).map((m) => {
        const done = m.finished >= m.branches && m.branches > 0;
        const bad = done && m.succeeded < m.finished;
        return html`<div class="os-row" data-run="${m.run_id}">
          <span class="tag ${!done ? 'warn' : bad ? 'bad' : 'ok'}">${!done ? 'running' : bad ? 'partly failed' : 'done'}</span>
          <span class="rt">${m.agents.filter(Boolean).join(', ')} <span class="muted">· ${plural(m.branches || 0, 'branch', 'branches')}, ${m.succeeded || 0} ok · ${agoLabel(m.ended || m.started)}</span></span>
          <span class="os-row-actions"><button type="button" class="os-btn" data-tx-run="${m.run_id}" data-spec="Opens the merged conversation for this run: every branch's task, tool calls and outcome in time order.">TRANSCRIPT</button></span>
        </div>`;
      })}`);
    }

    /* ---------------- transcript ---------------- */
    function paintTx() {
      txCard.hidden = st.tx === null && !st.txBusy && !st.txError;
      if (txCard.hidden) return;
      if (st.txBusy) {
        states.loading(txEl, 'Reading the transcript');
        return;
      }
      if (st.txError) {
        states.error(txEl, st.txError, { title: 'Could not read the transcript' });
        return;
      }
      const m = st.tx;
      const lines = branchLines(m, st.txBranch);
      const branches = Array.isArray(m.branches) ? m.branches : [];
      if (!branches.length) {
        states.empty(txEl, 'The log has no record of this run.', { hint: 'It may have been cleared, or the run never reported a branch.' });
        return;
      }
      txEl.dataset.state = 'populated';
      setHtml(txEl, html`
        <div class="or-txh"><b>Run ${runLabel(m.run_id)}</b>
          <span class="or-txtabs">
            <button type="button" class="os-btn" aria-pressed="${String(st.txBranch === '')}" data-tx-branch="" data-spec="Shows every branch of this run merged in time order.">ALL</button>
            ${branches.map((b) => html`<button type="button" class="os-btn" aria-pressed="${String(st.txBranch === b.call_id)}" data-tx-branch="${b.call_id || ''}" ${b.call_id ? '' : 'disabled'} data-spec="Shows only this branch, joined by its call_id.">${b.agent}</button>`)}
          </span>
          <button type="button" class="os-btn" data-tx-close data-spec="Closes the transcript.">CLOSE</button></div>
        ${meetingHtml(lines, m.truncated)}`);
    }

    async function openTranscript(runId, index) {
      st.tx = null;
      st.txError = '';
      st.txBusy = true;
      st.txBranch = '';
      paintTx();
      txCard.scrollIntoView({ block: 'nearest' });
      try {
        const m = await ctx.api.get('/api/office_log?meeting=' + encodeURIComponent(runId));
        if (ctx.signal.aborted) return;
        st.tx = m;
        if (index !== undefined) st.txBranch = callIdFor(m, index);
      } catch (err) {
        if (isAbort(err)) return;
        st.txError = err;
      }
      st.txBusy = false;
      paintTx();
    }

    /* ---------------- reads ---------------- */
    async function loadSnapshot() {
      try {
        const snap = await ctx.api.get('/api/activity');
        if (ctx.signal.aborted) return;
        st.snapError = null;
        /* keep the finished flag of runs already seen; a snapshot only lists runs in flight */
        const fresh = seedRuns(snap && snap.fanouts);
        st.runs = { ...fresh };
      } catch (err) {
        if (isAbort(err)) return;
        st.snapError = err;
      }
      paintLive();
    }

    async function loadRecent() {
      try {
        const r = await ctx.api.get('/api/office_log?meetings=1');
        if (ctx.signal.aborted) return;
        st.recent = Array.isArray(r.meetings) ? r.meetings : [];
        st.recentError = null;
      } catch (err) {
        if (isAbort(err)) return;
        st.recentError = err;
      }
      paintRecent();
    }

    async function loadAll() {
      states.loading(liveEl, 'Reading the activity snapshot');
      await Promise.all([loadSnapshot(), loadRecent()]);
    }

    /* ---------------- events ---------------- */
    ctx.events.on('delegate_fanout', (evt) => {
      applyFanout(st.runs, evt);
      paintLive();
      if (evt.finished) loadRecent();
    });
    ctx.events.onResync(() => loadAll());
    ctx.events.onStatus((s) => {
      noteEl.hidden = s !== 'error';
      noteEl.textContent = s === 'error' ? 'The live event stream dropped. The board may be behind until it reconnects.' : '';
    });

    root.addEventListener('click', (e) => {
      const tx = e.target.closest('[data-tx-run]');
      if (tx) {
        openTranscript(tx.dataset.txRun, tx.dataset.txIndex !== undefined ? Number(tx.dataset.txIndex) : undefined);
        return;
      }
      const br = e.target.closest('[data-tx-branch]');
      if (br) {
        st.txBranch = br.dataset.txBranch;
        paintTx();
        return;
      }
      if (e.target.closest('[data-tx-close]')) {
        st.tx = null;
        st.txError = '';
        paintTx();
      }
    });

    paintTx();
    await loadAll();
  },

  async refresh(ctx, reason) {
    if (reason === 'manual') ctx.notify({ level: 'info', title: 'ORCHESTRATION', detail: 'The board follows events by itself. REFRESH reads it again.', ttl: 3000 });
  },
};
