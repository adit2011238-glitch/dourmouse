/* Automations (plan 2.2, finding 207): things Dourmouse does by itself, on a clock or when something happens.

   One column of grouped boxes like System Settings. The list shows each automation's name, its trigger in words, the
   next run and the last result word, with a switch. Nothing is created from a click alone: a sentence (or a template)
   becomes a parsed card that says when, what, what it may use, how it tells you and its limits, and only Approve
   creates it. A step that needs confirmation never runs unattended; it waits here under Review. Reads
   GET /api/os/automations and acts through /api/os/automations/*. */

import { html, setHtml } from '../../kit/html.js';
import { states } from '../../kit/states.js';
import { confirmHere } from '../../kit/confirm-card.js';
import { isAbort } from '../../core/api.js';
import { countsText, rowSub, resultTone, cardRows, historyLine, historyNote, parkedRuns, EXAMPLE } from './helpers.js';

const POLL_MS = 10000;

export default {
  id: 'TIMETABLE',
  sub: 'automations',
  css: true,
  thread: false,

  async mount(root, ctx) {
    const st = { rows: null, templates: [], error: null, sig: '', open: null, detail: null, detailError: '', sheet: null, sheetError: '', busy: false };
    const linked = (ctx.params && ctx.params.query) || {};
    if (linked.id) st.open = String(linked.id);

    root.dataset.state = 'loading';
    setHtml(root, html`
      <div class="am">
        <div class="am-note" id="amNote" role="status" hidden></div>
        <div id="amConfirm"></div>
        <div id="amSheet"></div>
        <div class="am-group"><h2 class="am-h" id="amListH">Automations</h2><div class="am-box" id="amList" data-region></div></div>
        <div id="amTemplates"></div>
        <div id="amDetail"></div>
      </div>`);
    const $ = (id) => root.querySelector('#' + id);
    const noteEl = $('amNote');
    const confirmEl = $('amConfirm');
    const sheetEl = $('amSheet');
    const listEl = $('amList');
    const tplEl = $('amTemplates');
    const detailEl = $('amDetail');

    const note = (text, tone) => {
      noteEl.hidden = !text;
      noteEl.textContent = text || '';
      noteEl.dataset.tone = tone || '';
    };

    ctx.chrome.setActions([{
      id: 'new', label: 'New automation', kind: 'primary',
      spec: 'Write when and what in a sentence. You see what it will do, and nothing is created until you approve it.',
      onClick: () => openSheet({ sentence: '' }),
    }]);

    /* ---------------- reads ---------------- */
    async function load(reason) {
      let out;
      try {
        out = await ctx.api.get('/api/os/automations');
      } catch (err) {
        if (isAbort(err) || ctx.signal.aborted) return;
        st.error = err;
        paint();
        return;
      }
      if (ctx.signal.aborted) return;
      st.error = null;
      const sig = JSON.stringify(out) + String(st.open);
      if (sig === st.sig && reason === 'poll') return;
      st.sig = sig;
      st.rows = out.automations || [];
      st.templates = out.templates || [];
      paint();
      if (st.open) await loadDetail(reason === 'poll');
    }

    async function loadDetail(quiet) {
      if (!st.open) return;
      try {
        const out = await ctx.api.get('/api/os/automations/detail?id=' + encodeURIComponent(st.open));
        if (ctx.signal.aborted) return;
        st.detail = out;
        st.detailError = '';
      } catch (err) {
        if (isAbort(err) || ctx.signal.aborted) return;
        st.detail = null;
        st.detailError = err.message;
        if (!quiet && /no such/i.test(err.message)) st.open = null;
      }
      paintDetail();
    }

    /* ---------------- painting ---------------- */
    function paint() {
      if (st.error && !st.rows) {
        root.dataset.state = 'error';
        states.error(listEl, st.error, { title: 'Could not read the automations', retry: () => { states.loading(listEl, 'Reading the automations'); load('manual'); } });
        return;
      }
      if (!st.rows) return;
      root.dataset.state = st.rows.length ? 'populated' : 'empty';
      ctx.chrome.setSub(countsText(st.rows));
      ctx.chrome.setLive(false);
      root.querySelector('#amListH').hidden = !st.rows.length;
      listEl.parentElement.hidden = !st.rows.length;
      if (st.rows.length) {
        listEl.dataset.state = 'populated';
        setHtml(listEl, html`${st.rows.map((r) => row(r))}`);
      }
      paintTemplates();
      paintSheet();
    }

    function row(r) {
      const tone = resultTone(r.result_status);
      return html`<div class="am-row" data-id="${r.id}" data-open="${String(st.open === r.id)}">
        <button type="button" class="am-main" data-open-id="${r.id}" aria-expanded="${String(st.open === r.id)}">
          <span class="am-rt"><span class="am-name">${r.name}</span><span class="am-sub">${rowSub(r)}</span></span>
          <span class="am-word" data-tone="${tone}">${r.result}</span>
        </button>
        <button type="button" class="os-switch" role="switch" aria-checked="${String(Boolean(r.enabled))}" aria-label="${r.name}" data-switch="${r.id}" data-spec="${r.enabled ? 'Pauses this automation. It keeps its history.' : 'Turns this automation back on.'}"></button>
      </div>`;
    }

    function paintTemplates() {
      /* the five starting points show when the list is empty, and stay one click away behind New automation after that */
      if (!st.rows || st.rows.length || !st.templates.length) {
        tplEl.replaceChildren();
        return;
      }
      setHtml(tplEl, html`<div class="am-group"><h2 class="am-h">Start from one of these</h2><div class="am-box">${st.templates.map((t) => html`
        <div class="am-row"><span class="am-rt"><span class="am-name">${t.name}</span><span class="am-sub">${t.when}${t.when ? ' · ' : ''}${t.action}</span></span>
        <button type="button" class="os-btn" data-template="${t.id}" data-spec="Shows what this will do. Nothing is created until you approve it.">Add</button></div>`)}</div></div>`);
    }

    /* ---------------- the create sheet ---------------- */
    function openSheet(seed) {
      st.sheet = { sentence: seed.sentence || '', template: seed.template || '', card: null };
      st.sheetError = '';
      paintSheet();
      if (seed.template) previewSheet();
    }

    function closeSheet() {
      st.sheet = null;
      st.sheetError = '';
      sheetEl.replaceChildren();
    }

    function paintSheet() {
      if (!st.sheet) {
        sheetEl.replaceChildren();
        return;
      }
      const s = st.sheet;
      const keep = sheetEl.querySelector('[data-sentence]');
      if (keep) s.sentence = keep.value;
      setHtml(sheetEl, html`<div class="am-group"><h2 class="am-h">${s.template ? 'Add automation' : 'New automation'}</h2><div class="am-box am-pad">
        ${s.template ? '' : html`<label class="am-l" for="amSentence">When, and what to do</label>
        <input id="amSentence" class="am-in" type="text" maxlength="400" value="${s.sentence}" placeholder="${EXAMPLE}" data-sentence data-spec="For example: every weekday at 8 check my security, or remind me tomorrow at 5pm to call the bank.">`}
        ${s.card ? html`<div class="am-card">${cardRows(s.card).map(([k, v]) => html`<div class="am-kv"><span class="am-k">${k}</span><span class="am-v">${v}</span></div>`)}</div>` : ''}
        ${st.sheetError ? html`<div class="am-err" role="alert">${st.sheetError}</div>` : ''}
        <div class="am-btns">
          ${s.card ? html`<button type="button" class="os-btn os-btn--primary" data-approve ${st.busy ? 'disabled' : ''} data-spec="Creates this automation. It then runs by itself, within its limits.">Approve</button>` : html`<button type="button" class="os-btn os-btn--primary" data-preview ${st.busy ? 'disabled' : ''} data-spec="Shows what this would do. Nothing is created yet.">Preview</button>`}
          <button type="button" class="os-btn" data-cancel-sheet data-spec="Closes this without creating anything.">Cancel</button>
        </div></div></div>`);
      const input = sheetEl.querySelector('[data-sentence]');
      if (input && !s.card) input.focus();
    }

    function sheetBody() {
      const s = st.sheet;
      return s.template ? { template: s.template } : { sentence: s.sentence.trim() };
    }

    async function previewSheet() {
      const s = st.sheet;
      if (!s) return;
      const input = sheetEl.querySelector('[data-sentence]');
      if (input) s.sentence = input.value;
      if (!s.template && !s.sentence.trim()) {
        st.sheetError = 'Say when and what, for example: ' + EXAMPLE.toLowerCase() + '.';
        paintSheet();
        return;
      }
      st.busy = true;
      st.sheetError = '';
      paintSheet();
      try {
        const out = await ctx.api.post('/api/os/automations/preview', sheetBody());
        s.card = out.card;
      } catch (err) {
        if (isAbort(err)) return;
        st.sheetError = err.message;
        s.card = null;
      }
      st.busy = false;
      paintSheet();
    }

    async function approveSheet() {
      const s = st.sheet;
      if (!s || !s.card) return;
      st.busy = true;
      st.sheetError = '';
      paintSheet();
      try {
        const out = await ctx.api.post('/api/os/automations/create', { ...sheetBody(), approve: true });
        st.busy = false;
        closeSheet();
        st.open = out.automation.id;
        note('Created ' + out.automation.name + '.', 'ok');
        await load('manual');
        return;
      } catch (err) {
        if (isAbort(err)) return;
        st.sheetError = err.message;
      }
      st.busy = false;
      paintSheet();
    }

    /* ---------------- the detail ---------------- */
    function paintDetail() {
      if (!st.open) {
        detailEl.replaceChildren();
        return;
      }
      if (st.detailError) {
        setHtml(detailEl, html`<div class="am-group"><div class="am-box am-pad am-err" role="alert">${st.detailError}</div></div>`);
        return;
      }
      const d = st.detail;
      if (!d || d.automation.id !== st.open) return;
      const a = d.automation;
      const parked = parkedRuns(d.history);
      setHtml(detailEl, html`<div class="am-group"><h2 class="am-h">${a.name}</h2>
        <div class="am-box">
          <div class="am-row"><span class="am-rt"><span class="am-name">${a.trigger}</span><span class="am-sub">${a.action}</span></span></div>
          <div class="am-row"><span class="am-rt"><span class="am-name">Can use</span><span class="am-sub">${d.tools}</span></span></div>
          <div class="am-row"><span class="am-rt"><span class="am-name">Today</span><span class="am-sub">${a.runs_today} of ${a.caps.max_runs_per_day} runs, ${a.caps.max_steps} steps each</span></span></div>
          ${parked.map((p) => html`<div class="am-row am-parked" data-run="${p.id}"><span class="am-rt"><span class="am-name">Waiting for you</span><span class="am-sub">${p.result}</span></span>
            <button type="button" class="os-btn os-btn--primary" data-review="${p.id}" data-spec="Shows what it wants to do. Nothing runs until you approve.">Review</button></div>`)}
          <div class="am-row am-actions"><button type="button" class="os-btn" data-run-now="${a.id}" data-spec="Runs it once now, after you confirm. It counts toward today's runs.">Run now</button>
            <button type="button" class="os-btn" data-remove="${a.id}" data-spec="Deletes this automation and its history, after you confirm.">Remove</button></div>
        </div></div>
        <div class="am-group"><h2 class="am-h">History</h2><div class="am-box">${d.history.length ? d.history.map((h) => html`
          <div class="am-row"><span class="am-rt"><span class="am-name">${historyLine(h)}</span><span class="am-sub">${h.result}${historyNote(h) ? ' · ' + historyNote(h) : ''}</span></span>
          <span class="am-word" data-tone="${resultTone(h.status)}">${h.word}</span></div>`) : html`<div class="am-row am-sub">No runs yet.</div>`}</div></div>`);
    }

    /* ---------------- actions ---------------- */
    async function act(path, body, okText) {
      try {
        await ctx.api.post(path, body);
        note(okText, 'ok');
      } catch (err) {
        if (isAbort(err)) return;
        note(err.message, 'error');
      }
      if (!ctx.signal.aborted) await load('manual');
    }

    root.addEventListener('click', (e) => {
      const q = (sel) => e.target.closest(sel);
      let el;
      if ((el = q('[data-open-id]'))) {
        st.open = st.open === el.dataset.openId ? null : el.dataset.openId;
        st.detail = null;
        st.detailError = '';
        st.sig = '';
        paint();
        paintDetail();
        loadDetail(false);
      } else if ((el = q('[data-switch]'))) {
        const r = (st.rows || []).find((x) => x.id === el.dataset.switch);
        if (!r) return;
        act(r.enabled ? '/api/os/automations/pause' : '/api/os/automations/resume', { id: r.id }, r.enabled ? r.name + ' paused.' : r.name + ' is on.');
      } else if ((el = q('[data-template]'))) {
        openSheet({ template: el.dataset.template });
      } else if (q('[data-preview]')) {
        previewSheet();
      } else if (q('[data-approve]')) {
        approveSheet();
      } else if (q('[data-cancel-sheet]')) {
        closeSheet();
      } else if ((el = q('[data-run-now]'))) {
        const a = st.detail && st.detail.automation;
        if (!a) return;
        confirmHere(confirmEl, 'Run ' + a.name + ' now? It counts toward today\'s runs.',
          () => act('/api/os/automations/run', { id: a.id }, a.name + ' ran.'));
      } else if ((el = q('[data-remove]'))) {
        const a = st.detail && st.detail.automation;
        if (!a) return;
        confirmHere(confirmEl, 'Remove ' + a.name + '? Its history goes too. This cannot be undone.', async () => {
          await ctx.api.post('/api/os/automations/remove', { id: a.id });
          st.open = null;
          st.detail = null;
          note(a.name + ' removed.', 'ok');
          await load('manual');
          paintDetail();
        });
      } else if ((el = q('[data-review]'))) {
        const run = (st.detail && st.detail.history || []).find((h) => h.id === el.dataset.review);
        if (!run) return;
        const what = run.parked ? run.parked.tool.replace(/_/g, ' ') : 'this step';
        confirmHere(confirmEl, 'Let ' + (st.detail.automation.name) + ' run ' + what + ' once? It asked first because it needs your approval.',
          () => act('/api/os/automations/approve', { run: run.id }, 'Done.'));
      }
    });

    root.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && st.sheet) closeSheet();
      else if (e.key === 'Enter' && e.target.matches && e.target.matches('[data-sentence]')) {
        e.preventDefault();
        if (st.sheet && st.sheet.card) approveSheet();
        else previewSheet();
      }
    });

    /* a changed sentence invalidates the card the owner was about to approve */
    root.addEventListener('input', (e) => {
      if (e.target.matches && e.target.matches('[data-sentence]') && st.sheet && st.sheet.card) {
        st.sheet.card = null;
        st.sheet.sentence = e.target.value;
        paintSheet();
        const input = sheetEl.querySelector('[data-sentence]');
        if (input) {
          input.focus();
          input.setSelectionRange(input.value.length, input.value.length);
        }
      }
    });

    ctx.events.onResync(() => load('resync'));
    ctx.every(POLL_MS, () => load('poll'));
    states.loading(listEl, 'Reading the automations');
    await load('show');
  },

  async refresh(ctx, reason) {
    if (reason === 'manual') ctx.notify({ level: 'info', title: 'Automations', detail: 'This screen re-reads every few seconds while it is open.', ttl: 3000 });
  },
};
