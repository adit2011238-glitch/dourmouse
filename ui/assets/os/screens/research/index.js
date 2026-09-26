/* RESEARCH: the research questions the pipeline has stored, the loop it runs, the
   evidence graph around a question or hypothesis, and every claim with the
   passage and source it was quoted from. All of it is read from the stored graph
   (GET /api/os/research/*). A stage is drawn "built" only when the server says its
   tool is registered; a claim is "quote matched" because every stored claim passed
   the verbatim check on the fetched page, not because a flag says "verified".

   Live: the server's graph event log is checked every 10 seconds while this screen
   is showing, and a finished turn of this thread re-reads everything. NEW QUESTION
   sends one message to the research agent after a confirm; EXPORT downloads what
   the server rendered. */

import { html, setHtml } from '../../kit/html.js';
import { states } from '../../kit/states.js';
import { flowSvg } from '../../kit/flow-svg.js';
import { confirmHere } from '../../kit/confirm-card.js';
import { mountThreadView } from '../../kit/thread-view.js';
import { agoLabel } from '../../kit/format.js';
import { isAbort } from '../../core/api.js';
import { loopBoxes, stagesSub, countsLine, claimTag, safeFilename, shorten } from './helpers.js';
import { SourceWidget } from './source-widget.js';
import { graphSvg } from './graph-svg.js';

const CLAIM_SHOW = 60;

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

export default {
  id: 'RESEARCH',
  sub: 'research pipeline',
  css: true,
  thread: true,

  async mount(root, ctx) {
    const st = {
      loop: null, qs: null, sel: '', detail: null, graphRoot: '', graph: null, openClaim: '',
      cursor: 0, busyPrev: false, loopErr: null,
    };
    root.dataset.state = 'populated';
    setHtml(root, html`
      <div id="rsConfirm"></div>
      <div class="card"><div class="lbl">Research questions</div><div id="rsQs" data-region></div></div>
      <div class="card rs-loop"><div class="lbl">The research loop</div><div id="rsLoop" data-region></div><div class="muted rs-loopnote" id="rsLoopNote"></div></div>
      <div class="grid2 rs-mid">
        <div class="card"><div class="lbl">Evidence graph</div><div class="rs-pick" id="rsPick"></div><div id="rsGraph" data-region></div></div>
        <div class="card"><div class="lbl">Claims</div><div id="rsClaims" data-region></div></div>
      </div>
      <div id="rsThread" class="rs-thread"></div>`);
    const $ = (id) => root.querySelector('#' + id);
    const confirmEl = $('rsConfirm');
    const qsEl = $('rsQs');
    const loopEl = $('rsLoop');
    const loopNote = $('rsLoopNote');
    const pickEl = $('rsPick');
    const graphEl = $('rsGraph');
    const claimsEl = $('rsClaims');
    const guard = () => ctx.signal.aborted;

    /* ---------------- stage bar ---------------- */
    const actions = () => [
      {
        id: 'new', label: 'NEW QUESTION', kind: 'primary', disabled: ctx.chat.busy(),
        title: ctx.chat.busy() ? 'Stop the run first' : '',
        spec: 'Asks the research agent to plan a new question. It opens a small form, asks you to confirm, then sends ONE message to the model in this thread: the agent decomposes the question and may fetch web pages.',
        onClick: () => newQuestion(),
      },
      {
        id: 'export', label: 'EXPORT', disabled: !st.sel,
        title: st.sel ? '' : 'Choose a question first',
        spec: 'Saves the chosen question as a markdown file: its sub-questions, every stored claim with the passage it was quoted from and its source URL, and its contradictions. The server renders the text; your browser saves it.',
        onClick: () => exportQuestion(),
      },
    ];

    /* ---------------- NEW QUESTION ---------------- */
    function newQuestion() {
      const form = el('form', 'card rs-new');
      const label = el('label', 'lbl', 'New research question');
      label.setAttribute('for', 'rsNewQ');
      const input = el('input');
      input.id = 'rsNewQ';
      input.type = 'text';
      input.maxLength = 500;
      input.placeholder = 'What do you want researched?';
      input.dataset.spec = 'The research question to send to the research agent.';
      const go = el('button', 'os-btn os-btn--primary', 'CONTINUE');
      go.type = 'submit';
      go.dataset.spec = 'Shows what will be sent and waits for your approval before anything is sent.';
      const cancel = el('button', 'os-btn', 'CANCEL');
      cancel.type = 'button';
      cancel.dataset.spec = 'Closes this form without sending anything.';
      const row = el('div', 'rs-newrow');
      row.append(input, go, cancel);
      form.append(label, row);
      const off = ctx.keys.pushEsc(() => close());
      const close = () => {
        off();
        confirmEl.replaceChildren();
      };
      cancel.addEventListener('click', close);
      form.addEventListener('keydown', (ev) => {
        if (ev.key === 'Escape') {
          ev.stopPropagation();
          close();
        }
      });
      form.addEventListener('submit', (ev) => {
        ev.preventDefault();
        const text = input.value.trim();
        if (!text) {
          input.focus();
          return;
        }
        off();
        confirmHere(confirmEl, 'Send this question to the research agent? "' + shorten(text, 200) + '" It will be planned into sub-questions, and the agent may fetch web pages and spend model credit.', async () => {
          ctx.chat.send(text, { focusAgent: 'research_info' });
        }, { onDone: () => setTimeout(() => confirmEl.replaceChildren(), 1500) });
      });
      confirmEl.replaceChildren(form);
      input.focus();
    }

    /* ---------------- EXPORT ---------------- */
    async function exportQuestion() {
      if (!st.sel) return;
      try {
        const r = await ctx.api.get('/api/os/research/export?question=' + encodeURIComponent(st.sel) + '&format=markdown');
        const blob = new Blob([r.content], { type: (r.mime || 'text/markdown') + ';charset=utf-8' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = safeFilename(r.filename);
        a.rel = 'noopener';
        root.append(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 4000);
        ctx.notify({ level: 'ok', title: 'Exported', detail: safeFilename(r.filename), ttl: 3000 });
      } catch (err) {
        if (!isAbort(err)) ctx.notify({ level: 'error', title: 'Could not export', detail: err && err.message });
      }
    }

    /* ---------------- questions ---------------- */
    function paintQs() {
      const d = st.qs;
      if (!d) return;
      if (!d.questions.length) {
        states.empty(qsEl, 'Nothing researched yet.', { hint: 'Press NEW QUESTION, or ask the research agent in the thread below.' });
        return;
      }
      const wrap = el('div', 'rs-qs');
      d.questions.forEach((qq) => {
        const row = el('button', 'os-row rs-q');
        row.type = 'button';
        row.dataset.spec = 'Chooses this question: its claims and its evidence graph are shown below.';
        row.setAttribute('aria-pressed', String(qq.id === st.sel));
        row.append(el('span', 'rt', qq.text));
        if (qq.stage) row.append(el('span', 'tag', qq.stage.toLowerCase().replace(/_/g, ' ')));
        row.append(el('span', 'muted', qq.claims + (qq.claims === 1 ? ' claim' : ' claims') + ' · ' + qq.sub_questions + ' sub'));
        if (qq.open_contradictions) row.append(el('span', 'tag warn', qq.open_contradictions + ' contested'));
        if (qq.open_tasks) row.append(el('span', 'tag', qq.open_tasks + ' open'));
        const when = agoLabel(qq.created_at);
        if (when) row.append(el('span', 'muted', when));
        row.addEventListener('click', () => select(qq.id));
        wrap.append(row);
      });
      states.populated(qsEl, wrap);
    }

    async function loadQs() {
      try {
        const d = await ctx.api.get('/api/os/research/questions');
        if (guard()) return;
        st.qs = d;
        states.clearStale(qsEl);
        if (!d.questions.some((x) => x.id === st.sel)) st.sel = d.questions.length ? d.questions[0].id : '';
        paintQs();
        ctx.chrome.setActions(actions());
        if (view) view.paintChrome();
        await Promise.all([loadDetail(), loadGraph()]);
      } catch (err) {
        if (isAbort(err)) return;
        if (st.qs) states.stale(qsEl, 'Could not refresh: ' + err.message);
        else states.error(qsEl, err, { title: 'Could not read the research questions', retry: () => refreshAll() });
      }
    }

    function select(id) {
      st.sel = id;
      st.graphRoot = '';
      st.openClaim = '';
      paintQs();
      ctx.chrome.setActions(actions());
      if (view) view.paintChrome();
      loadDetail();
      loadGraph();
    }

    /* ---------------- loop ---------------- */
    function paintLoop() {
      const lp = st.loop;
      if (!lp) return;
      ctx.chrome.setSub(stagesSub(lp));
      const back = lp.back && lp.back.built
        ? { label: 'backward edge: a contradiction reopens the question (research_follow_up, at most ' + lp.back.max_follow_ups + ' follow-ups per pass)' }
        : null;
      const svg = flowSvg({ layout: 'snake', boxes: loopBoxes(lp.stages), back, idPrefix: 'rs', label: 'The research loop: ' + lp.built + ' of ' + lp.total + ' stages have a registered tool' });
      states.populated(loopEl, svg);
      const off = lp.stages.filter((s) => !s.built).map((s) => s.title + ' (' + s.detail + ')');
      const note = 'Green stages have a registered tool (' + lp.built + ' of ' + lp.total + '). ' +
        (back ? 'The dashed loop is the backward edge and it is built. ' : 'The backward edge is not available on this server. ') +
        (off.length ? 'Not built: ' + off.join('; ') + '. ' : '') +
        (countsLine(lp.counts) ? 'Stored now: ' + countsLine(lp.counts) + '.' : '');
      loopNote.textContent = note;
    }

    async function loadLoop() {
      try {
        const d = await ctx.api.get('/api/os/research/loop');
        if (guard()) return;
        st.loop = d;
        st.loopErr = null;
        states.clearStale(loopEl);
        paintLoop();
      } catch (err) {
        if (isAbort(err)) return;
        if (st.loop) states.stale(loopEl, 'Could not refresh: ' + err.message);
        else states.error(loopEl, err, { title: 'Could not read the research loop', retry: () => loadLoop() });
      }
    }

    /* ---------------- claims ---------------- */
    function paintClaims() {
      const d = st.detail;
      if (!d) {
        if (!st.sel && st.qs) states.empty(claimsEl, 'Choose a question to see its claims.');
        return;
      }
      if (!d.claims.length) {
        states.empty(claimsEl, 'No claims are stored for this question yet.', { hint: d.stage ? 'Pipeline stage: ' + d.stage.toLowerCase().replace(/_/g, ' ') + '.' : 'The research agent has not extracted evidence for it.' });
        return;
      }
      const wrap = el('div', 'rs-claims');
      d.claims.slice(0, CLAIM_SHOW).forEach((c) => {
        const t = claimTag(c);
        const open = st.openClaim === c.id;
        const row = el('button', 'os-row rs-claim');
        row.type = 'button';
        row.dataset.spec = 'Shows or hides the source this claim was quoted from, with the stored passage.';
        row.setAttribute('aria-expanded', String(open));
        row.append(el('span', 'tag ' + t.tone, t.text), el('span', 'rt', c.text));
        row.addEventListener('click', () => {
          st.openClaim = open ? '' : c.id;
          paintClaims();
        });
        wrap.append(row);
        if (open) wrap.append(SourceWidget(c, { openExternal: (u) => ctx.host.openExternal(u) }));
      });
      if (d.claims.length > CLAIM_SHOW) wrap.append(el('div', 'muted', 'Showing ' + CLAIM_SHOW + ' of ' + d.claims.length + ' claims.'));
      if (d.contradictions && d.contradictions.length) {
        wrap.append(el('div', 'lbl rs-sub', 'Contradictions'));
        d.contradictions.forEach((x) => {
          const row = el('div', 'os-row');
          row.append(el('span', 'tag ' + (x.status === 'open' ? 'warn' : ''), x.status), el('span', 'rt', x.note));
          wrap.append(row);
        });
      }
      wrap.append(el('div', 'muted rs-foot', 'Each stored claim passed a check that its quoted passage is an exact substring of the fetched page. "Contested" means another claim contradicts it. The original passage is stored unchanged.'));
      states.populated(claimsEl, wrap);
    }

    async function loadDetail() {
      if (!st.sel) {
        st.detail = null;
        paintClaims();
        return;
      }
      try {
        const d = await ctx.api.get('/api/os/research/question?id=' + encodeURIComponent(st.sel));
        if (guard()) return;
        st.detail = d;
        states.clearStale(claimsEl);
        paintClaims();
      } catch (err) {
        if (isAbort(err)) return;
        if (st.detail) states.stale(claimsEl, 'Could not refresh: ' + err.message);
        else states.error(claimsEl, err, { title: 'Could not read this question', retry: () => loadDetail() });
      }
    }

    /* ---------------- graph ---------------- */
    function paintPick() {
      const opts = [];
      if (st.sel) {
        const qq = (st.qs && st.qs.questions.find((x) => x.id === st.sel)) || null;
        opts.push({ v: 'q:' + st.sel, t: 'Question: ' + shorten(qq ? qq.text : st.sel, 50) });
      }
      ((st.qs && st.qs.hypotheses) || []).forEach((h) => opts.push({ v: 'h:' + h.id, t: 'Hypothesis: ' + shorten(h.statement, 50) }));
      if (opts.length < 2) {
        pickEl.replaceChildren();
        return;
      }
      const sel = el('select', 'rs-select');
      sel.setAttribute('aria-label', 'Graph of');
      sel.dataset.spec = 'Chooses what the evidence graph is drawn around: the chosen question, or one of the stored hypotheses.';
      opts.forEach((o) => {
        const op = el('option', '', o.t);
        op.value = o.v;
        op.selected = o.v === (st.graphRoot || opts[0].v);
        sel.append(op);
      });
      sel.addEventListener('change', () => {
        st.graphRoot = sel.value;
        loadGraph();
      });
      pickEl.replaceChildren(sel);
    }

    async function loadGraph() {
      paintPick();
      const rootKey = st.graphRoot || (st.sel ? 'q:' + st.sel : '');
      if (!rootKey) {
        st.graph = null;
        states.empty(graphEl, 'No question to draw.', { hint: 'The graph shows how claims answer a question, contradict each other and spawn follow-up tasks.' });
        return;
      }
      const qs = rootKey.startsWith('h:') ? 'hypothesis=' + encodeURIComponent(rootKey.slice(2)) : 'question=' + encodeURIComponent(rootKey.slice(2));
      try {
        const d = await ctx.api.get('/api/os/research/graph?' + qs);
        if (guard()) return;
        st.graph = d;
        states.clearStale(graphEl);
        if (d.nodes.length < 2) {
          states.empty(graphEl, 'This object has no stored relationships yet.', { hint: 'Nothing is linked to it, so there is nothing to draw.' });
          return;
        }
        const wrap = el('div');
        setHtml(wrap, graphSvg(d, 'Evidence graph with ' + d.nodes.length + ' nodes and ' + d.edges.length + ' relationships'));
        wrap.append(el('div', 'muted rs-foot', d.nodes.length + ' nodes, ' + d.edges.length + ' relationships' + (d.truncated ? ' (cut at ' + d.cap + ' nodes)' : '') + '. Hover a node for its full text.'));
        states.populated(graphEl, wrap);
      } catch (err) {
        if (isAbort(err)) return;
        if (st.graph) states.stale(graphEl, 'Could not refresh: ' + err.message);
        else states.error(graphEl, err, { title: 'Could not read the graph', retry: () => loadGraph() });
      }
    }

    /* ---------------- refresh and live ---------------- */
    async function refreshAll() {
      await Promise.all([loadLoop(), loadQs()]);
    }

    /* Reads the graph event log from the cursor onward; returns how many events were new.
       The log answers 500 at a time, so a long log is walked (bounded) and the
       cursor ends at its current end. */
    async function readLog() {
      let count = 0;
      for (let i = 0; i < 20; i += 1) {
        const r = await ctx.api.get('/api/events/log?kind=graph.&since=' + st.cursor);
        if (guard()) return count;
        const evs = Array.isArray(r.events) ? r.events : [];
        count += evs.length;
        if (typeof r.next === 'number') st.cursor = r.next;
        if (evs.length < 500) break;
      }
      return count;
    }

    async function pollLog() {
      try {
        const n = await readLog();
        if (n && !guard()) refreshAll();
      } catch (err) {
        if (!isAbort(err)) states.stale(qsEl, 'Live updates paused: ' + err.message);
      }
    }

    let view = null;
    view = mountThreadView($('rsThread'), ctx, {
      scroller: root.parentElement,
      placeholder: 'Ask the research agent a question, or say what to check',
      emptyMessage: 'No research conversation yet.',
      actions,
    });
    ctx.chat.subscribe((e) => {
      if (e.kind === 'busy') {
        const now = ctx.chat.busy();
        if (st.busyPrev && !now) refreshAll();
        st.busyPrev = now;
      }
    });
    ctx.every(10000, () => pollLog());
    ctx.events.onResync(() => refreshAll());

    states.loading(qsEl, 'Reading the research graph');
    states.loading(loopEl, 'Reading the pipeline');
    states.loading(graphEl, 'Reading relationships');
    states.loading(claimsEl, 'Reading claims');
    await view.start();
    await refreshAll();
    /* the cursor starts at the log's current end, so only later changes count as live */
    try {
      await readLog();
    } catch (err) {
      if (!isAbort(err)) states.stale(qsEl, 'Live updates unavailable: ' + err.message);
    }
  },

  async refresh(ctx, reason) {
    if (reason === 'manual') ctx.notify({ level: 'info', title: 'RESEARCH', detail: 'This screen re-reads when the research graph changes.', ttl: 2000 });
  },
};
