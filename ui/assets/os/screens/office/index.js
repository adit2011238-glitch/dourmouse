/* OFFICE: the fourteen agents as a floor plan (plan 1.7).

   Floor: eight rooms, one desk per agent. A desk is Working (a skill is computing or the agent owns a running job),
   Needs you (a sign-in, an approval or a job waiting for you) or Idle. An agent that is part of a running split task
   sits in the Meeting room. A dashed line with a short bubble joins two desks for a real agent-to-agent message from
   the last minute and a half. The latest events run underneath.
   Team: the same fourteen as a list with what each one is for and how its capability tests went.
   Everything comes from GET /api/os/office/floor, GET /api/os/agents and the shared event stream. Nothing is drawn
   that the server did not report: no sample agents, no invented states. Feed polls are not agent runs and never light
   a desk (CC-1). */

import { html, setHtml } from '../../kit/html.js';
import { states } from '../../kit/states.js';
import { clock } from '../../kit/format.js';
import { isAbort } from '../../core/api.js';
import {
  STATUS_WORD, TICKER_SHOWN, initials, summaryLine, visibleLines, bubbleText, nameResolver, addTicker, tickerFromActivity,
  tickerFromFanout, tickerFromLines, teamRows, doingText, testsText,
} from './helpers.js';

export default {
  id: 'OFFICE',
  sub: 'the agents at work',
  css: true,
  thread: false,

  async mount(root, ctx) {
    const st = {
      view: ctx.prefs.get('view') === 'team' ? 'team' : 'floor', floor: null, floorError: null, profiles: null, aliases: {},
      sel: '', ticker: [], seen: {}, runs: {}, knownLines: new Set(),
    };
    const nameOf = (key) => nameResolver(st.floor ? st.floor.agents : [], st.aliases)(key);

    root.dataset.state = 'populated';
    setHtml(root, html`<div class="of">
      <div class="of-bar"><div class="segs" role="tablist" aria-label="Office view">
        <button type="button" class="seg" role="tab" data-view="floor" aria-selected="true">Floor</button>
        <button type="button" class="seg" role="tab" data-view="team" aria-selected="false">Team</button></div>
        <span class="of-sum" id="ofSum" role="status"></span></div>
      <div class="of-note" id="ofNote" role="status" hidden></div>
      <div class="of-cols"><div class="of-main" id="ofMain" data-region></div><aside class="of-side" id="ofSide" hidden aria-label="Agent details"></aside></div>
      <section class="of-sec" id="ofTickSec"><h2 class="of-h">Latest</h2><div class="of-box" id="ofTick"></div></section></div>`);
    const $ = (id) => root.querySelector('#' + id);
    const mainEl = $('ofMain');
    const sideEl = $('ofSide');
    const sumEl = $('ofSum');
    const tickEl = $('ofTick');
    const noteEl = $('ofNote');

    /* ---------------- painting ---------------- */
    function paintTabs() {
      root.querySelectorAll('[data-view]').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.view === st.view)));
    }

    function desk(a) {
      return html`<button type="button" class="of-desk" data-agent="${a.id}" data-status="${a.status}" aria-pressed="${String(st.sel === a.id)}"
        aria-label="${a.name}, ${STATUS_WORD[a.status]}${a.doing ? ', ' + doingText(a, nameOf) : ''}">
        <span class="of-av" aria-hidden="true">${initials(a.name)}</span><span class="of-screen" aria-hidden="true"></span>
        <span class="of-name">${a.name}</span>${a.calls > 1 ? html`<span class="of-calls">${String(a.calls)} runs</span>` : ''}</button>`;
    }

    function paintFloor() {
      const byId = {};
      for (const a of st.floor.agents) byId[a.id] = a;
      setHtml(mainEl, html`<div class="of-plan" id="ofPlan">
        <div class="of-rooms">${st.floor.rooms.map((r) => html`<section class="of-room" data-room="${r.id}" data-empty="${String(!r.agents.length)}">
          <h3 class="of-rn">${r.name}</h3>
          <div class="of-desks">${r.agents.length ? r.agents.map((id) => desk(byId[id])) : html`<span class="of-vacant">${r.id === 'meeting' ? 'Empty' : 'In the meeting room'}</span>`}</div></section>`)}</div>
        <svg class="of-wires" id="ofWires" aria-hidden="true"></svg></div>`);
      requestAnimationFrame(drawLines);
    }

    function drawLines() {
      const plan = root.querySelector('#ofPlan');
      const svg = root.querySelector('#ofWires');
      if (!plan || !svg || !st.floor) return;
      const box = plan.getBoundingClientRect();
      const lines = visibleLines(st.floor.lines);
      const parts = [];
      lines.forEach((l, i) => {
        const a = plan.querySelector('[data-agent="' + CSS.escape(l.from) + '"] .of-av');
        const b = plan.querySelector('[data-agent="' + CSS.escape(l.to) + '"] .of-av');
        if (!a || !b) return;
        const ra = a.getBoundingClientRect();
        const rb = b.getBoundingClientRect();
        const x1 = ra.left + ra.width / 2 - box.left;
        const y1 = ra.top + ra.height / 2 - box.top;
        const x2 = rb.left + rb.width / 2 - box.left;
        const y2 = rb.top + rb.height / 2 - box.top;
        const f = 0.35 + 0.25 * (i % 3); /* each bubble sits at its own point along its wire so two lines never stack them */
        const mx = x1 + (x2 - x1) * f;
        const my = y1 + (y2 - y1) * f;
        const text = bubbleText(l.subject);
        const w = Math.min(220, 14 + text.length * 6);
        parts.push({ x1, y1, x2, y2, mx, my, text, w });
      });
      svg.setAttribute('width', String(box.width));
      svg.setAttribute('height', String(box.height));
      setSvg(svg, parts);
    }

    function setSvg(svg, parts) {
      svg.replaceChildren();
      const ns = 'http://www.w3.org/2000/svg';
      for (const p of parts) {
        const line = document.createElementNS(ns, 'line');
        for (const [k, v] of Object.entries({ x1: p.x1, y1: p.y1, x2: p.x2, y2: p.y2 })) line.setAttribute(k, String(v));
        line.setAttribute('class', 'of-wire');
        svg.append(line);
        const g = document.createElementNS(ns, 'g');
        g.setAttribute('class', 'of-bubble');
        const rect = document.createElementNS(ns, 'rect');
        for (const [k, v] of Object.entries({ x: p.mx - p.w / 2, y: p.my - 11, width: p.w, height: 22, rx: 6 })) rect.setAttribute(k, String(v));
        const t = document.createElementNS(ns, 'text');
        t.setAttribute('x', String(p.mx));
        t.setAttribute('y', String(p.my + 4));
        t.setAttribute('text-anchor', 'middle');
        t.textContent = p.text;
        g.append(rect, t);
        svg.append(g);
      }
    }

    function paintTeam() {
      if (!st.profiles) {
        states.loading(mainEl, 'Reading the team');
        return;
      }
      const rows = teamRows(st.profiles, st.floor ? st.floor.agents : []);
      mainEl.dataset.state = 'populated';
      setHtml(mainEl, html`<div class="of-box of-team">${rows.map((r) => html`<button type="button" class="of-row" data-agent="${r.id}" aria-pressed="${String(st.sel === r.id)}">
        <i class="of-dot" data-status="${r.status}" aria-hidden="true"></i>
        <span class="of-row-t"><b>${r.name}</b><span class="of-row-s">${r.job}</span></span>
        <span class="of-row-m">${r.status === 'idle' ? r.tests : STATUS_WORD[r.status]}</span></button>`)}</div>`);
    }

    function paintMain() {
      paintTabs();
      if (st.floorError && !st.floor) {
        states.error(mainEl, st.floorError, { title: 'Could not read the office', retry: () => load() });
        return;
      }
      if (!st.floor) {
        states.loading(mainEl, 'Reading the office');
        return;
      }
      mainEl.dataset.state = 'populated';
      sumEl.textContent = summaryLine(st.floor.counts);
      ctx.chrome.setSub(summaryLine(st.floor.counts));
      if (st.view === 'team') paintTeam(); else paintFloor();
      paintSide();
    }

    function paintSide() {
      const a = st.floor && st.floor.agents.find((x) => x.id === st.sel);
      sideEl.hidden = !a;
      root.querySelector('.of-cols').dataset.side = a ? 'open' : 'closed';
      if (!a) return;
      const p = (st.profiles || []).find((x) => x.id === a.id);
      const doing = doingText(a, nameOf);
      setHtml(sideEl, html`<div class="of-sh"><h2 class="of-sn">${a.name}</h2><button type="button" class="of-x" data-close aria-label="Close details">×</button></div>
        <p class="of-sj">${a.job}</p>
        <div class="of-kv"><span>Status</span><b>${STATUS_WORD[a.status]}${a.meeting ? ', in the meeting room' : ''}</b></div>
        ${doing ? html`<div class="of-kv"><span>Right now</span><b>${doing}</b></div>` : ''}
        ${a.calls ? html`<div class="of-kv"><span>Runs</span><b>${String(a.calls)}</b></div>` : ''}
        ${p ? html`<div class="of-kv"><span>Model</span><b>${p.model || 'Default'}</b></div>
          <div class="of-kv"><span>Tools</span><b>${String(p.tool_count)}</b></div>
          <div class="of-kv"><span>Tests</span><b>${testsText(p)}</b></div>
          <h3 class="of-h3">Promises</h3><ul class="of-list">${(p.contract || []).map((c) => html`<li>${c}</li>`)}</ul>
          <h3 class="of-h3">Skills</h3><p class="of-sj">${(p.skills || []).filter((s) => !s.compat_only).map((s) => s.name).join(', ')}</p>` : ''}`);
    }

    function paintTicker() {
      const items = st.ticker.slice(0, TICKER_SHOWN);
      if (!items.length) {
        tickEl.dataset.state = 'empty';
        setHtml(tickEl, html`<div class="of-tick-empty">Nothing has happened since the server started.</div>`);
        return;
      }
      tickEl.dataset.state = 'populated';
      setHtml(tickEl, html`${items.map((i) => html`<div class="of-tick"><span class="of-tick-t">${clock(i.at).slice(0, 5)}</span><span>${i.text}</span></div>`)}`);
    }

    /* ---------------- reads ---------------- */
    async function load() {
      const [fl, ag] = await Promise.allSettled([ctx.api.get('/api/os/office/floor'), st.profiles ? Promise.resolve(null) : ctx.api.get('/api/os/agents')]);
      if (ctx.signal.aborted) return;
      if (fl.status === 'fulfilled') {
        st.floor = fl.value;
        st.floorError = null;
      } else if (!isAbort(fl.reason)) {
        st.floorError = fl.reason;
      }
      if (ag.status === 'fulfilled' && ag.value) {
        st.profiles = ag.value.agents;
        st.aliases = ag.value.aliases || {};
      }
      if (st.floor) {
        for (const t of tickerFromLines(st.floor.lines, nameOf, st.knownLines)) addTicker(st.ticker, t);
        st.ticker.sort((x, y) => y.at - x.at);
      }
      paintMain();
      paintTicker();
    }

    let pending = 0;
    function soon() {
      if (pending) return;
      pending = setTimeout(() => { pending = 0; load(); }, 500);
    }

    /* ---------------- events ---------------- */
    ctx.events.on('agent_activity', (evt) => {
      for (const t of tickerFromActivity(evt, st.seen, nameOf)) addTicker(st.ticker, t);
      paintTicker();
      soon();
    });
    ctx.events.on('delegate_fanout', (evt) => {
      for (const t of tickerFromFanout(evt, st.runs)) addTicker(st.ticker, t);
      paintTicker();
      soon();
    });
    ctx.events.onResync(() => load());
    ctx.events.onStatus((s) => {
      noteEl.hidden = s !== 'error';
      noteEl.textContent = s === 'error' ? 'The live event stream dropped. Desks may be behind until it reconnects.' : '';
    });
    ctx.every(10000, () => load());
    const ro = typeof ResizeObserver === 'function' ? new ResizeObserver(() => drawLines()) : null;
    if (ro) ro.observe(root);
    ctx.signal.addEventListener('abort', () => { clearTimeout(pending); if (ro) ro.disconnect(); });

    root.addEventListener('click', (e) => {
      const v = e.target.closest('[data-view]');
      if (v) {
        st.view = v.dataset.view;
        ctx.prefs.set('view', st.view);
        paintMain();
        return;
      }
      const a = e.target.closest('[data-agent]');
      if (a) {
        st.sel = st.sel === a.dataset.agent ? '' : a.dataset.agent;
        paintMain();
        const again = root.querySelector('[data-agent="' + CSS.escape(a.dataset.agent) + '"]');
        if (again) again.focus();
        return;
      }
      if (e.target.closest('[data-close]')) {
        st.sel = '';
        paintMain();
      }
    });
    root.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && st.sel) {
        st.sel = '';
        paintMain();
      }
    });

    paintTicker();
    await load();
  },

  async refresh(ctx, reason) {
    if (reason === 'manual') ctx.notify({ level: 'info', title: 'Office', detail: 'Desks follow events by themselves.', ttl: 3000 });
  },
};
