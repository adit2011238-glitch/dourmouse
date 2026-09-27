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
  RUN_CAP, seedRuns, applyFanout, liveRuns, branchRows, runCounts, STATE_TAG, runLabel, branchLines, callIdFor,
} from './helpers.js';
import { meetingHtml } from './meeting.js';
import {
  seedAllHands, applyAllHands, counts as ahCounts, newestRuns, activeCount, runWord, runTone, brainTag, preview, goalLine,
} from './allhands.js';

const RECENT_CAP = 30;
const RUN_DRAWN = RUN_CAP;

export default {
  id: 'ORCHESTRATION',
  sub: 'parallel runs',
  css: true,
  thread: false,

  async mount(root, ctx) {
    const st = { evicted: 0, all: {}, allErr: null, allLoaded: false, allOpen: new Set(), runs: {}, recent: null, recentError: null, snapError: null, tx: null, txBranch: '', txBusy: false, txError: '' };

    root.dataset.state = 'populated';
    setHtml(root, html`
      <div class="or-note" id="orNote" role="status" hidden></div>
      <div class="card"><div class="lbl" id="orLiveLbl">Running in parallel now</div><div id="orLive" data-region></div></div>
      <div class="card or-gap"><div class="lbl">All hands (started with /all in HOME)</div><div id="orAll" data-region></div></div>
      <div class="card or-gap" id="orTxCard" hidden><div class="lbl">Transcript</div><div id="orTx" data-region></div></div>
      <div class="card or-gap"><div class="lbl">Recent runs</div><div id="orRecent" data-region></div></div>
      <div class="muted or-foot">When the orchestrator gives one job to several agents at once, each agent works in its own thread and reports back on its own. The server does not report how many tools a branch has used or what is waiting in a queue, so this screen shows neither. Finished runs and their transcripts are read from the saved office log, so they survive a restart. The live board and the all hands list start empty after a restart.</div>`);
    const $ = (id) => root.querySelector('#' + id);
    const liveEl = $('orLive');
    const allEl = $('orAll');
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
      root.dataset.state = st.snapError && st.recentError ? 'error' : 'populated';
      const runs = liveRuns(st.runs);
      ctx.chrome.setLive(runs.length > 0);
      if (st.snapError && !runs.length) {
        states.error(liveEl, st.snapError, { title: 'Could not read the activity snapshot', retry: () => loadAll() });
        return;
      }
      if (!runs.length) {
        states.empty(liveEl, 'Nothing is running in parallel right now.', { hint: 'When the orchestrator hands one job to several agents at once, each agent appears here as it starts. Finished runs move to Recent runs. To put every model on one goal, type /all and the goal in HOME.' });
        return;
      }
      liveEl.dataset.state = 'populated';
      setHtml(liveEl, html`${st.evicted ? html`<div class="muted or-more">The board draws the newest ${String(RUN_DRAWN)} runs. ${String(st.evicted)} older running ${st.evicted === 1 ? 'run is' : 'runs are'} not drawn.</div>` : ''}${runs.map((run) => {
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

    /* ---------------- all hands (/all) ---------------- */
    function paintAll() {
      const runs = newestRuns(st.all);
      ctx.chrome.setLive(liveRuns(st.runs).length > 0 || activeCount(st.all) > 0);
      if (!st.allLoaded && !st.allErr) {
        states.loading(allEl, 'Reading all hands runs');
        return;
      }
      if (st.allErr && !runs.length) {
        states.error(allEl, st.allErr, { title: 'Could not read the all hands runs', retry: () => loadAllHands() });
        return;
      }
      if (!runs.length) {
        states.empty(allEl, 'No all hands run since the server started.', { hint: 'In HOME, type /all and a goal. Every model this Mac can reach works on it at once and the answers are merged. The run shows up here while it works.' });
        return;
      }
      allEl.dataset.state = 'populated';
      setHtml(allEl, html`${runs.map((run) => {
        const c = ahCounts(run);
        const keySyn = run.id + '|synthesis';
        return html`<div class="or-run" data-ah-run="${run.id}">
          <div class="or-runh"><span class="tag ${runTone(run)}">${runWord(run)}</span><b>${goalLine(run)}</b>
            <span class="muted">${c.total ? c.done + ' of ' + plural(c.total, 'model') + ' answered' + (c.failed ? ', ' + c.failed + ' failed' : '') : 'starting'}${run.started ? ' · ' + agoLabel(run.started) : ''}</span>
            <span class="os-row-actions"><button type="button" class="os-btn" data-ah-window="${run.id}" data-spec="Opens this run in its own page, the same all hands page the classic console opened.">OPEN PAGE</button></span></div>
          ${Object.entries(run.brains || {}).map(([key, b]) => {
            const k = run.id + '|' + key;
            const open = st.allOpen.has(k);
            const body = b.status === 'error' ? b.error : b.result;
            return html`<div class="os-row or-branch">
              <span class="tag ${brainTag(b.status)}">${b.status}</span>
              <span class="rt"><b>${b.label || key}</b> ${body ? html`<span class="muted">${open ? '' : preview(body)}</span>` : ''}
                ${open && body ? html`<div class="or-full">${body}</div>` : ''}</span>
              ${typeof b.elapsed === 'number' ? html`<span class="muted mono">${b.elapsed.toFixed(1)}s</span>` : ''}
              ${body && String(body).length > 120 ? html`<span class="os-row-actions"><button type="button" class="os-btn" data-ah-toggle="${k}" aria-expanded="${String(open)}" data-spec="Shows or hides the full text this model returned.">${open ? 'HIDE' : 'SHOW'}</button></span>` : ''}
            </div>`;
          })}
          ${run.synthesis ? html`<div class="or-syn"><div class="who">Merged answer</div><div class="or-full">${st.allOpen.has(keySyn) || run.synthesis.length <= 600 ? run.synthesis : preview(run.synthesis, 600)}</div>
            ${run.synthesis.length > 600 ? html`<button type="button" class="os-btn" data-ah-toggle="${keySyn}" aria-expanded="${String(st.allOpen.has(keySyn))}" data-spec="Shows or hides the whole merged answer.">${st.allOpen.has(keySyn) ? 'HIDE' : 'SHOW ALL'}</button>` : ''}</div>` : ''}
          ${run.error ? html`<div class="muted or-err">${preview(run.error, 400)}</div>` : ''}
        </div>`;
      })}`);
    }

    /* ---------------- recent runs ---------------- */
    function paintRecent() {
      root.dataset.state = st.snapError && st.recentError ? 'error' : 'populated';
      if (st.recentError) {
        states.error(recentEl, st.recentError, { title: 'Could not read recent runs', retry: () => loadRecent() });
        return;
      }
      if (st.recent === null) {
        states.loading(recentEl, 'Reading recent runs');
        return;
      }
      if (!st.recent.length) {
        states.empty(recentEl, 'No parallel runs recorded yet.', { hint: 'A run is saved here once its first agent reports back.' });
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
    let offEsc = null;
    function paintTx() {
      txCard.hidden = st.tx === null && !st.txBusy && !st.txError;
      /* Esc closes the transcript, but only while one is open, so it never swallows the shell's own Esc */
      if (txCard.hidden && offEsc) { offEsc(); offEsc = null; }
      if (!txCard.hidden && !offEsc) {
        offEsc = ctx.keys.pushEsc(() => {
          st.tx = null;
          st.txError = '';
          st.txBusy = false;
          paintTx();
        });
      }
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

    async function loadAllHands() {
      try {
        const r = await ctx.api.get('/api/allhands');
        if (ctx.signal.aborted) return;
        st.all = seedAllHands(r && r.runs);
        st.allErr = null;
      } catch (err) {
        if (isAbort(err)) return;
        st.allErr = err;
      }
      st.allLoaded = true;
      paintAll();
    }

    async function loadAll() {
      states.loading(liveEl, 'Reading the activity snapshot');
      await Promise.all([loadSnapshot(), loadRecent(), loadAllHands()]);
    }

    /* ---------------- events ---------------- */
    ctx.events.on('delegate_fanout', (evt) => {
      const before = Object.keys(st.runs).filter((id) => !st.runs[id].finished);
      applyFanout(st.runs, evt);
      const gone = before.filter((id) => !(id in st.runs)).length;
      if (gone) st.evicted += gone;
      if (!liveRuns(st.runs).length) st.evicted = 0;
      paintLive();
      if (evt.finished) loadRecent();
    });
    ctx.events.on('allhands', (evt) => {
      const run = applyAllHands(st.all, evt);
      if (!run) return;
      paintAll();
      /* the start event has no goal or roster, and the final one has the elapsed times: read the list again */
      if (run.partial || (!evt.brain && evt.status === 'done')) loadAllHands();
    });
    ctx.events.onResync(() => loadAll());
    ctx.events.onStatus((s) => {
      noteEl.hidden = s !== 'error';
      noteEl.textContent = s === 'error' ? 'The live event stream dropped. The board may be behind until it reconnects.' : '';
    });

    root.addEventListener('click', (e) => {
      const win = e.target.closest('[data-ah-window]');
      if (win) {
        if (!ctx.host.openExternal('/all-hands?run=' + encodeURIComponent(win.dataset.ahWindow))) ctx.notify({ level: 'warn', title: 'ORCHESTRATION', detail: 'This window could not open a separate page.' });
        return;
      }
      const tg = e.target.closest('[data-ah-toggle]');
      if (tg) {
        const k = tg.dataset.ahToggle;
        if (st.allOpen.has(k)) st.allOpen.delete(k);
        else st.allOpen.add(k);
        paintAll();
        const again = root.querySelector('[data-ah-toggle="' + CSS.escape(k) + '"]');
        if (again) again.focus();
        return;
      }
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
