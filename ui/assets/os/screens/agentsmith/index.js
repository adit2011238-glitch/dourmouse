/* AGENTSMITH: the review gate for a tool Dourmouse drafted for itself.

   The model can draft; only the owner approves. So APPROVE and REJECT are
   offered only inside a draft's opened detail, below the exact module text
   that would be written and run and its sha256, and the approval sends that
   hash so the owner approves the bytes they read. Approving does not make a
   tool callable: it needs a server restart, and this screen keeps saying so
   (from a real check of the running server's registry) until the tool is
   live. Every read is the server's; a failed read says so. */

import { states } from '../../kit/states.js';
import { confirmHere } from '../../kit/confirm-card.js';
import { agoLabel } from '../../kit/format.js';
import { isAbort } from '../../core/api.js';
import {
  signature, statusInfo, GROUPS, grouped, ledeLine, restartLine, boardSignature, approvePrompt, rejectPrompt,
  draftDirective, draftCheck, diskLine, whyNotApprovable,
} from './helpers.js';

const POLL_MS = 10000;

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function button(label, spec, cls = '') {
  const b = el('button', 'os-btn' + (cls ? ' ' + cls : ''), label);
  b.type = 'button';
  b.dataset.spec = spec;
  return b;
}

export default {
  id: 'AGENTSMITH',
  sub: 'self-extension',
  css: true,
  thread: false,

  async mount(root, ctx) {
    const st = { board: null, sig: '', open: new Map(), drafting: false, seq: 0 };

    /* ---------------- skeleton ---------------- */
    const noteEl = el('div', 'as-note');
    noteEl.setAttribute('role', 'status');
    noteEl.hidden = true;
    const restartEl = el('div', 'as-restart');
    restartEl.setAttribute('role', 'status');
    restartEl.hidden = true;
    const confirmEl = el('div', 'as-confirm');
    const ledeEl = el('div', 'as-lede');
    const draftEl = el('div', 'as-draft card');
    draftEl.hidden = true;
    const listEl = el('div', 'as-list');
    listEl.dataset.region = '';
    const footEl = el('div', 'muted as-foot',
      'Agent Smith can draft a tool. It cannot approve one, and no chat tool can. An approved tool always asks for your confirmation before it runs. The code is not scanned for safety: reading it is the review.');
    root.replaceChildren(noteEl, restartEl, confirmEl, ledeEl, draftEl, listEl, footEl);
    root.dataset.state = 'populated';

    const note = (text, tone) => {
      noteEl.hidden = !text;
      noteEl.textContent = text || '';
      noteEl.dataset.tone = tone || '';
    };

    /* ---------------- stage bar ---------------- */
    function paintActions() {
      ctx.chrome.setActions([{
        id: 'draft', label: 'DRAFT TOOL', kind: 'primary', pressed: st.drafting,
        spec: 'Asks Agent Smith to write a new tool for something Dourmouse cannot do yet. It only drafts: it cannot register the tool, and a draft does nothing until you read the code and approve it below.',
        onClick: () => (st.drafting ? closeDraft() : openDraft()),
      }]);
    }

    /* ---------------- DRAFT TOOL ---------------- */
    let offDraftEsc = null;
    const df = {};

    function openDraft() {
      if (!df.text) {
        const lab = el('label', 'as-lab');
        lab.append(el('span', 'who', 'What should the new tool do?'));
        const text = el('textarea', 'as-field');
        text.rows = 4;
        text.dataset.spec = 'Describe the capability Dourmouse is missing, in a sentence or more. Agent Smith writes the tool and its test from this.';
        lab.append(text);
        const status = el('div', 'as-cstatus');
        status.setAttribute('role', 'alert');
        const what = el('div', 'muted', 'This sends one directive to Agent Smith on HOME and runs a model turn. It writes a draft file. It registers nothing: the draft appears below and waits for you.');
        const row = el('div', 'as-btns');
        const cancel = button('CANCEL', 'Closes this and sends nothing. Esc does the same.');
        const send = button('ASK AGENT SMITH', 'Sends the directive to Agent Smith on HOME. It only drafts. You approve or reject the result below.', 'os-btn--primary');
        cancel.addEventListener('click', () => closeDraft());
        send.addEventListener('click', () => sendDraft());
        row.append(cancel, send);
        draftEl.replaceChildren(el('div', 'lbl', 'Draft a tool'), lab, what, status, row);
        df.text = text;
        df.status = status;
        df.send = send;
      }
      df.status.textContent = '';
      draftEl.hidden = false;
      st.drafting = true;
      paintActions();
      if (!offDraftEsc) offDraftEsc = ctx.keys.pushEsc(() => closeDraft());
      df.text.focus();
    }

    function closeDraft() {
      draftEl.hidden = true;
      st.drafting = false;
      if (offDraftEsc) offDraftEsc();
      offDraftEsc = null;
      paintActions();
    }

    function sendDraft() {
      const text = df.text.value;
      const check = draftCheck(text);
      if (!check.ok) {
        df.status.textContent = check.error;
        return;
      }
      if (ctx.chat.busy()) {
        df.status.textContent = 'HOME is still working on an earlier directive. Wait for it or stop it there, then send.';
        return;
      }
      df.status.textContent = '';
      ctx.chat.send(draftDirective(text), { focusAgent: 'agent_smith' })
        .catch((err) => ctx.notify({ level: 'error', title: 'Could not reach Agent Smith', detail: err && err.message }));
      df.text.value = '';
      closeDraft();
      const a = el('a', '', 'Open HOME to follow it');
      a.href = '#/home';
      noteEl.hidden = false;
      noteEl.dataset.tone = 'info';
      noteEl.replaceChildren(document.createTextNode('Sent to Agent Smith. When it writes the draft it appears below (this list reads again every 10 seconds). '), a);
    }

    /* ---------------- the list ---------------- */
    function paintTop() {
      const b = st.board;
      ledeEl.textContent = ledeLine(b);
      const line = restartLine(b);
      restartEl.hidden = !line;
      restartEl.textContent = line;
    }

    function cardNode(d) {
      const info = statusInfo(d);
      const open = st.open.get(d.id);
      const card = el('div', 'card as-card');
      card.dataset.id = d.id;
      card.dataset.status = d.status;
      const head = el('div', 'as-head');
      head.append(el('span', 'as-sig', signature(d.tool_name, d.parameters_schema)), el('span', 'tag ' + info.tone, info.word));
      const gap = el('div', 'muted as-gap', d.capability_gap || d.description || '');
      const meta = [d.id, d.created_at ? 'drafted ' + agoLabel(d.created_at) : '', d.goal_id ? 'goal ' + d.goal_id : '', d.decided_at ? 'decided ' + agoLabel(d.decided_at) : '']
        .filter(Boolean).join(' · ');
      const btns = el('div', 'as-btns');
      const review = button(open ? 'HIDE CODE' : 'REVIEW CODE',
        'Opens the exact code Dourmouse would write and run, its test, and its hash. Approve and reject are offered only there, below the code.');
      review.dataset.review = d.id;
      review.setAttribute('aria-expanded', String(Boolean(open)));
      btns.append(review);
      card.append(head, gap, el('div', 'muted as-meta', meta), btns);
      if (open) card.append(detailNode(d, open));
      return card;
    }

    function detailNode(d, open) {
      const box = el('div', 'as-detail');
      if (open.state === 'loading') {
        const s = el('div');
        states.loading(s, 'Reading the draft');
        box.append(s);
        return box;
      }
      if (open.state === 'error') {
        const s = el('div');
        states.error(s, open.error, { title: 'Could not read this draft', retry: () => openDraftDetail(d.id, true) });
        box.append(s);
        return box;
      }
      const x = open.data;
      box.append(el('div', 'who', 'What it does'), el('div', 'muted as-body', x.description || ''));
      if (x.decision_reason) {
        box.append(el('div', 'who', x.status === 'APPROVAL_FAILED' ? 'Why approval failed' : 'Decision'));
        box.append(x.decision_reason.length > 200 || x.decision_reason.includes('\n') ? codeBlock(x.decision_reason, 'Decision text') : el('div', 'muted as-body', x.decision_reason));
      }
      box.append(el('div', 'who', 'Parameters schema'), codeBlock(JSON.stringify(x.parameters_schema || {}, null, 2), 'Parameters schema'));
      if (x.too_large_to_review) {
        box.append(el('div', 'as-warn', 'This draft is ' + x.module_chars + ' characters, too large to read on this screen. It cannot be approved here.'));
      } else {
        box.append(el('div', 'who', 'The module that would be written and run (' + x.module_lines + ' lines, sha256 ' + x.preview_sha256 + ')'));
        box.append(codeBlock(x.module_preview, 'Module source'));
        box.append(el('div', 'who', 'The draft\'s own test, run once when you approve (' + x.test_chars + ' characters)'));
        box.append(codeBlock(x.test_source, 'Test source'));
      }
      if (x.status === 'APPROVED') {
        box.append(el('div', 'muted as-body', (x.module_sha256 ? 'Recorded at approval: sha256 ' + x.module_sha256 + '. ' : '') + diskLine(x)));
        if (x.live === false) box.append(el('div', 'as-warn', 'Not live: it was approved after the server started, so it cannot be called until the server restarts.'));
        if (x.live === true) box.append(el('div', 'muted as-body', 'It is registered in the running server and always asks for your confirmation.'));
      }
      if (x.status === 'DRAFTED') {
        const why = whyNotApprovable(x);
        const row = el('div', 'as-btns as-decide');
        if (why) {
          row.append(el('div', 'as-warn', why));
        } else {
          const ok = button('APPROVE', 'Approves exactly the module text above (its hash is sent with the request). It runs the draft\'s own test, writes the tool, and needs a server restart before it can be called. Every call still asks for your confirmation.', 'os-btn--primary');
          ok.dataset.approve = x.id;
          row.append(ok);
        }
        const rej = button('REJECT', 'Rejects this draft after asking. It is kept as rejected and can never be approved.', 'os-btn--danger');
        rej.dataset.reject = x.id;
        row.append(rej);
        box.append(row);
      }
      return box;
    }

    function codeBlock(text, label) {
      const pre = el('pre', 'as-code');
      pre.textContent = text || '(empty)';
      pre.tabIndex = 0;
      pre.setAttribute('aria-label', label);
      return pre;
    }

    function paintList() {
      const b = st.board;
      if (!b) return;
      paintTop();
      if (!b.drafts.length) {
        states.empty(listEl, 'Nothing has been drafted yet.', { hint: 'Agent Smith drafts a tool when Dourmouse hits something it cannot do. Use DRAFT TOOL to ask for one.' });
        return;
      }
      listEl.dataset.state = 'populated';
      const by = grouped(b.drafts);
      const nodes = [];
      if (!by.pending) nodes.push(el('div', 'muted as-none', 'Nothing waiting on you right now.'));
      GROUPS.forEach((g) => {
        if (!by[g.key]) return;
        nodes.push(el('div', 'lbl as-group', g.label + ' (' + by[g.key].length + ')'));
        by[g.key].forEach((d) => nodes.push(cardNode(d)));
      });
      const keep = listEl.querySelector('.st-stale');
      listEl.replaceChildren(...nodes);
      if (keep) listEl.prepend(keep);
    }

    async function load(reason) {
      if (!st.board) states.loading(listEl, 'Reading the drafts');
      try {
        const b = await ctx.api.get('/api/os/agentsmith/board');
        if (ctx.signal.aborted) return;
        states.clearStale(listEl);
        const sig = boardSignature(b);
        const changed = sig !== st.sig || !st.board;
        st.board = b;
        st.sig = sig;
        if (changed) {
          /* an open draft whose status moved is read again */
          st.open.forEach((o, id) => {
            const d = b.drafts.find((x) => x.id === id);
            if (!d) st.open.delete(id);
            else if (o.data && (o.data.status !== d.status || o.data.live !== d.live)) openDraftDetail(id, true);
          });
          paintList();
        } else {
          paintTop();
        }
      } catch (err) {
        if (isAbort(err) || ctx.signal.aborted) return;
        if (st.board) states.stale(listEl, 'Could not refresh: ' + err.message + ' The list below is from the last read.');
        else states.error(listEl, err, { title: 'Could not read the drafts', retry: () => load('manual') });
      }
    }

    async function openDraftDetail(id, force = false) {
      if (st.open.has(id) && !force) {
        st.open.delete(id);
        paintList();
        return;
      }
      st.open.set(id, { state: 'loading' });
      paintList();
      try {
        const r = await ctx.api.get('/api/os/agentsmith/draft?id=' + encodeURIComponent(id));
        if (ctx.signal.aborted || !st.open.has(id)) return;
        st.open.set(id, { state: 'ready', data: r.draft });
      } catch (err) {
        if (isAbort(err) || ctx.signal.aborted || !st.open.has(id)) return;
        st.open.set(id, { state: 'error', error: err });
      }
      paintList();
    }

    /* ---------------- approve and reject: the owner's click, then a card ---------------- */
    let offConfirmEsc = null;
    function clearConfirm() {
      confirmEl.replaceChildren();
      if (offConfirmEsc) offConfirmEsc();
      offConfirmEsc = null;
    }

    function ask(prompt, run, origin) {
      clearConfirm();
      const handle = confirmHere(confirmEl, prompt, run, {
        onDone: (ok) => {
          if (ok) clearConfirm();
          else {
            clearConfirm();
            if (origin && origin.isConnected) origin.focus();
          }
        },
      });
      offConfirmEsc = ctx.keys.pushEsc(() => {
        if (handle.entry.busy) return;
        clearConfirm();
        if (origin && origin.isConnected) origin.focus();
      });
    }

    function approve(id, origin) {
      const o = st.open.get(id);
      if (!o || o.state !== 'ready') return;
      const d = o.data;
      ask(approvePrompt(d), async () => {
        let r;
        try {
          r = await ctx.api.post('/api/os/agentsmith/approve', { id, sha256: d.preview_sha256 });
        } catch (err) {
          load('action');
          throw err;
        }
        note((r.entry && r.entry.tool_name ? r.entry.tool_name + ' ' : '') + (r.note || 'approved.'), 'ok');
        st.open.delete(id);
        await load('action');
        openDraftDetail(id, true);
      }, origin);
    }

    function reject(id, origin) {
      const o = st.open.get(id);
      if (!o || o.state !== 'ready') return;
      ask(rejectPrompt(o.data), async () => {
        await ctx.api.post('/api/self_extensions/reject', { id, reason: 'rejected from the AGENTSMITH screen' });
        note('Rejected ' + o.data.tool_name + '.', 'ok');
        st.open.delete(id);
        await load('action');
      }, origin);
    }

    root.addEventListener('click', (e) => {
      const rv = e.target.closest('[data-review]');
      if (rv) {
        openDraftDetail(rv.dataset.review);
        return;
      }
      const ap = e.target.closest('[data-approve]');
      if (ap) {
        approve(ap.dataset.approve, ap);
        return;
      }
      const rj = e.target.closest('[data-reject]');
      if (rj) reject(rj.dataset.reject, rj);
    });

    /* ---------------- live: there is no event for a new draft, so it is read on a timer ---------------- */
    ctx.events.onResync(() => load('resync'));
    ctx.every(POLL_MS, () => load('poll'));

    paintActions();
    await load('show');
  },

  async refresh(ctx, reason) {
    if (reason === 'manual') ctx.notify({ level: 'info', title: 'AGENTSMITH', detail: 'This list reads the drafts again every 10 seconds.', ttl: 2500 });
  },
};
