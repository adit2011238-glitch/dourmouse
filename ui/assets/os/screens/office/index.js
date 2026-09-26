/* OFFICE: the agent roster as a building. Three real reads, each once:
   GET /api/os/office/floors (the roster grouped into floors; the rule is
   documented on the server and shown here), GET /api/activity (status per
   agent, runs in flight) and then the shared event stream (agent_activity and
   delegate_fanout). Meetings persist in the office log
   (GET /api/office_log?meetings=1 and ?meeting=<run>).

   Statuses are the ones the tracker really reports: idle, computing (drawn as
   working) and auth. "In meeting" is derived from a branch of a running
   fan-out. There is no always-on "live" status because no source reports it,
   so none is drawn. No sprite art: a desk is a card. */

import { html, setHtml } from '../../kit/html.js';
import { states } from '../../kit/states.js';
import { agoLabel, plural } from '../../kit/format.js';
import { isAbort } from '../../core/api.js';
import {
  seedAgents, applyAgentDelta, seedRuns, applyFanout, inMeeting, deskStatus, DESK_WORD, liveRuns, runLabel, trimTask,
} from '../orchestration/helpers.js';
import { meetingHtml } from '../orchestration/meeting.js';

export default {
  id: 'OFFICE',
  sub: 'floors and agents',
  css: true,
  thread: false,

  async mount(root, ctx) {
    const st = {
      floors: null, floorsError: null, total: 0, unassigned: [], unknown: [], rule: '',
      agents: {}, runs: {}, snapError: null, sel: Number(ctx.prefs.get('floor')) || 0,
      follow: ctx.prefs.get('follow') === 'true', agent: '', meetings: null,
      txRun: '', tx: null, txBusy: false, txError: null,
    };

    root.dataset.state = 'populated';
    setHtml(root, html`
      <div class="of-note" id="ofNote" role="status" hidden></div>
      <div class="of-main" id="ofMain" data-region></div>
      <div class="card of-gap" id="ofTxCard" hidden><div class="lbl">Meeting transcript</div><div id="ofTx" data-region></div></div>
      <div class="muted of-foot" id="ofFoot"></div>`);
    const $ = (id) => root.querySelector('#' + id);
    const mainEl = $('ofMain');
    const txCard = $('ofTxCard');
    const txEl = $('ofTx');
    const noteEl = $('ofNote');
    const footEl = $('ofFoot');

    /* ---------------- derived ---------------- */
    const meeting = () => inMeeting(st.runs);
    const status = (name) => deskStatus(name, st.agents, meeting());
    const activeCount = (f) => f.agents.filter((a) => status(a.name) !== 'idle').length;

    function selectedMeeting() {
      const live = liveRuns(st.runs)[0];
      if (st.txRun) return st.txRun;
      if (live) return live.id;
      return st.meetings && st.meetings.length ? st.meetings[0].run_id : '';
    }

    function paintActions() {
      const target = selectedMeeting();
      ctx.chrome.setActions([
        {
          id: 'follow', label: 'FOLLOW', pressed: st.follow,
          spec: 'Pins the view to whichever floor has activity, so the building follows the work. It only moves the view; it starts and stops nothing.',
          onClick: () => {
            st.follow = !st.follow;
            ctx.prefs.set('follow', st.follow);
            if (st.follow) followBusiest();
            paintActions();
            paintMain();
          },
        },
        {
          id: 'transcripts', label: 'TRANSCRIPTS', disabled: !target,
          title: target ? '' : 'No meeting has been recorded yet',
          spec: 'Opens the merged conversation for the selected meeting: the fan-out running now, else the latest recorded one. It reads the office log and changes nothing.',
          onClick: () => openTranscript(selectedMeeting()),
        },
      ]);
    }

    function followBusiest() {
      if (!st.floors) return;
      let best = -1;
      let bestCount = 0;
      st.floors.forEach((f, i) => {
        const n = activeCount(f);
        if (n > bestCount) {
          best = i;
          bestCount = n;
        }
      });
      if (best >= 0) st.sel = best;
    }

    /* ---------------- painting ---------------- */
    function paintMain() {
      if (st.floorsError) {
        states.error(mainEl, st.floorsError, { title: 'Could not read the roster', retry: () => loadAll() });
        return;
      }
      if (!st.floors) {
        states.loading(mainEl, 'Reading the roster');
        return;
      }
      if (!st.floors.length || !st.total) {
        states.empty(mainEl, 'The roster is empty.', { hint: 'No agents are registered on this server.' });
        return;
      }
      if (st.sel >= st.floors.length || st.sel < 0) st.sel = 0;
      const floor = st.floors[st.sel];
      const mt = meeting();
      const seated = [];
      for (const f of st.floors) for (const a of f.agents) if (mt[a.name]) seated.push(a.name);
      const picked = st.agent ? floor.agents.find((a) => a.name === st.agent) : null;

      mainEl.dataset.state = 'populated';
      setHtml(mainEl, html`<div class="of-wrap">
        <div class="of-bldg" role="group" aria-label="Floors">${st.floors.map((f, i) => html`<button type="button" class="floor" data-floor="${String(i)}" aria-current="${String(i === st.sel)}" data-spec="Floor ${String(i + 1)}, ${f.name}. ${plural(f.agents.length, 'agent')}. Opens this floor. ${String(activeCount(f))} of them are doing something right now.">
            <span class="fno">F${String(i + 1)}</span><span class="fname">${f.name}</span>
            <span class="fdots">${f.agents.map((a) => html`<i class="fd ${status(a.name)}" title="${a.name}: ${DESK_WORD[status(a.name)]}"></i>`)}</span></button>`)}</div>
        <div class="card of-floor">
          <div class="lbl">${floor.name} <span class="muted">${plural(floor.agents.length, 'agent')}</span></div>
          <div class="of-body">
            <div class="desks" role="group" aria-label="Agents on ${floor.name}">${floor.agents.map((a) => {
              const s = status(a.name);
              const cur = st.agents[a.name];
              return html`<button type="button" class="desk ${s}" data-agent="${a.name}" aria-pressed="${String(st.agent === a.name)}" data-spec="${a.name}. ${DESK_WORD[s]}. Opens what this agent last did and which model it uses.">
                <span class="av"></span><span class="who">${a.name}</span><span class="st">${DESK_WORD[s]}</span>${cur && cur.concurrent > 1 ? html`<span class="badge">x ${String(cur.concurrent)}</span>` : ''}</button>`;
            })}</div>
            <div class="meetroom" data-spec="Meeting room. An agent sits here while one of its branches of a real delegate_parallel run is still running, and leaves when the branch reports. It is driven by events only, so an empty room means nothing is fanned out.">
              <div class="meetroom-l">MEETING ROOM</div>
              <div class="meetroom-seats">${seated.length ? seated.map((n) => html`<div class="seat"><div class="av"></div><div class="nm">${n}</div></div>`) : html`<div class="of-empty">no fan-out running</div>`}</div>
            </div>
          </div>
          ${picked ? agentCard(picked) : ''}
        </div></div>`);
      setFoot();
    }

    function agentCard(a) {
      const cur = st.agents[a.name];
      const last = cur && cur.last;
      return html`<div class="of-agent" data-agent-card="${a.name}">
        <div class="of-ah"><b>${a.name}</b><span class="tag ${status(a.name) === 'work' ? 'warn' : status(a.name) === 'meet' ? 'ok' : status(a.name) === 'auth' ? 'bad' : ''}">${DESK_WORD[status(a.name)]}</span></div>
        <div class="muted">${a.description || 'No description registered.'}</div>
        <div class="kv"><span>Model</span><b>${a.model || 'not reported'}</b></div>
        <div class="kv"><span>Tools</span><b>${String(a.tool_count)}</b></div>
        <div class="kv"><span>Runs in flight</span><b>${cur ? String(cur.concurrent) : '0'}</b></div>
        <div class="kv"><span>Last activity</span><b>${last ? last.tool + (last.at ? ' · ' + agoLabel(last.at) : '') : 'none since the server started'}</b></div>
        ${last && last.args ? html`<div class="muted mono of-args">${trimTask(last.args, 200)}</div>` : ''}
        ${ctx.host.kind !== 'browser' ? html`<button type="button" class="os-btn" data-open-agent="${a.name}" data-spec="Opens this agent's own live window, which streams its reasoning tagged by call_id.">OPEN AGENT WINDOW</button>` : ''}
      </div>`;
    }

    function setFoot() {
      const bits = [];
      bits.push(st.total + ' agents on the roster, ' + (st.floors.length) + ' floors.');
      if (st.unassigned.length) bits.push(st.unassigned.length + ' not named by any floor and shown on Unassigned: ' + st.unassigned.join(', ') + '.');
      if (st.unknown.length) bits.push('The floor table names ' + st.unknown.length + ' agent(s) the roster does not have: ' + st.unknown.join(', ') + '.');
      if (st.snapError) bits.push('Activity could not be read (' + st.snapError.message + '), so every desk shows idle.');
      footEl.textContent = bits.join(' ') + ' ' + st.rule + ' Status is idle, working, needs sign-in or in meeting; no always-on status is reported by any source, so none is drawn.';
      ctx.chrome.setSub(st.floors.length + ' floors · ' + st.total + ' agents');
    }

    /* ---------------- transcript ---------------- */
    function paintTx() {
      txCard.hidden = !st.txBusy && !st.tx && !st.txError;
      if (txCard.hidden) return;
      if (st.txBusy) {
        states.loading(txEl, 'Reading the meeting');
        return;
      }
      if (st.txError) {
        states.error(txEl, st.txError, { title: 'Could not read the meeting' });
        return;
      }
      const m = st.tx;
      if (!m.branches || !m.branches.length) {
        states.empty(txEl, 'The log has no record of this meeting.');
        return;
      }
      txEl.dataset.state = 'populated';
      setHtml(txEl, html`<div class="of-txh"><b>Run ${runLabel(m.run_id)}</b><span class="muted">${(m.branches || []).map((b) => b.agent).join(', ')}</span>
        <button type="button" class="os-btn" data-tx-close data-spec="Closes the transcript.">CLOSE</button></div>${meetingHtml(m.lines || [], m.truncated)}`);
    }

    async function openTranscript(runId) {
      if (!runId) return;
      st.txRun = runId;
      st.tx = null;
      st.txError = null;
      st.txBusy = true;
      paintTx();
      try {
        const m = await ctx.api.get('/api/office_log?meeting=' + encodeURIComponent(runId));
        if (ctx.signal.aborted) return;
        st.tx = m;
      } catch (err) {
        if (isAbort(err)) return;
        st.txError = err;
      }
      st.txBusy = false;
      paintTx();
    }

    /* ---------------- reads ---------------- */
    async function loadAll() {
      states.loading(mainEl, 'Reading the roster');
      const [fl, snap, mt] = await Promise.allSettled([
        ctx.api.get('/api/os/office/floors'),
        ctx.api.get('/api/activity'),
        ctx.api.get('/api/office_log?meetings=1'),
      ]);
      if (ctx.signal.aborted) return;
      if (fl.status === 'fulfilled') {
        st.floors = fl.value.floors;
        st.total = fl.value.total;
        st.unassigned = fl.value.unassigned || [];
        st.unknown = fl.value.unknown || [];
        st.rule = fl.value.rule || '';
        st.floorsError = null;
      } else if (!isAbort(fl.reason)) {
        st.floorsError = fl.reason;
      }
      if (snap.status === 'fulfilled') {
        st.agents = seedAgents(snap.value.agents);
        st.runs = seedRuns(snap.value.fanouts);
        st.snapError = null;
      } else if (!isAbort(snap.reason)) {
        st.snapError = snap.reason;
      }
      st.meetings = mt.status === 'fulfilled' && Array.isArray(mt.value.meetings) ? mt.value.meetings : (st.meetings || []);
      if (st.follow) followBusiest();
      paintActions();
      paintMain();
      ctx.chrome.setLive(liveRuns(st.runs).length > 0);
    }

    /* ---------------- events ---------------- */
    ctx.events.on('agent_activity', (evt) => {
      applyAgentDelta(st.agents, evt);
      if (st.follow) followBusiest();
      paintMain();
    });
    ctx.events.on('delegate_fanout', (evt) => {
      applyFanout(st.runs, evt);
      if (st.follow) followBusiest();
      paintMain();
      paintActions();
      ctx.chrome.setLive(liveRuns(st.runs).length > 0);
      if (evt.finished) {
        ctx.api.get('/api/office_log?meetings=1').then((r) => {
          if (ctx.signal.aborted) return;
          st.meetings = Array.isArray(r.meetings) ? r.meetings : st.meetings;
          paintActions();
        }).catch(() => {});
      }
    });
    ctx.events.onResync(() => loadAll());
    ctx.events.onStatus((s) => {
      noteEl.hidden = s !== 'error';
      noteEl.textContent = s === 'error' ? 'The live event stream dropped. Statuses may be behind until it reconnects.' : '';
    });

    root.addEventListener('click', (e) => {
      const fl = e.target.closest('[data-floor]');
      if (fl) {
        st.sel = Number(fl.dataset.floor);
        st.agent = '';
        ctx.prefs.set('floor', st.sel);
        paintMain();
        return;
      }
      const ag = e.target.closest('[data-agent]');
      if (ag) {
        st.agent = st.agent === ag.dataset.agent ? '' : ag.dataset.agent;
        paintMain();
        const again = root.querySelector('[data-agent="' + CSS.escape(ag.dataset.agent) + '"]');
        if (again) again.focus();
        return;
      }
      const oa = e.target.closest('[data-open-agent]');
      if (oa) {
        if (!ctx.host.openAgent(oa.dataset.openAgent)) ctx.notify({ level: 'warn', title: 'OFFICE', detail: 'This window cannot open a separate agent window.' });
        return;
      }
      if (e.target.closest('[data-tx-close]')) {
        st.tx = null;
        st.txError = null;
        st.txRun = '';
        paintTx();
        paintActions();
      }
    });
    root.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && (st.agent || !txCard.hidden)) {
        if (st.agent) st.agent = '';
        else {
          st.tx = null;
          st.txError = null;
          st.txRun = '';
        }
        paintMain();
        paintTx();
      }
    });

    paintActions();
    paintTx();
    await loadAll();
  },

  async refresh(ctx, reason) {
    if (reason === 'manual') ctx.notify({ level: 'info', title: 'OFFICE', detail: 'Statuses follow events by themselves.', ttl: 3000 });
  },
};
