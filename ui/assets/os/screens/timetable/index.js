/* TIMETABLE: the recurring routines the scheduler runs. Reads
   GET /api/schedules and the tool tiers from GET /api/roster.

   EDIT changes only WHEN (POST /api/schedules/update); DISABLE and ENABLE keep
   the routine and its last run (POST /api/schedules/toggle). Each is the
   owner's click plus a card stating what will change. NEW ROUTINE hands the
   request to HOME, because a routine is a tool call plus a schedule and the
   model chooses the tool; that call is gated, so it is approved there.

   The store keeps only the last run time, so there is no run history and no
   "missed" record. What is shown instead is true: an overdue routine will be
   fired once by the runner's next check (catch-up). It is never claimed as run. */

import { html, setHtml } from '../../kit/html.js';
import { states } from '../../kit/states.js';
import { agoLabel, plural } from '../../kit/format.js';
import { isAbort } from '../../core/api.js';
import { isOverdue, argsPreview, toolTiers, cannotRunUnattended, sortEntries, handoffText } from './helpers.js';

const POLL_MS = 10000;

export default {
  id: 'TIMETABLE',
  sub: 'routines',
  css: true,
  thread: false,

  async mount(root, ctx) {
    const st = { entries: null, tiers: {}, tiersError: '', error: null, edit: null, editError: '', sig: '', hand: null };

    root.dataset.state = 'populated';
    setHtml(root, html`
      <div class="tt-note" id="ttNote" role="status" hidden></div>
      <div id="ttConfirm"></div>
      <div class="card tt-hand" id="ttHandCard" hidden><div class="lbl">New routine</div><div id="ttHand"></div></div>
      <div id="ttList" data-region></div>
      <div class="muted tt-foot">The scheduler only runs while this server runs. Nothing records a run that was missed while it was off: the store keeps only each routine's last run. When a routine is overdue the runner fires it once on its next check (catch-up), and it is marked overdue here until then. Only tools that need no confirmation can run unattended; a routine on any other tool is stopped by the gate and does nothing.</div>`);
    const $ = (id) => root.querySelector('#' + id);
    const noteEl = $('ttNote');
    const confirmEl = $('ttConfirm');
    const listEl = $('ttList');
    const handCard = $('ttHandCard');
    const handEl = $('ttHand');

    const note = (text, tone) => {
      noteEl.hidden = !text;
      noteEl.textContent = text || '';
      noteEl.dataset.tone = tone || '';
    };

    ctx.chrome.setActions([{
      id: 'new', label: 'NEW ROUTINE', kind: 'primary',
      spec: 'Describe a routine in words. It is sent to HOME, where the model proposes the tool and the schedule and you approve the change. Nothing is created from this screen.',
      onClick: () => openHand(),
    }]);

    function confirmHere(prompt, run) {
      const entry = { id: 'local-' + Date.now(), prompt, email: false, autonomous: false, state: 'pending', busy: false, error: '' };
      const card = ctx.kit.approvalCard(entry, async (ok) => {
        if (!ok) {
          entry.state = 'declined';
          card.paint();
          return true;
        }
        entry.busy = true;
        entry.error = '';
        card.paint();
        try {
          await run();
          entry.state = 'approved';
        } catch (err) {
          entry.error = err && err.message ? err.message : String(err);
        }
        entry.busy = false;
        card.paint();
        return true;
      });
      confirmEl.replaceChildren(card.el);
      card.focus();
    }

    /* ---------------- reads ---------------- */
    async function load(reason) {
      const [sch, ros] = await Promise.allSettled([ctx.api.get('/api/schedules'), ctx.api.get('/api/roster')]);
      if (ctx.signal.aborted) return;
      if (ros.status === 'fulfilled') {
        st.tiers = toolTiers(ros.value);
        st.tiersError = '';
      } else if (!isAbort(ros.reason)) {
        st.tiersError = ros.reason.message;
      }
      if (sch.status === 'fulfilled') {
        st.error = null;
        const sig = JSON.stringify(sch.value) + JSON.stringify(st.tiers);
        if (sig === st.sig && reason === 'poll' && !st.edit) {
          paintOverdue();
          return;
        }
        st.sig = sig;
        st.entries = sortEntries(sch.value.schedules);
      } else if (!isAbort(sch.reason)) {
        st.error = sch.reason;
      } else {
        return;
      }
      paint();
    }

    /* ---------------- painting ---------------- */
    function paint() {
      if (st.error && !st.entries) {
        root.dataset.state = 'error';
        states.error(listEl, st.error, { title: 'Could not read the routines', retry: () => { states.loading(listEl, 'Reading the routines'); load('manual'); } });
        return;
      }
      if (!st.entries) return;
      root.querySelectorAll(':scope > .st-stale').forEach((n) => n.remove());
      if (st.error) states.stale(root, 'Could not refresh: ' + st.error.message + ' Showing the last read.');
      else root.dataset.state = 'populated';
      const on = st.entries.filter((e) => e.enabled).length;
      ctx.chrome.setSub(plural(st.entries.length, 'routine') + ' · ' + on + ' on');
      ctx.chrome.setLive(false);
      if (!st.entries.length) {
        if (!st.error) root.dataset.state = 'empty';
        states.empty(listEl, 'Nothing is scheduled yet.', { hint: 'Press NEW ROUTINE and describe what should happen and when.' });
        return;
      }
      listEl.dataset.state = 'populated';
      setHtml(listEl, html`<div class="card tt-card">${st.tiersError ? html`<div class="muted tt-err">Tool tiers could not be read (${st.tiersError}), so the unattended check below is not shown.</div>` : ''}${st.entries.map((e) => row(e))}</div>`);
      const box = listEl.querySelector('[data-edit-input]');
      if (box) box.focus();
    }

    function row(e) {
      const overdue = isOverdue(e);
      const blocked = cannotRunUnattended(e, st.tiers);
      const editing = st.edit === e.id;
      return html`<div class="tt-item" data-sched="${e.id}" data-enabled="${String(Boolean(e.enabled))}">
        <div class="os-row">
          <span class="tag ${e.enabled ? 'ok' : ''}">${e.schedule_description || 'unknown schedule'}</span>
          <span class="rt"><b>${e.tool}</b> <span class="muted mono">${argsPreview(e.arguments)}</span>
            <div class="muted">${e.enabled ? 'next run ' + (e.next_run || 'unknown') : 'paused'} · ${e.last_run ? 'last run ' + agoLabel(e.last_run) : 'never run'}</div></span>
          ${!e.enabled ? html`<span class="tag">paused</span>` : ''}
          <span data-overdue="${e.id}">${overdue ? html`<span class="tag warn" title="The runner fires this once on its next check">overdue</span>` : ''}</span>
          ${blocked ? html`<span class="tag bad" title="This tool needs confirmation, so the unattended runner stops it">needs confirmation, will not run</span>` : ''}
          <span class="os-row-actions">
            <button type="button" class="os-btn" data-edit="${e.id}" aria-expanded="${String(editing)}" data-spec="Changes when this routine runs, not what it does. The routine keeps its id and its last run time.">EDIT</button>
            <button type="button" class="os-btn" data-toggle="${e.id}" data-spec="${e.enabled ? 'Stops future runs after asking. The routine and its last run time are kept.' : 'Turns this routine back on after asking.'}">${e.enabled ? 'DISABLE' : 'ENABLE'}</button>
          </span>
        </div>
        ${editing ? html`<div class="tt-edit"><label class="tt-l" for="ttEdit-${e.id}">When, in words (for example: daily at 8:30, every 30 minutes, every Monday at 9:00)</label>
          <div class="tt-editrow"><input id="ttEdit-${e.id}" class="tt-in" type="text" maxlength="120" value="${e.schedule_text || ''}" data-edit-input data-spec="The new schedule in words. The server checks it and says why if it does not understand.">
          <button type="button" class="os-btn os-btn--primary" data-save="${e.id}" data-spec="Asks for confirmation, then changes the schedule.">SAVE</button>
          <button type="button" class="os-btn" data-edit-cancel data-spec="Closes this editor without changing anything.">CANCEL</button></div>
          ${st.editError ? html`<div class="tt-err" role="alert">${st.editError}</div>` : ''}</div>` : ''}
      </div>`;
    }

    /* the overdue tag depends on the clock, so it is refreshed without a full repaint */
    function paintOverdue() {
      if (!st.entries) return;
      for (const e of st.entries) {
        const slot = listEl.querySelector('[data-overdue="' + CSS.escape(e.id) + '"]');
        if (slot) slot.replaceChildren(...(isOverdue(e) ? [Object.assign(document.createElement('span'), { className: 'tag warn', textContent: 'overdue', title: 'The runner fires this once on its next check' })] : []));
      }
    }

    /* ---------------- new routine (hand-off) ---------------- */
    function openHand() {
      handCard.hidden = false;
      st.hand = st.hand || '';
      setHtml(handEl, html`
        <label class="tt-l" for="ttWhat">What should happen, and when</label>
        <textarea id="ttWhat" class="tt-in" rows="3" data-hand-input placeholder="Every weekday at 07:30, check my mail for anything urgent" data-spec="Your request in words. It is sent to HOME as a message; the model picks the tool and the schedule.">${st.hand}</textarea>
        <div class="muted tt-warn">Sending puts this message into the HOME conversation. The model then proposes a schedule_recurring call, and that call waits for your approval there. Nothing is scheduled from this screen.</div>
        <div class="tt-editrow"><button type="button" class="os-btn os-btn--primary" data-send-hand data-spec="Asks for confirmation, then sends your request to HOME.">SEND TO HOME</button>
        <button type="button" class="os-btn" data-hand-close data-spec="Closes this form without sending anything.">DISCARD</button></div><div class="tt-err" id="ttHandErr" role="alert"></div>`);
      handEl.querySelector('#ttWhat').focus();
    }

    function closeHand() {
      handCard.hidden = true;
      handEl.replaceChildren();
    }

    function sendHand() {
      const what = handEl.querySelector('#ttWhat').value.trim();
      st.hand = what;
      const err = handEl.querySelector('#ttHandErr');
      if (!what) {
        err.textContent = 'Write what should happen and when.';
        return;
      }
      if (what.length > 1200) {
        err.textContent = 'That is longer than 1200 characters.';
        return;
      }
      err.textContent = '';
      confirmHere('Send this to HOME? "' + what + '". The model will propose a routine and wait for your approval before anything is scheduled.', async () => {
        await ctx.chat.send(handoffText(what));
        st.hand = '';
        closeHand();
        const a = document.createElement('a');
        a.href = '#/home';
        a.textContent = 'Open HOME to follow it';
        noteEl.hidden = false;
        noteEl.dataset.tone = 'ok';
        noteEl.replaceChildren(document.createTextNode('Sent. Nothing is scheduled until you approve it. '), a);
      });
    }

    /* ---------------- actions ---------------- */
    const find = (id) => (st.entries || []).find((e) => e.id === id);

    async function post(path, body, okText) {
      await ctx.api.post(path, body);
      note(okText, 'ok');
      if (!ctx.signal.aborted) await load('manual');
    }

    root.addEventListener('click', (e) => {
      const q = (sel) => e.target.closest(sel);
      let el;
      if ((el = q('[data-edit]'))) {
        st.edit = st.edit === el.dataset.edit ? null : el.dataset.edit;
        st.editError = '';
        paint();
      } else if (q('[data-edit-cancel]')) {
        st.edit = null;
        paint();
      } else if ((el = q('[data-save]'))) {
        saveEdit(el.dataset.save);
      } else if ((el = q('[data-toggle]'))) {
        const en = find(el.dataset.toggle);
        if (!en) return;
        const to = !en.enabled;
        confirmHere((to ? 'Turn this routine back on? ' : 'Disable this routine? ') + en.tool + ', ' + (en.schedule_description || '') + '. ' +
          (to ? 'It will run again when it is next due.' : 'It stops running until you enable it again. The routine and its last run time are kept.'),
        () => post('/api/schedules/toggle', { id: en.id, enabled: to }, to ? 'Routine enabled.' : 'Routine disabled.'));
      } else if (q('[data-send-hand]')) {
        sendHand();
      } else if (q('[data-hand-close]')) {
        closeHand();
      }
    });

    function saveEdit(id) {
      const en = find(id);
      const input = listEl.querySelector('[data-edit-input]');
      if (!en || !input) return;
      const text = input.value.trim();
      if (!text) {
        st.editError = 'Write when it should run.';
        paint();
        return;
      }
      confirmHere('Change when "' + en.tool + '" runs, from "' + (en.schedule_text || en.schedule_description) + '" to "' + text + '"? What it does stays the same and its last run time is kept.',
        async () => {
          try {
            await post('/api/schedules/update', { id: id, schedule_text: text }, 'Schedule changed.');
            st.edit = null;
            st.editError = '';
            paint();
          } catch (err) {
            st.editError = err.message;
            paint();
            throw err;
          }
        });
    }

    root.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        if (st.edit) {
          st.edit = null;
          paint();
        } else if (!handCard.hidden) {
          closeHand();
        }
      } else if (e.key === 'Enter' && e.target.matches && e.target.matches('[data-edit-input]') && st.edit) {
        e.preventDefault();
        saveEdit(st.edit);
      }
    });

    ctx.events.onResync(() => load('resync'));
    ctx.every(POLL_MS, () => load('poll'));
    states.loading(listEl, 'Reading the routines');
    await load('show');
  },

  async refresh(ctx, reason) {
    if (reason === 'manual') ctx.notify({ level: 'info', title: 'TIMETABLE', detail: 'This screen re-reads every few seconds while it is open.', ttl: 3000 });
  },
};
