/* COMMS: the real Gmail inbox and the mail agent's conversation.

   The inbox reads /api/os/comms/inbox (structured rows, real unread and
   starred state when the Google account is signed in, a stated cache age).
   Opening a row reads the message. Archive, trash and flag are the owner's own
   click on a row and each asks first in a card that names the message and says
   what will happen. Nothing here can delete permanently.

   Sending mail never happens from this page: COMPOSE hands a fixed directive
   to the mail agent, whose gmail_send tool stops at the real approval gate.
   The card that asks shows the recipient and the start of the body, and the
   send is finished by typing "send it" (or cancelled). Reading is not gated. */

import { states } from '../../kit/states.js';
import { mountThreadView } from '../../kit/thread-view.js';
import { confirmHere } from '../../kit/confirm-card.js';
import { isAbort } from '../../core/api.js';
import {
  senderName, whenLabel, listAge, composeCheck, composeDirective, replyDraft,
  actionPrompt, doneLine, withFlag, withoutRow, unavailableHint,
} from './helpers.js';

const LIMIT = 25;
const POLL_MS = 180000;

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
  id: 'COMMS',
  sub: 'real gmail inbox',
  css: true,
  thread: true,

  async mount(root, ctx) {
    const st = {
      rows: [], payload: null, query: '', selected: '', loading: false,
      detail: null, detailSeq: 0, composing: false, refreshing: false,
    };

    /* ---------------- skeleton ---------------- */
    const noteEl = el('div', 'cm-note');
    noteEl.setAttribute('role', 'status');
    noteEl.hidden = true;
    const confirmEl = el('div', 'cm-confirm');
    const barEl = el('div', 'cm-bar');
    const input = el('input', 'cm-field cm-q');
    input.type = 'search';
    input.placeholder = 'Search by from:, subject:, or plain words. Empty shows the inbox.';
    input.setAttribute('aria-label', 'Search mail');
    input.dataset.spec = 'Searches your mailbox with Gmail search words (from:, subject:, newer_than:3d, or plain text). An empty box shows the inbox. Reading is not gated.';
    const searchBtn = button('SEARCH', 'Runs the search against Gmail now. Empty shows the inbox.');
    const clearBtn = button('CLEAR', 'Clears the search and shows the inbox again.');
    clearBtn.hidden = true;
    const ageEl = el('span', 'muted cm-age');
    barEl.append(input, searchBtn, clearBtn, ageEl);
    const listEl = el('div', 'cm-list');
    listEl.dataset.region = '';
    const detailEl = el('div', 'cm-detail');
    detailEl.hidden = true;
    const composeEl = el('div', 'cm-compose');
    composeEl.hidden = true;
    const footEl = el('div', 'muted cm-foot', 'Sending, deleting and flagging always ask first. Reading does not. Trash is Gmail\'s Trash, recoverable for 30 days.');
    const threadHead = el('div', 'lbl cm-thread-head', 'Mail agent conversation');
    const threadEl = el('div', 'cm-thread');
    root.replaceChildren(noteEl, confirmEl, barEl, listEl, footEl, detailEl, composeEl, threadHead, threadEl);
    root.dataset.state = 'populated';

    const note = (text, tone) => {
      noteEl.hidden = !text;
      noteEl.textContent = text || '';
      noteEl.dataset.tone = tone || '';
    };

    /* ---------------- stage bar and the conversation ---------------- */
    const view = mountThreadView(threadEl, ctx, {
      scroller: root.parentElement,
      autonomous: false,
      placeholder: 'Ask about your mail. Sending always asks first.',
      emptyMessage: 'No mail directives yet.',
      emptyHint: 'Use COMPOSE to write a message. It goes to the mail agent, and an approval card shows who it goes to before anything is sent.',
      actions: () => [
        {
          id: 'refresh', label: st.refreshing ? 'READING' : 'REFRESH', disabled: st.refreshing,
          spec: 'Reads Gmail again now, skipping the short cache the inbox is normally served from.',
          onClick: () => load('manual', { fresh: true }),
        },
        {
          id: 'compose', label: 'COMPOSE', kind: 'primary', pressed: st.composing,
          spec: 'Opens a message form. Sending is not done here: it goes to the mail agent, and a real approval card shows the recipient before anything leaves.',
          onClick: () => (st.composing ? closeCompose() : openCompose({})),
        },
      ],
    });

    /* ---------------- the list ---------------- */
    function paintAge() {
      const p = st.payload;
      ageEl.textContent = p && p.state === 'populated' ? listAge(p) : '';
    }

    function rowNode(row) {
      const wrap = el('div', 'os-row cm-row');
      wrap.dataset.id = row.id;
      wrap.setAttribute('aria-selected', String(st.selected === row.id));
      if (row.unread) wrap.dataset.unread = 'true';
      const open = el('button', 'cm-open');
      open.type = 'button';
      open.dataset.open = row.id;
      open.dataset.spec = 'Opens this message here. Reading is not gated. It does not change anything in Gmail.';
      if (row.snippet) open.title = row.snippet;
      const who = el('b', 'cm-who', senderName(row.from) || '(unknown sender)');
      const subj = el('span', 'muted cm-subj', row.subject || '(no subject)');
      const when = el('span', 'muted cm-when', whenLabel(row));
      open.append(who, subj, when);
      const actions = el('span', 'os-row-actions');
      const flag = button(row.flagged ? 'UNFLAG' : 'FLAG', row.flagged
        ? 'Removes the star in Gmail after asking. Nothing moves.'
        : 'Stars this message in Gmail after asking. Nothing moves.');
      flag.dataset.act = row.flagged ? 'unflag' : 'flag';
      flag.dataset.id = row.id;
      const arch = button('ARCHIVE', 'Takes this message out of the inbox after asking. It stays in All Mail and stays searchable. Nothing is deleted.');
      arch.dataset.act = 'archive';
      arch.dataset.id = row.id;
      const trash = button('TRASH', 'Moves this message to Gmail Trash after asking. Gmail keeps it for 30 days. It is never a permanent delete.', 'os-btn--danger');
      trash.dataset.act = 'trash';
      trash.dataset.id = row.id;
      actions.append(flag, arch, trash);
      wrap.append(open);
      if (row.flagged) wrap.append(el('span', 'tag warn', 'flagged'));
      wrap.append(actions);
      return wrap;
    }

    function paintList() {
      const p = st.payload;
      if (!p) return;
      if (p.state === 'unavailable') {
        states.unavailable(listEl, p.note || 'Gmail is not connected.', { detail: unavailableHint(p.mode), retry: () => load('manual', { fresh: true }) });
        return;
      }
      if (!st.rows.length) {
        const searching = Boolean(st.query);
        states.empty(listEl, searching ? 'No messages matched "' + st.query + '".' : (p.note && !/^GMAIL SEARCH: inbox is empty/i.test(p.note) ? p.note : 'The inbox is empty.'),
          { hint: searching ? 'Try Gmail words such as from:name or newer_than:7d.' : 'New mail appears here after the next read.' });
        return;
      }
      listEl.dataset.state = 'populated';
      const frag = st.rows.map(rowNode);
      const keep = listEl.querySelector('.st-stale');
      listEl.replaceChildren(...frag);
      if (keep) listEl.prepend(keep);
    }

    async function load(reason, { fresh = false, query = st.query } = {}) {
      st.query = query;
      clearBtn.hidden = !st.query;
      if (fresh) {
        st.refreshing = true;
        view.paintChrome();
      }
      if (!st.payload) states.loading(listEl, st.query ? 'Searching Gmail' : 'Reading the inbox');
      const url = '/api/os/comms/inbox?limit=' + LIMIT + (st.query ? '&q=' + encodeURIComponent(st.query) : '') + (fresh ? '&fresh=1' : '');
      try {
        const data = await ctx.api.get(url);
        if (ctx.signal.aborted) return;
        st.payload = data;
        st.rows = Array.isArray(data.rows) ? data.rows : [];
        if (st.selected && !st.rows.some((r) => r.id === st.selected) && !st.query) closeDetail();
        states.clearStale(listEl);
        paintList();
        paintAge();
        if (data.__stale) states.stale(listEl, 'Showing the last copy this window kept. The server did not answer.');
      } catch (err) {
        if (isAbort(err) || ctx.signal.aborted) return;
        if (st.rows.length) {
          states.stale(listEl, 'Could not refresh: ' + err.message + ' The rows below are from the last read.');
        } else {
          states.error(listEl, err, { title: 'Could not read your mail', retry: () => load('manual', { fresh: true }) });
        }
        ageEl.textContent = '';
      } finally {
        if (fresh && !ctx.signal.aborted) {
          st.refreshing = false;
          view.paintChrome();
        }
      }
    }

    /* ---------------- one message ---------------- */
    let offDetailEsc = null;

    function closeDetail() {
      st.selected = '';
      st.detail = null;
      st.detailSeq += 1;
      detailEl.hidden = true;
      detailEl.replaceChildren();
      if (offDetailEsc) offDetailEsc();
      offDetailEsc = null;
      listEl.querySelectorAll('.cm-row[aria-selected="true"]').forEach((r) => r.setAttribute('aria-selected', 'false'));
    }

    function rowById(id) {
      return st.rows.find((r) => r.id === id) || null;
    }

    async function openMessage(id) {
      const row = rowById(id);
      if (!row) return;
      st.selected = id;
      listEl.querySelectorAll('.cm-row').forEach((r) => r.setAttribute('aria-selected', String(r.dataset.id === id)));
      const seq = (st.detailSeq += 1);
      detailEl.hidden = false;
      detailEl.dataset.region = '';
      states.loading(detailEl, 'Opening the message');
      if (!offDetailEsc) offDetailEsc = ctx.keys.pushEsc(() => closeDetail());
      try {
        const m = await ctx.api.get('/api/os/comms/message?id=' + encodeURIComponent(id));
        if (seq !== st.detailSeq || ctx.signal.aborted) return;
        st.detail = m;
        paintDetail(row, m);
      } catch (err) {
        if (isAbort(err) || seq !== st.detailSeq || ctx.signal.aborted) return;
        states.error(detailEl, err, { title: 'Could not open this message', retry: () => openMessage(id) });
      }
    }

    function paintDetail(row, m) {
      detailEl.dataset.state = 'populated';
      const card = el('div', 'card');
      const head = el('div', 'cm-dhead');
      const title = el('div', 'cm-dsubj', m.subject || row.subject || '(no subject)');
      const meta = el('div', 'muted cm-dfrom', [m.from || row.from, m.date || row.date].filter(Boolean).join(' · '));
      head.append(title, meta);
      const body = el('pre', 'cm-dbody');
      body.textContent = m.body || '(no text body)';
      body.tabIndex = 0;
      body.setAttribute('aria-label', 'Message text');
      const acts = el('div', 'cm-dacts');
      const reply = button('REPLY', 'Opens the message form filled with this sender and a Re: subject. Nothing is sent from the form: it goes to the mail agent and asks first.', 'os-btn--primary');
      reply.dataset.reply = row.id;
      const flag = button(row.flagged ? 'UNFLAG' : 'FLAG', 'Stars or un-stars this message in Gmail after asking. Nothing moves.');
      flag.dataset.act = row.flagged ? 'unflag' : 'flag';
      flag.dataset.id = row.id;
      const arch = button('ARCHIVE', 'Takes this message out of the inbox after asking. It stays in All Mail.');
      arch.dataset.act = 'archive';
      arch.dataset.id = row.id;
      const trash = button('TRASH', 'Moves this message to Gmail Trash after asking. Recoverable for 30 days.', 'os-btn--danger');
      trash.dataset.act = 'trash';
      trash.dataset.id = row.id;
      const close = button('CLOSE', 'Closes the message. Esc does the same.');
      close.dataset.closeDetail = '1';
      acts.append(reply, flag, arch, trash, close);
      card.append(head, body, acts);
      if (String(m.body || '').length >= 6000) card.append(el('div', 'muted', 'Only the first 6,000 characters are read. The rest is in Gmail.'));
      detailEl.replaceChildren(card);
    }

    /* ---------------- row changes: the owner's click, then a card ---------------- */
    let offConfirmEsc = null;
    function clearConfirm() {
      confirmEl.replaceChildren();
      if (offConfirmEsc) offConfirmEsc();
      offConfirmEsc = null;
    }

    function ask(kind, id, origin) {
      const row = rowById(id);
      if (!row) return;
      clearConfirm();
      const handle = confirmHere(confirmEl, actionPrompt(kind, row), async () => {
        const r = await ctx.api.post('/api/os/comms/' + kind, { id });
        if (kind === 'archive' || kind === 'trash') {
          st.rows = withoutRow(st.rows, id);
          if (st.selected === id) closeDetail();
        } else {
          st.rows = withFlag(st.rows, id, kind === 'flag');
          if (st.selected === id) openMessage(id);
        }
        paintList();
        note(doneLine(kind, r), 'ok');
        load('action', { fresh: true });
      }, {
        onDone: (ok) => {
          clearConfirm();
          if (!ok && origin && origin.isConnected) origin.focus();
        },
      });
      offConfirmEsc = ctx.keys.pushEsc(() => {
        if (handle.entry.busy) return;
        clearConfirm();
        if (origin && origin.isConnected) origin.focus();
      });
    }

    /* ---------------- compose ---------------- */
    let offComposeEsc = null;
    const f = {};

    function buildCompose() {
      const title = el('div', 'lbl', 'Compose');
      const mk = (name, label, spec, tag = 'input') => {
        const wrap = el('label', 'cm-lab');
        wrap.append(el('span', 'who', label));
        const field = el(tag, 'cm-field' + (tag === 'textarea' ? ' cm-body' : ''));
        if (tag === 'input') field.type = 'text';
        field.dataset.spec = spec;
        wrap.append(field);
        f[name] = field;
        return wrap;
      };
      const to = mk('to', 'To', 'One recipient address. The mail agent shows it in its approval card before anything is sent.');
      const subject = mk('subject', 'Subject', 'One line, without a double quote.');
      const body = mk('body', 'Message', 'The message text, sent exactly as written.', 'textarea');
      const status = el('div', 'cm-cstatus');
      status.setAttribute('role', 'alert');
      f.status = status;
      const row = el('div', 'cm-dacts');
      const cancel = button('CANCEL', 'Closes the form and keeps nothing. Esc does the same.');
      const send = button('SEND TO MAIL AGENT', 'Hands this message to the mail agent. It stops at the real approval gate: a card shows the recipient and the start of the body, and nothing leaves until you type "send it".', 'os-btn--primary');
      f.send = send;
      cancel.addEventListener('click', () => closeCompose());
      send.addEventListener('click', () => sendCompose());
      row.append(cancel, send);
      composeEl.replaceChildren(title, to, subject, body, status, row);
    }

    function openCompose({ to = '', subject = '', body = '' }) {
      if (!f.to) buildCompose();
      f.to.value = to;
      f.subject.value = subject;
      f.body.value = body;
      f.status.textContent = '';
      composeEl.hidden = false;
      st.composing = true;
      view.paintChrome();
      if (!offComposeEsc) offComposeEsc = ctx.keys.pushEsc(() => closeCompose());
      (to ? (subject ? f.body : f.subject) : f.to).focus();
      composeEl.scrollIntoView({ block: 'nearest' });
    }

    function closeCompose() {
      composeEl.hidden = true;
      st.composing = false;
      if (offComposeEsc) offComposeEsc();
      offComposeEsc = null;
      view.paintChrome();
    }

    function sendCompose() {
      const draft = { to: f.to.value, subject: f.subject.value, body: f.body.value };
      const check = composeCheck(draft);
      if (!check.ok) {
        f.status.textContent = check.error;
        return;
      }
      if (ctx.chat.busy()) {
        f.status.textContent = 'The conversation is still busy. Wait for it, or press STOP in the directive box, then send.';
        return;
      }
      f.status.textContent = '';
      ctx.chat.send(composeDirective(draft), { focusAgent: 'mail' })
        .catch((err) => ctx.notify({ level: 'error', title: 'Could not hand it to the mail agent', detail: err && err.message }));
      closeCompose();
      note('Handed to the mail agent. Its approval card appears in the conversation below and shows who it goes to. Nothing is sent until you approve it.', 'info');
      threadEl.scrollIntoView({ block: 'nearest' });
    }

    /* ---------------- events on this screen's own elements ---------------- */
    root.addEventListener('click', (e) => {
      const open = e.target.closest('[data-open]');
      if (open) {
        openMessage(open.dataset.open);
        return;
      }
      const act = e.target.closest('[data-act]');
      if (act) {
        ask(act.dataset.act, act.dataset.id, act);
        return;
      }
      const reply = e.target.closest('[data-reply]');
      if (reply) {
        const row = rowById(reply.dataset.reply);
        if (row) openCompose(replyDraft(row, st.detail));
        return;
      }
      if (e.target.closest('[data-close-detail]')) closeDetail();
    });
    const runSearch = () => {
      const q = input.value.trim();
      closeDetail();
      st.rows = [];
      st.payload = null;
      load('manual', { query: q, fresh: !q });
    };
    searchBtn.addEventListener('click', runSearch);
    clearBtn.addEventListener('click', () => {
      input.value = '';
      closeDetail();
      st.rows = [];
      st.payload = null;
      load('manual', { query: '' });
    });
    input.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      runSearch();
    });

    /* ---------------- live: there is no mail event, so it is read on a timer ---------------- */
    ctx.events.onResync(() => load('resync'));
    ctx.every(POLL_MS, () => load('poll'));

    await view.start();
    await load('show');
  },

  async refresh(ctx, reason) {
    /* the shell's refresh shortcut: the stage bar's REFRESH owns the read */
    if (reason === 'manual') ctx.notify({ level: 'info', title: 'COMMS', detail: 'Use REFRESH to read Gmail again now.', ttl: 2500 });
  },
};
