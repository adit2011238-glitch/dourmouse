/* Memory (plan 2.4, PL-06, finding 210): what Dourmouse knows about you, where it learned it, and how to fix it.

   One column of grouped boxes like System Settings. The list shows each fact with its kind, where it came from, its age
   and how often it was used. A click opens it: edit the words, pin it (always remembered), hide it (kept, never used),
   replace it with a newer one (the old one is closed, not deleted) or forget it (asks first, cannot be undone). Facts
   Dourmouse learned by itself wait under Review until you keep or forget them. Reads GET /api/os/memory and acts
   through /api/os/memory/*. */

import { html, setHtml } from '../../kit/html.js';
import { states } from '../../kit/states.js';
import { confirmHere } from '../../kit/confirm-card.js';
import { isAbort } from '../../core/api.js';
import { FILTERS, KIND_WORD, ORIGIN_WORD, query, kindWord, originWord, rowSub, countsText, cloudLine, historyLine, fileName } from './helpers.js';

const POLL_MS = 15000;

export default {
  id: 'MEMORY',
  sub: 'what it knows',
  css: true,
  thread: false,

  async mount(root, ctx) {
    const st = { data: null, error: null, sig: '', q: '', kind: '', origin: '', filter: 'all', open: null, detail: null, detailError: '', sheet: null, sheetError: '', pairs: [], busy: false };
    const linked = (ctx.params && ctx.params.query) || {};
    if (linked.id) st.open = Number(linked.id);

    root.dataset.state = 'loading';
    setHtml(root, html`
      <div class="mm">
        <div class="mm-note" id="mmNote" role="status" hidden></div>
        <div id="mmConfirm"></div>
        <div class="mm-reviewbar" id="mmReview" hidden></div>
        <div class="mm-bar">
          <input id="mmSearch" class="mm-in mm-search" type="search" placeholder="Search what it knows" aria-label="Search what it knows" data-spec="Searches the words of every fact.">
          <select id="mmKind" class="mm-sel" aria-label="Kind" data-spec="Shows one kind of fact.">
            <option value="">Any kind</option>${Object.keys(KIND_WORD).filter((k) => k !== 'record').map((k) => html`<option value="${k}">${KIND_WORD[k]}</option>`)}
          </select>
          <select id="mmOrigin" class="mm-sel" aria-label="Where it came from" data-spec="Shows facts from one place.">
            <option value="">Any source</option>${Object.keys(ORIGIN_WORD).map((k) => html`<option value="${k}">${ORIGIN_WORD[k]}</option>`)}
          </select>
        </div>
        <div class="mm-chips" id="mmChips" role="group" aria-label="Filter"></div>
        <div class="mm-group"><div class="mm-box" id="mmList" data-region></div></div>
        <div id="mmSheet" class="mm-slot"></div>
        <div id="mmDetail" class="mm-slot"></div>
        <div id="mmMerge" class="mm-slot"></div>
        <div class="mm-line" id="mmCloud"></div>
      </div>`);
    const $ = (id) => root.querySelector('#' + id);
    const noteEl = $('mmNote');
    const confirmEl = $('mmConfirm');
    const reviewEl = $('mmReview');
    const listEl = $('mmList');
    const sheetEl = $('mmSheet');
    const detailEl = $('mmDetail');
    const mergeEl = $('mmMerge');
    const chipsEl = $('mmChips');

    const note = (text, tone) => {
      noteEl.hidden = !text;
      noteEl.textContent = text || '';
      noteEl.dataset.tone = tone || '';
    };

    ctx.chrome.setActions([
      { id: 'add', label: 'Add a fact', kind: 'primary', spec: 'Type something you want it to remember. It is saved as something you said.', onClick: () => openSheet('add') },
      { id: 'export', label: 'Export', spec: 'Saves everything it knows about you as a Markdown file and a JSON file, through the save dialog.', onClick: () => exportAll() },
      { id: 'forget-where', label: 'Forget by source or date', spec: 'Forgets every fact from one source or date range. It shows how many first.', onClick: () => openSheet('forget') },
    ]);

    /* ---------------- reads ---------------- */
    async function load(reason) {
      let out;
      try {
        out = await ctx.api.get('/api/os/memory' + query(st));
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
      st.data = out;
      paint();
      if (st.open) await loadDetail(reason === 'poll');
      loadPairs();
    }

    async function loadPairs() {
      try {
        const out = await ctx.api.get('/api/os/memory/merge');
        if (ctx.signal.aborted) return;
        const key = JSON.stringify(out.pairs);
        if (key !== st.pairKey) {
          st.pairKey = key;
          st.pairs = out.pairs || [];
          paintMerge();
        }
      } catch (err) {
        if (!isAbort(err)) st.pairs = [];
      }
    }

    async function loadDetail(quiet) {
      if (!st.open) return;
      try {
        const out = await ctx.api.get('/api/os/memory/fact?id=' + encodeURIComponent(st.open));
        if (ctx.signal.aborted) return;
        st.detail = out.fact;
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
    function paintChips() {
      const counts = (st.data && st.data.counts) || {};
      setHtml(chipsEl, html`${FILTERS.map((f) => html`<button type="button" class="mm-chip" data-filter="${f.id}" aria-pressed="${String(st.filter === f.id)}">${f.label}${f.id === 'review' && counts.review ? ' (' + counts.review + ')' : ''}</button>`)}`);
    }

    function paint() {
      if (st.error && !st.data) {
        root.dataset.state = 'error';
        states.error(listEl, st.error, { title: 'Could not read what it knows', retry: () => { states.loading(listEl, 'Reading memory'); load('manual'); } });
        return;
      }
      if (!st.data) return;
      const facts = st.data.facts || [];
      root.dataset.state = facts.length ? 'populated' : 'empty';
      ctx.chrome.setSub(countsText(st.data.counts));
      ctx.chrome.setLive(false);
      paintChips();
      const review = (st.data.counts && st.data.counts.review) || 0;
      reviewEl.hidden = !review || st.filter === 'review';
      if (!reviewEl.hidden) {
        setHtml(reviewEl, html`<span>${review === 1 ? '1 new thing' : review + ' new things'} it learned. Keep or forget each one.</span>
          <button type="button" class="os-btn" data-filter="review" data-spec="Shows the facts it learned by itself, so you can keep or forget them.">Review</button>`);
      }
      $('mmCloud').textContent = cloudLine(st.data.cloud_recall);
      if (!facts.length) {
        listEl.dataset.state = 'empty';
        setHtml(listEl, html`<div class="mm-row"><span class="mm-rt"><span class="mm-name">${emptyWord()}</span><span class="mm-sub">${emptyHelp()}</span></span></div>`);
        return;
      }
      listEl.dataset.state = 'populated';
      setHtml(listEl, html`${facts.map((f) => row(f))}${st.data.total > facts.length ? html`<div class="mm-row mm-sub">Showing ${facts.length} of ${st.data.total}. Search to narrow it.</div>` : ''}`);
    }

    function emptyWord() {
      if (st.q || st.kind || st.origin) return 'Nothing matches';
      if (st.filter === 'review') return 'Nothing to review';
      if (st.filter === 'pinned') return 'Nothing pinned';
      if (st.filter === 'recent') return 'Nothing used this week';
      if (st.filter === 'closed') return 'Nothing replaced';
      return 'It does not know anything yet';
    }

    function emptyHelp() {
      if (st.filter === 'all' && !st.q && !st.kind && !st.origin) return 'Add a fact, or tell it to remember something in a chat.';
      return 'Try another filter.';
    }

    function row(f) {
      return html`<div class="mm-row" data-id="${f.id}" data-open="${String(st.open === f.id)}">
        <button type="button" class="mm-main" data-open-id="${f.id}" aria-expanded="${String(st.open === f.id)}">
          <span class="mm-rt"><span class="mm-name" data-dim="${String(f.hidden || f.closed)}">${f.body}</span><span class="mm-sub">${rowSub(f)}</span></span>
        </button>
        ${st.filter === 'review' ? html`<button type="button" class="os-btn" data-keep="${f.id}" data-spec="Keeps this fact. It stays in memory and can be used in replies.">Keep</button>
          <button type="button" class="os-btn" data-forget-row="${f.id}" data-spec="Forgets this fact, after you confirm.">Forget</button>` : ''}
      </div>`;
    }

    /* ---------------- the detail ---------------- */
    function paintDetail() {
      if (!st.open) {
        detailEl.replaceChildren();
        return;
      }
      if (st.detailError) {
        setHtml(detailEl, html`<div class="mm-group"><div class="mm-box mm-pad mm-err" role="alert">${st.detailError}</div></div>`);
        return;
      }
      const f = st.detail;
      if (!f || f.id !== st.open) return;
      const keepEdit = detailEl.querySelector('[data-edit-text]');
      const draft = keepEdit && keepEdit.dataset.for === String(f.id) ? keepEdit.value : f.body;
      setHtml(detailEl, html`<div class="mm-group"><h2 class="mm-h">${kindWord(f.kind)}</h2>
        <div class="mm-box mm-pad">
          <label class="mm-l" for="mmEdit">What it knows</label>
          <textarea id="mmEdit" class="mm-in" data-edit-text data-for="${f.id}" maxlength="4000" ${f.closed ? 'readonly' : ''}>${draft}</textarea>
          <div class="mm-line">${rowSub(f)}${f.valid_to ? ' · Replaced ' + f.valid_to.slice(0, 10) : ''}</div>
          ${st.sheetError ? html`<div class="mm-err" role="alert">${st.sheetError}</div>` : ''}
          <div class="mm-btns">
            ${f.closed ? '' : html`<button type="button" class="os-btn os-btn--primary" data-save="${f.id}" data-spec="Saves your edit. The earlier words stay in the history.">Save</button>
            <button type="button" class="os-btn" data-pin="${f.id}" data-spec="${f.pinned ? 'Stops always remembering this.' : 'Always remember this in replies. Up to three facts can be pinned at once.'}">${f.pinned ? 'Unpin' : 'Pin'}</button>
            <button type="button" class="os-btn" data-hide="${f.id}" data-spec="${f.hidden ? 'Lets replies use this fact again.' : 'Keeps this fact but never uses it in replies.'}">${f.hidden ? 'Show to replies' : 'Hide from replies'}</button>
            <button type="button" class="os-btn" data-replace="${f.id}" data-spec="Writes a newer fact in its place. This one is closed, not deleted.">Replace</button>`}
            <button type="button" class="os-btn" data-forget="${f.id}" data-spec="Deletes this fact, its search entries and its history, after you confirm. This cannot be undone.">Forget</button>
          </div>
        </div></div>
        <div class="mm-group"><h2 class="mm-h">Used in</h2><div class="mm-box">${(f.used_in || []).length ? f.used_in.slice(0, 8).map((u) => html`
          <div class="mm-row"><span class="mm-rt"><span class="mm-name">A reply in ${u.session.replace(/^session_/, '').slice(0, 24)}</span><span class="mm-sub">Turn ${u.turn} · ${u.at.replace('T', ' ').slice(0, 16)}</span></span></div>`) : html`<div class="mm-row mm-sub">No reply has used this yet.</div>`}</div></div>
        <div class="mm-group"><h2 class="mm-h">History</h2><div class="mm-box">${(f.history || []).length ? f.history.map((h) => html`
          <div class="mm-row"><span class="mm-rt"><span class="mm-name">${historyLine(h)}</span>${h.before ? html`<span class="mm-sub">Was: ${h.before.slice(0, 160)}</span>` : ''}</span></div>`) : html`<div class="mm-row mm-sub">No changes yet.</div>`}</div></div>`);
    }

    function paintMerge() {
      if (!st.pairs.length) {
        mergeEl.replaceChildren();
        return;
      }
      setHtml(mergeEl, html`<div class="mm-group"><h2 class="mm-h">These look the same</h2><div class="mm-box">${st.pairs.slice(0, 5).map((p) => html`
        <div class="mm-row"><span class="mm-rt"><span class="mm-name">${p.keep_text}</span><span class="mm-sub">${p.drop_text}</span></span>
        <button type="button" class="os-btn" data-merge="${p.keep}:${p.drop}" data-spec="Keeps the first and closes the second. Nothing is deleted.">Merge</button></div>`)}</div></div>`);
    }

    /* ---------------- sheets: add, replace, forget by source or date ---------------- */
    function openSheet(mode, seed) {
      st.sheet = { mode, ...(seed || {}), count: null };
      st.sheetError = '';
      paintSheet();
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
      if (s.mode === 'forget') {
        setHtml(sheetEl, html`<div class="mm-group"><h2 class="mm-h">Forget by source or date</h2><div class="mm-box mm-pad">
          <label class="mm-l" for="mmSource">Source</label>
          <input id="mmSource" class="mm-in" type="text" maxlength="200" value="${s.source || ''}" data-f-source placeholder="For example agent, or mail:alex">
          <div class="mm-two"><div><label class="mm-l" for="mmSince">From</label><input id="mmSince" class="mm-in" type="date" value="${s.since || ''}" data-f-since></div>
          <div><label class="mm-l" for="mmUntil">To</label><input id="mmUntil" class="mm-in" type="date" value="${s.until || ''}" data-f-until></div></div>
          ${s.count !== null ? html`<div class="mm-line">${s.count === 1 ? '1 fact would be forgotten.' : s.count + ' facts would be forgotten.'} This cannot be undone.</div>` : ''}
          ${st.sheetError ? html`<div class="mm-err" role="alert">${st.sheetError}</div>` : ''}
          <div class="mm-btns">
            ${s.count ? html`<button type="button" class="os-btn os-btn--primary" data-forget-go ${st.busy ? 'disabled' : ''} data-spec="Forgets these facts for good.">Forget ${s.count}</button>` : html`<button type="button" class="os-btn os-btn--primary" data-forget-count ${st.busy ? 'disabled' : ''} data-spec="Counts what would be forgotten. Nothing is deleted yet.">Count</button>`}
            <button type="button" class="os-btn" data-cancel-sheet data-spec="Closes this without forgetting anything.">Cancel</button>
          </div></div></div>`);
        return;
      }
      const title = s.mode === 'replace' ? 'Replace with a newer fact' : 'Add a fact';
      setHtml(sheetEl, html`<div class="mm-group"><h2 class="mm-h">${title}</h2><div class="mm-box mm-pad">
        <label class="mm-l" for="mmNew">${s.mode === 'replace' ? 'What is true now' : 'What should it remember'}</label>
        <textarea id="mmNew" class="mm-in" maxlength="4000" data-new-text placeholder="${s.mode === 'replace' ? 'I live in Dubai' : 'I prefer short answers'}">${s.text || ''}</textarea>
        ${s.mode === 'add' ? html`<label class="mm-l" for="mmNewKind">Kind</label><select id="mmNewKind" class="mm-sel" data-new-kind>${Object.keys(KIND_WORD).filter((k) => k !== 'record').map((k) => html`<option value="${k}" ${s.kind === k ? 'selected' : ''}>${KIND_WORD[k]}</option>`)}</select>` : ''}
        ${st.sheetError ? html`<div class="mm-err" role="alert">${st.sheetError}</div>` : ''}
        <div class="mm-btns">
          <button type="button" class="os-btn os-btn--primary" data-sheet-save ${st.busy ? 'disabled' : ''} data-spec="${s.mode === 'replace' ? 'Saves the newer fact and closes the old one.' : 'Saves it as something you said.'}">${s.mode === 'replace' ? 'Replace' : 'Add'}</button>
          <button type="button" class="os-btn" data-cancel-sheet data-spec="Closes this without saving.">Cancel</button>
        </div></div></div>`);
      const input = sheetEl.querySelector('[data-new-text]');
      if (input && !s.focused) {
        s.focused = true;
        input.focus();
      }
    }

    function readSheet() {
      const s = st.sheet;
      if (!s) return;
      const q = (sel) => sheetEl.querySelector(sel);
      if (s.mode === 'forget') {
        s.source = (q('[data-f-source]') || {}).value || '';
        s.since = (q('[data-f-since]') || {}).value || '';
        s.until = (q('[data-f-until]') || {}).value || '';
      } else {
        s.text = (q('[data-new-text]') || {}).value || '';
        s.kind = (q('[data-new-kind]') || {}).value || 'fact';
      }
    }

    async function saveSheet() {
      readSheet();
      const s = st.sheet;
      if (!s || !s.text.trim()) {
        st.sheetError = 'Write the fact first.';
        paintSheet();
        return;
      }
      st.busy = true;
      st.sheetError = '';
      paintSheet();
      try {
        if (s.mode === 'replace') {
          const out = await ctx.api.post('/api/os/memory/supersede', { id: s.id, text: s.text.trim() });
          st.open = out.fact.id;
          note('Replaced. The old fact is closed and stays in its history.', 'ok');
        } else {
          const out = await ctx.api.post('/api/os/memory/add', { text: s.text.trim(), kind: s.kind });
          st.open = out.fact.id;
          note('Added.', 'ok');
        }
        st.busy = false;
        closeSheet();
        st.sig = '';
        await load('manual');
        return;
      } catch (err) {
        if (isAbort(err)) return;
        st.sheetError = err.message;
      }
      st.busy = false;
      paintSheet();
    }

    async function forgetCount() {
      readSheet();
      const s = st.sheet;
      if (!s.source.trim() && !s.since && !s.until) {
        st.sheetError = 'Pick a source or a date range.';
        paintSheet();
        return;
      }
      st.busy = true;
      st.sheetError = '';
      try {
        const out = await ctx.api.post('/api/os/memory/forget_where', { source: s.source.trim(), since: s.since, until: s.until });
        s.count = out.would_forget;
        if (!out.would_forget) st.sheetError = 'Nothing matches that.';
      } catch (err) {
        if (isAbort(err)) return;
        st.sheetError = err.message;
      }
      st.busy = false;
      paintSheet();
    }

    async function forgetGo() {
      const s = st.sheet;
      readSheet();
      st.busy = true;
      try {
        const out = await ctx.api.post('/api/os/memory/forget_where', { source: s.source.trim(), since: s.since, until: s.until, confirm: true });
        st.busy = false;
        closeSheet();
        note(out.forgotten === 1 ? 'Forgot 1 fact.' : 'Forgot ' + out.forgotten + ' facts.', 'ok');
        st.open = null;
        st.detail = null;
        paintDetail();
        st.sig = '';
        await load('manual');
        return;
      } catch (err) {
        if (isAbort(err)) return;
        st.sheetError = err.message;
      }
      st.busy = false;
      paintSheet();
    }

    /* ---------------- export: through the save dialog ---------------- */
    async function exportAll() {
      try {
        const out = await ctx.api.get('/api/os/memory/export');
        save(fileName('md'), out.markdown, 'text/markdown');
        save(fileName('json'), JSON.stringify(out.json, null, 2), 'application/json');
        note('Exported ' + out.count + (out.count === 1 ? ' fact.' : ' facts.'), 'ok');
      } catch (err) {
        if (!isAbort(err)) note(err.message, 'error');
      }
    }

    function save(name, text, type) {
      const url = URL.createObjectURL(new Blob([text], { type }));
      const a = document.createElement('a');
      a.href = url;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 4000);
    }

    /* ---------------- actions ---------------- */
    async function act(path, body, okText) {
      try {
        await ctx.api.post(path, body);
        if (okText) note(okText, 'ok');
      } catch (err) {
        if (isAbort(err)) return;
        note(err.message, 'error');
      }
      st.sig = '';
      if (!ctx.signal.aborted) await load('manual');
    }

    function forgetConfirm(id) {
      confirmHere(confirmEl, 'Forget this fact? Its words, history and search entries are deleted. This cannot be undone.', async () => {
        await ctx.api.post('/api/os/memory/forget', { id, confirm: true });
        if (st.open === id) {
          st.open = null;
          st.detail = null;
          paintDetail();
        }
        note('Forgotten.', 'ok');
        st.sig = '';
        await load('manual');
      });
    }

    root.addEventListener('click', (e) => {
      const q = (sel) => e.target.closest(sel);
      let el;
      if ((el = q('[data-filter]'))) {
        st.filter = el.dataset.filter;
        st.sig = '';
        load('manual');
      } else if ((el = q('[data-open-id]'))) {
        const id = Number(el.dataset.openId);
        st.open = st.open === id ? null : id;
        st.detail = null;
        st.detailError = '';
        st.sig = '';
        paint();
        paintDetail();
        loadDetail(false);
      } else if ((el = q('[data-keep]'))) {
        act('/api/os/memory/keep', { id: Number(el.dataset.keep) }, 'Kept.');
      } else if ((el = q('[data-forget-row]'))) {
        forgetConfirm(Number(el.dataset.forgetRow));
      } else if ((el = q('[data-forget]'))) {
        forgetConfirm(Number(el.dataset.forget));
      } else if ((el = q('[data-save]'))) {
        const area = detailEl.querySelector('[data-edit-text]');
        act('/api/os/memory/edit', { id: Number(el.dataset.save), text: area ? area.value : '' }, 'Saved. The earlier words are in the history.');
      } else if ((el = q('[data-pin]'))) {
        const id = Number(el.dataset.pin);
        act('/api/os/memory/pin', { id, value: !(st.detail && st.detail.pinned) }, st.detail && st.detail.pinned ? 'Unpinned.' : 'Pinned.');
      } else if ((el = q('[data-hide]'))) {
        const id = Number(el.dataset.hide);
        act('/api/os/memory/hide', { id, value: !(st.detail && st.detail.hidden) }, st.detail && st.detail.hidden ? 'Replies can use it again.' : 'Hidden from replies.');
      } else if ((el = q('[data-replace]'))) {
        openSheet('replace', { id: Number(el.dataset.replace) });
      } else if ((el = q('[data-merge]'))) {
        const [keep, drop] = el.dataset.merge.split(':').map(Number);
        act('/api/os/memory/merge', { keep, drop }, 'Merged. The second one is closed, not deleted.');
      } else if (q('[data-sheet-save]')) {
        saveSheet();
      } else if (q('[data-forget-count]')) {
        forgetCount();
      } else if (q('[data-forget-go]')) {
        forgetGo();
      } else if (q('[data-cancel-sheet]')) {
        closeSheet();
      }
    });

    let timer = null;
    root.addEventListener('input', (e) => {
      if (e.target.id === 'mmSearch') {
        clearTimeout(timer);
        timer = setTimeout(() => { st.q = e.target.value; st.sig = ''; load('manual'); }, 250);
      } else if (e.target.matches && e.target.matches('[data-f-source], [data-f-since], [data-f-until]') && st.sheet) {
        st.sheet.count = null;
        readSheet();
        paintSheet();
        const again = sheetEl.querySelector('[' + (e.target.hasAttribute('data-f-source') ? 'data-f-source' : e.target.hasAttribute('data-f-since') ? 'data-f-since' : 'data-f-until') + ']');
        if (again) {
          again.focus();
          if (again.setSelectionRange && again.type === 'text') again.setSelectionRange(again.value.length, again.value.length);
        }
      }
    });
    root.addEventListener('change', (e) => {
      if (e.target.id === 'mmKind') { st.kind = e.target.value; st.sig = ''; load('manual'); }
      else if (e.target.id === 'mmOrigin') { st.origin = e.target.value; st.sig = ''; load('manual'); }
    });
    root.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && st.sheet) closeSheet();
    });

    ctx.events.onResync(() => load('resync'));
    ctx.every(POLL_MS, () => load('poll'));
    states.loading(listEl, 'Reading memory');
    await load('show');
  },

  async refresh(ctx, reason) {
    if (reason === 'manual') ctx.notify({ level: 'info', title: 'Memory', detail: 'This screen re-reads every few seconds while it is open.', ttl: 3000 });
  },
};
