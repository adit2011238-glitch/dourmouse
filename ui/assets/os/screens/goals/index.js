/* GOALS: the persistent autonomous runtime. One read of
   GET /api/os/goals/board (goals with real task counts) every few seconds
   while the screen is open, because the runtime raises no events. Progress is
   completed tasks over tasks not cancelled, never a guess.

   Every change is the owner's click plus a card that says what it will do:
   PAUSE and RESUME (durable; resume restores the exact previous status),
   CANCEL (finished work is kept), a task's APPROVE or DECLINE (shows the exact
   actions it is parked on) and NEW GOAL (runs unattended). AUDIT only reads. */

import { html, setHtml } from '../../kit/html.js';
import { states } from '../../kit/states.js';
import { agoLabel, plural } from '../../kit/format.js';
import { isAbort } from '../../core/api.js';
import {
  goalWord, goalTone, taskTag, progress, splitBoard, describeAudit, validateForm, createBody, cancelPrompt, approvePrompt,
  ACTIVE, LIMITS,
} from './helpers.js';

const AUDIT_SHOWN = 80;
const POLL_MS = 5000;

export default {
  id: 'GOALS',
  sub: 'autonomous runtime',
  css: true,
  thread: false,

  async mount(root, ctx) {
    const st = { board: null, error: null, running: true, audit: {}, form: null, formError: '', busy: false, sig: '' };

    root.dataset.state = 'populated';
    setHtml(root, html`
      <div class="gl-note" id="glNote" role="status" hidden></div>
      <div id="glConfirm"></div>
      <div class="card gl-form" id="glFormCard" hidden><div class="lbl">New goal</div><div id="glForm"></div></div>
      <div id="glList" data-region></div>
      <div class="muted gl-foot">Completion is independently verified: after each task a second reasoning pass checks the claim, and a task whose check could not run is marked unverified rather than accepted.</div>`);
    const $ = (id) => root.querySelector('#' + id);
    const noteEl = $('glNote');
    const confirmEl = $('glConfirm');
    const formCard = $('glFormCard');
    const formEl = $('glForm');
    const listEl = $('glList');

    const note = (text, tone) => {
      noteEl.hidden = !text;
      noteEl.textContent = text || '';
      noteEl.dataset.tone = tone || '';
    };

    ctx.chrome.setActions([{
      id: 'new', label: 'NEW GOAL', kind: 'primary',
      spec: 'Opens a form for a goal you write, with the steps you write. It runs on its own in the background and survives closing this window. Nothing starts until you confirm.',
      onClick: () => openForm(),
    }]);

    /* A page confirmation: nothing runs until APPROVE is pressed. */
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
      try {
        const b = await ctx.api.get('/api/os/goals/board');
        if (ctx.signal.aborted) return;
        st.error = null;
        st.running = !b.runtime || b.runtime.running !== false;
        const sig = JSON.stringify(b);
        if (sig === st.sig && reason === 'poll') return;
        st.sig = sig;
        st.board = b;
      } catch (err) {
        if (isAbort(err)) return;
        st.error = err;
      }
      paint();
    }

    /* ---------------- painting ---------------- */
    function paint() {
      if (st.error && !st.board) {
        root.dataset.state = 'error';
        states.error(listEl, st.error, { title: 'Could not read the goals', retry: () => { states.loading(listEl, 'Reading the goals'); load('manual'); } });
        return;
      }
      if (!st.board) return;
      root.querySelectorAll(':scope > .st-stale').forEach((n) => n.remove());
      if (st.error) states.stale(root, 'Could not refresh: ' + st.error.message + ' Showing the last read.');
      else root.dataset.state = 'populated';
      note(st.running ? '' : 'The goal runtime is switched off on this server (DOURMOUSE_GOAL_RUNTIME=0). Goals are stored, but nothing advances them.', 'warn');
      const { live, finished } = splitBoard(st.board.goals);
      ctx.chrome.setLive(live.some((g) => ACTIVE.has(g.status)) && st.running);
      ctx.chrome.setSub(plural(live.length, 'active goal') + ' · ' + plural(finished.length, 'finished'));
      if (!st.board.goals.length) {
        if (!st.error) root.dataset.state = 'empty';
        states.empty(listEl, 'No goals yet.', { hint: 'Press NEW GOAL, or ask in HOME for something that takes several steps. A goal keeps running after you close this window.' });
        return;
      }
      listEl.dataset.state = 'populated';
      setHtml(listEl, html`
        ${live.length ? live.map((g) => goalCard(g)) : html`<div class="card"><div class="muted">Nothing is running right now.</div></div>`}
        ${finished.length ? html`<div class="card gl-gap"><div class="lbl">Finished</div>${finished.slice(0, 12).map((g) => finishedRow(g))}${finished.length > 12 ? html`<div class="muted">Showing the 12 newest of ${String(finished.length)}.</div>` : ''}${st.board.capped ? html`<div class="muted">Only the newest ${String(st.board.goals.length)} of ${String(st.board.total_goals)} goals are read.</div>` : ''}</div>` : ''}`);
    }

    function goalCard(g) {
      const p = progress(g);
      const paused = g.status === 'PAUSED';
      return html`<div class="card gl-card ${st.board.goals.indexOf(g) > 0 ? 'gl-gap' : ''}" data-goal="${g.id}" data-status="${g.status}">
        <div class="lbl">${goalWord(g)} <span class="muted">· ${g.priority} · started ${agoLabel(g.created_at)}</span></div>
        <div class="gl-obj">${g.objective}</div>
        ${g.blocked_reason ? html`<div class="gl-block"><span class="tag ${goalTone(g.status)}">${g.status === 'PAUSED' ? 'held' : 'reason'}</span> ${g.blocked_reason}</div>` : ''}
        ${p ? html`<div class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${String(p.pct)}" aria-label="Goal progress"><i style="width:${String(p.pct)}%"></i></div>
          <div class="muted gl-pct">${String(p.done)} of ${plural(p.total, 'task')} done (${String(p.pct)}%), ${String(p.verified)} independently verified</div>`
          : html`<div class="muted gl-pct">No tasks have been added yet, so there is no progress to show.</div>`}
        <div class="gl-btns">
          ${paused
            ? html`<button type="button" class="os-btn" data-resume="${g.id}" data-spec="Resumes this goal. It goes back to exactly the status it had when you paused it and the worker picks it up on its next pass.">RESUME</button>`
            : html`<button type="button" class="os-btn" data-pause="${g.id}" data-spec="Halts after the task that is running now completes. No further task starts. The pause is stored, so it survives a restart, and resume restores the exact previous status.">PAUSE</button>`}
          <button type="button" class="os-btn" data-audit="${g.id}" aria-expanded="${String(Boolean(st.audit[g.id]))}" data-spec="Opens this goal's event history: every task, every status change and every verification pass. It only reads.">AUDIT</button>
          <button type="button" class="os-btn os-btn--danger" data-cancel="${g.id}" data-spec="Stops the goal after asking. Finished work is kept and every unfinished task is cancelled.">CANCEL</button>
        </div>
        <div class="lbl gl-sub">Tasks</div>
        ${g.tasks.length ? g.tasks.map((t) => taskRow(g, t)) : html`<div class="muted">This goal has no tasks.</div>`}
        ${g.tasks.length >= 80 ? html`<div class="muted">Showing the first 80 tasks.</div>` : ''}
        ${st.audit[g.id] ? auditBlock(g.id) : ''}
      </div>`;
    }

    function taskRow(g, t) {
      const tag = taskTag(t.status);
      return html`<div class="os-row gl-task" data-task="${t.id}" data-status="${t.status}">
        <span class="tag ${tag.tone}">${tag.word}</span>
        <span class="rt">${t.description}${t.assigned_agent ? html` <span class="muted">· ${t.assigned_agent}</span>` : ''}${t.status === 'COMPLETED' && t.verified === false ? html` <span class="muted">· not independently verified</span>` : ''}${t.last_error && t.status !== 'COMPLETED' ? html`<div class="muted gl-err">${t.last_error}</div>` : ''}${t.status === 'WAITING_FOR_APPROVAL' && t.pending_prompts.length ? html`<div class="muted">Waiting on: ${t.pending_prompts.join(' | ')}</div>` : ''}</span>
        ${t.status === 'WAITING_FOR_APPROVAL' ? html`<span class="os-row-actions">
          <button type="button" class="os-btn" data-approve="${t.id}" data-spec="Approve this task after a confirmation. It runs once with exactly the actions it is parked on.">APPROVE</button>
          <button type="button" class="os-btn os-btn--danger" data-decline="${t.id}" data-spec="Decline this task after a confirmation. It is marked failed and the goal becomes blocked.">DECLINE</button></span>` : ''}
      </div>`;
    }

    function finishedRow(g) {
      const p = progress(g);
      return html`<div class="os-row" data-goal="${g.id}" data-status="${g.status}">
        <span class="tag ${goalTone(g.status)}">${goalWord(g).toLowerCase()}</span>
        <span class="rt">${g.objective} <span class="muted">${p ? '· ' + p.done + ' of ' + p.total + ' tasks' : ''} · ${agoLabel(g.updated_at)}</span></span>
        <span class="os-row-actions"><button type="button" class="os-btn" data-audit="${g.id}" aria-expanded="${String(Boolean(st.audit[g.id]))}" data-spec="Opens this goal's event history. It only reads.">AUDIT</button></span>
      </div>${st.audit[g.id] ? auditBlock(g.id) : ''}`;
    }

    function auditBlock(id) {
      const a = st.audit[id];
      if (a.busy) return html`<div class="gl-audit"><div class="muted">Reading the event history</div></div>`;
      if (a.error) return html`<div class="gl-audit" role="alert"><div class="muted gl-err">Could not read the history: ${a.error.message}${a.error.status ? ' (HTTP ' + a.error.status + ')' : ''}</div></div>`;
      const ev = a.events;
      if (!ev.length) return html`<div class="gl-audit"><div class="muted">No recorded activity for this goal yet.</div></div>`;
      return html`<div class="gl-audit"><div class="lbl">Event history</div>${ev.slice(-AUDIT_SHOWN).map((e) => html`<div class="gl-ev"><span class="muted mono">${agoLabel(e.at)}</span> ${describeAudit(e)}</div>`)}${ev.length > AUDIT_SHOWN ? html`<div class="muted">Showing the latest ${String(AUDIT_SHOWN)} of ${String(ev.length)}.</div>` : ''}</div>`;
    }

    /* ---------------- new goal form ---------------- */
    function openForm() {
      st.form = st.form || { objective: '', steps: '', criteria: '', priority: 'normal' };
      formCard.hidden = false;
      paintForm();
      const first = formEl.querySelector('#glObjective');
      if (first) first.focus();
    }

    function closeForm() {
      formCard.hidden = true;
      st.formError = '';
      formEl.replaceChildren();
    }

    function paintForm() {
      const f = st.form;
      formEl.dataset.state = 'populated';
      setHtml(formEl, html`
        <label class="gl-l" for="glObjective">Objective</label>
        <input id="glObjective" class="gl-in" type="text" maxlength="${String(LIMITS.objective)}" value="${f.objective}" placeholder="What should be true when this is finished" data-spec="What the goal must achieve. Up to ${String(LIMITS.objective)} characters.">
        <label class="gl-l" for="glSteps">Steps, one per line (optional)</label>
        <textarea id="glSteps" class="gl-in" rows="4" data-spec="The tasks, in the order they run. Each starts after the one before it finishes. With none written, the objective itself becomes the only task; nothing plans steps for you.">${f.steps}</textarea>
        <label class="gl-l" for="glCriteria">Success criteria, one per line (optional)</label>
        <textarea id="glCriteria" class="gl-in" rows="2" data-spec="Checked by a second reasoning pass when every task is done. If a criterion is not met the goal is marked blocked instead of completed.">${f.criteria}</textarea>
        <label class="gl-l" for="glPriority">Priority</label>
        <select id="glPriority" class="gl-in" data-spec="Higher priority goals are advanced first.">${['critical', 'high', 'normal', 'low', 'background'].map((p) => html`<option value="${p}" ${p === f.priority ? 'selected' : ''}>${p}</option>`)}</select>
        <div class="muted gl-warn">This goal runs on its own, using the same tools as chat. A tool that needs your confirmation parks its task and waits for your approval here. You will be asked to confirm before it starts.</div>
        ${st.formError ? html`<div class="gl-err" role="alert">${st.formError}</div>` : ''}
        <div class="gl-btns"><button type="button" class="os-btn os-btn--primary" data-create ${st.busy ? 'disabled' : ''} data-spec="Asks for confirmation, then creates the goal and starts it.">CREATE GOAL</button>
          <button type="button" class="os-btn" data-form-close data-spec="Closes this form without creating anything.">DISCARD</button></div>`);
    }

    function readForm() {
      st.form = {
        objective: formEl.querySelector('#glObjective').value,
        steps: formEl.querySelector('#glSteps').value,
        criteria: formEl.querySelector('#glCriteria').value,
        priority: formEl.querySelector('#glPriority').value,
      };
    }

    function submitForm() {
      readForm();
      const bad = validateForm(st.form);
      st.formError = bad;
      paintForm();
      if (bad) return;
      const body = createBody(st.form);
      confirmHere(
        'Create this goal and start it now? "' + body.objective + '". ' + (body.steps.length ? plural(body.steps.length, 'step') + ' in the order written.' : 'No steps written, so the objective is its own single task.') +
          ' It runs on its own in the background; a tool that needs confirmation waits for your approval.',
        async () => {
          await ctx.api.post('/api/os/goals/create', body);
          st.form = null;
          closeForm();
          note('Goal created and started.', 'ok');
          await load('manual');
        },
      );
    }

    /* ---------------- actions ---------------- */
    /* throws on failure: inside a confirm card the card shows the server's words */
    async function act(path, body, okText) {
      await ctx.api.post(path, body);
      note(okText, 'ok');
      if (!ctx.signal.aborted) await load('manual');
    }

    async function actDirect(path, body, okText) {
      try {
        await act(path, body, okText);
      } catch (err) {
        if (!isAbort(err)) note(err.message, 'error');
      }
    }

    async function toggleAudit(id) {
      if (st.audit[id]) {
        delete st.audit[id];
        st.sig = '';
        paint();
        return;
      }
      st.audit[id] = { busy: true, events: [], error: null };
      paint();
      try {
        const r = await ctx.api.get('/api/audit?goal_id=' + encodeURIComponent(id) + '&limit=300');
        if (ctx.signal.aborted) return;
        st.audit[id] = { busy: false, events: Array.isArray(r.events) ? r.events : [], error: null };
      } catch (err) {
        if (isAbort(err)) return;
        st.audit[id] = { busy: false, events: [], error: err };
      }
      paint();
    }

    const findGoal = (id) => (st.board ? st.board.goals.find((g) => g.id === id) : null);
    const findTask = (id) => {
      for (const g of st.board ? st.board.goals : []) {
        const t = g.tasks.find((x) => x.id === id);
        if (t) return t;
      }
      return null;
    };

    root.addEventListener('click', (e) => {
      const q = (sel) => e.target.closest(sel);
      let el;
      if ((el = q('[data-pause]'))) {
        const g = findGoal(el.dataset.pause);
        confirmHere('Pause this goal? "' + (g ? g.objective : '') + '". The task running now finishes, then nothing more starts until you resume. The pause is stored and survives a restart.',
          () => act('/api/os/goals/pause', { id: el.dataset.pause }, 'Goal paused.'));
      } else if ((el = q('[data-resume]'))) {
        actDirect('/api/os/goals/resume', { id: el.dataset.resume }, 'Goal resumed.');
      } else if ((el = q('[data-cancel]'))) {
        const g = findGoal(el.dataset.cancel);
        if (g) confirmHere(cancelPrompt(g), () => act('/api/goals/cancel', { id: g.id }, 'Goal cancelled. Finished work is kept.'));
      } else if ((el = q('[data-audit]'))) {
        toggleAudit(el.dataset.audit);
      } else if ((el = q('[data-approve]')) || (el = q('[data-decline]'))) {
        const approve = Boolean(el.dataset.approve);
        const t = findTask(el.dataset.approve || el.dataset.decline);
        if (t) confirmHere(approvePrompt(t, approve), () => act('/api/goals/tasks/approve', { task_id: t.id, approved: approve, reason: approve ? '' : 'declined in the GOALS screen' }, approve ? 'Task approved.' : 'Task declined.'));
      } else if (q('[data-create]')) {
        submitForm();
      } else if (q('[data-form-close]')) {
        closeForm();
      }
    });
    root.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !formCard.hidden) {
        readForm();
        closeForm();
      }
    });

    ctx.events.onResync(() => load('resync'));
    ctx.every(POLL_MS, () => load('poll'));
    states.loading(listEl, 'Reading the goals');
    await load('show');
  },

  async refresh(ctx, reason) {
    if (reason === 'manual') ctx.notify({ level: 'info', title: 'GOALS', detail: 'This screen re-reads every few seconds while it is open.', ttl: 3000 });
  },
};
