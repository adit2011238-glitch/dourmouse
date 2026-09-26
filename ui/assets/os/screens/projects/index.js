/* PROJECTS: the folders Dourmouse knows about, read from the Claude Code and
   Codex session history on this Mac plus the projects created here. Every
   figure is a real read of /api/os/projects/list (the persisted bookkeeper;
   it never rescans on its own). Nothing polls: REFRESH rescans on request.

   OPEN gives a project its own chat thread on the server: it stores the
   project's tab id as this tab's scope, so every chat and confirmation from
   any screen carries it (HOME shows a bar with LEAVE PROJECT). It is not an
   isolated environment and the screen says so.

   Changes are the owner's click plus a card that says what will happen:
   NEW PROJECT creates a folder, STOP TRACKING removes a row. Neither can
   delete anything that already exists. */

import { html, setHtml } from '../../kit/html.js';
import { states } from '../../kit/states.js';
import { confirmHere } from '../../kit/confirm-card.js';
import { agoLabel } from '../../kit/format.js';
import { isAbort } from '../../core/api.js';
import { createScope } from '../../core/scope.js';
import {
  ACTIVE_CAP, ARCHIVED_CAP, NAME_MAX, DESCRIPTION_MAX, splitProjects, countsLine, sessionCount, sessionsWord,
  originLine, excerpt, scopeFrom, isActiveScope, stopPrompt, createPrompt, validateName, sourceStatusLines,
} from './helpers.js';

export default {
  id: 'PROJECTS',
  sub: 'project scope',
  css: true,
  thread: false,

  async mount(root, ctx) {
    const st = { view: null, staleMsg: '', refreshing: false, formOpen: false, shown: [] };
    let lastError = null;
    /* ctx.scope has no setter. This writes the same tab-scope record the shell
       reads (it is read from storage on every call), so chat and confirm carry
       the project from the next request on. A change to ctx.scope is requested
       in the report. */
    const scopeWriter = createScope();

    root.dataset.state = 'loading';
    setHtml(root, html`
      <div class="prj-note" id="prjNote" role="status" hidden></div>
      <div id="prjConfirm"></div>
      <div id="prjForm"></div>
      <div class="prj-scope card" id="prjScope" hidden></div>
      <div id="prjActive" data-region></div>
      <div class="card prj-archived" id="prjArchivedCard" hidden><div class="lbl">Archived: no activity for 14 days</div><div id="prjArchived" data-region></div></div>
      <div class="card prj-honest"><div class="lbl">Honest state</div><div id="prjHonest" data-region></div></div>`);
    const $ = (id) => root.querySelector('#' + id);
    const noteEl = $('prjNote');
    const confirmEl = $('prjConfirm');
    const formEl = $('prjForm');
    const scopeEl = $('prjScope');
    const activeEl = $('prjActive');
    const archCard = $('prjArchivedCard');
    const archEl = $('prjArchived');
    const honestEl = $('prjHonest');

    const note = (text, tone) => {
      noteEl.hidden = !text;
      noteEl.textContent = text || '';
      noteEl.dataset.tone = tone || '';
    };

    /* ---------------- stage bar ---------------- */
    function paintActions() {
      ctx.chrome.setActions([
        {
          id: 'new', label: 'NEW PROJECT', kind: 'primary', pressed: st.formOpen,
          spec: 'Opens a short form. After you approve a card that names the folder, it creates a new folder under Documents/Dourmouse Projects and adds it to this list. It never changes a folder that already exists.',
          onClick: () => toggleForm(),
        },
        {
          id: 'refresh', label: st.refreshing ? 'READING' : 'REFRESH', disabled: st.refreshing,
          spec: 'Rescans the Claude Code and Codex session history on this Mac for new and changed projects. It only reads those files and updates this list.',
          onClick: () => refreshHistory(),
        },
      ]);
    }

    /* ---------------- reads ---------------- */
    async function load() {
      try {
        const data = await ctx.api.get('/api/os/projects/list');
        if (ctx.signal.aborted) return;
        st.view = data;
        st.staleMsg = ctx.api.isStale(data) ? 'Showing the last copy this window kept. The server did not answer.' : '';
        lastError = null;
      } catch (err) {
        if (isAbort(err)) return;
        lastError = err;
      }
      paint();
    }

    /* ---------------- painting ---------------- */
    function paint() {
      if (!st.view) {
        if (!lastError) return;
        root.dataset.state = lastError.offline ? 'unavailable' : 'error';
        states.error(activeEl, lastError, { title: 'Could not read the project list', retry: () => reload() });
        archCard.hidden = true;
        honestEl.replaceChildren();
        honestEl.dataset.state = 'error';
        paintScope();
        return;
      }
      paintScope();
      paintActive();
      paintArchived();
      paintHonest();
      if (lastError) {
        states.stale(root, 'Could not refresh: ' + lastError.message + ' The list below is from the last read.');
      } else if (st.staleMsg) {
        states.stale(root, st.staleMsg);
      } else {
        root.querySelectorAll(':scope > .st-stale').forEach((n) => n.remove());
        root.dataset.state = activeEl.dataset.state === 'unavailable' ? 'unavailable' : (st.view.projects.length ? 'populated' : 'empty');
      }
    }

    function paintScope() {
      const p = ctx.scope.project();
      if (!p) {
        scopeEl.hidden = true;
        scopeEl.replaceChildren();
        return;
      }
      scopeEl.hidden = false;
      setHtml(scopeEl, html`<div class="prj-scope-row"><span class="muted">Chats and confirmations from every screen are scoped to</span> <b>${p.name || p.tab_id}</b>
        <button type="button" class="os-btn" data-leave data-spec="Returns this tab to its own general conversation. The project keeps its own thread on the server.">LEAVE PROJECT</button>
        <a class="os-btn" href="#/home" data-spec="Opens HOME, where you talk to Dourmouse inside this project.">OPEN HOME</a></div>`);
    }

    function card(p, i) {
      const active = isActiveScope(p, ctx.scope.project());
      const counts = countsLine(p);
      const origin = originLine(p);
      const when = p.last_active ? agoLabel(p.last_active) : '';
      const ctxText = excerpt(p.context);
      return html`<div class="card prj-card" data-active="${String(active)}">
        <div class="lbl prj-name"><span class="prj-nm">${p.name}</span>${active ? html` <span class="tag ok">scope</span>` : ''}${p.exists === false ? html` <span class="tag bad">path gone</span>` : ''}</div>
        <div class="big">${sessionCount(p)}</div>
        <div class="muted">${sessionsWord(p)}${counts ? ' · ' + counts : ''}${when ? ' · last active ' + when : ''}</div>
        <div class="muted mono prj-path">${p.path}</div>
        ${p.git_branch ? html`<div class="muted">branch <span class="mono">${p.git_branch}</span></div>` : ''}
        ${origin ? html`<div class="muted">from ${origin}</div>` : ''}
        ${ctxText ? html`<div class="prj-ctx muted">${ctxText}</div>` : ''}
        <div class="prj-btns">
          ${active
    ? html`<button type="button" class="os-btn" data-leave data-spec="Returns this tab to its own general conversation. The project keeps its own thread on the server.">LEAVE</button>`
    : html`<button type="button" class="os-btn os-btn--primary" data-open="${i}" ${p.exists === false ? 'disabled' : ''} data-spec="${p.exists === false ? 'The folder is gone from disk, so this project cannot be opened.' : 'Gives this project its own chat thread. Every chat and confirmation from any screen then carries the project until you leave it.'}">OPEN</button>`}
          <button type="button" class="os-btn os-btn--danger" data-stop="${i}" data-spec="Stops tracking this project after asking. The folder and the session history are never touched.">STOP TRACKING</button>
        </div></div>`;
    }

    function paintActive() {
      const view = st.view;
      const { active, archived, total } = splitProjects(view.projects);
      const shown = active.slice(0, ACTIVE_CAP);
      st.shown = [...shown, ...archived.slice(0, ARCHIVED_CAP)];
      if (!total) {
        const src = sourceStatusLines(view);
        if (!src.some((s) => s.found)) {
          states.unavailable(activeEl, 'No project history was found to read.', {
            detail: 'Looked for Claude Code history at ' + (view.claude_code && view.claude_code.root ? view.claude_code.root : 'its usual folder') +
              ' and Codex history at ' + (view.codex_cli && view.codex_cli.db ? view.codex_cli.db : 'its usual database') + '. You can still create a project here.',
            retry: () => refreshHistory(),
          });
        } else {
          states.empty(activeEl, 'No projects yet.', { hint: 'The history was found but holds no sessions. Create a project, or use one of those tools and press REFRESH.' });
        }
        return;
      }
      if (!shown.length) {
        states.empty(activeEl, 'Nothing active in the last 14 days.', { hint: 'Older projects are listed below.' });
        return;
      }
      activeEl.dataset.state = 'populated';
      setHtml(activeEl, html`<div class="grid3 prj-grid">${shown.map((p, i) => card(p, i))}</div>
        ${active.length > shown.length ? html`<div class="muted prj-cap">Showing the newest ${shown.length} of ${active.length} active projects.</div>` : ''}`);
    }

    function paintArchived() {
      const { active, archived } = splitProjects(st.view.projects);
      if (!archived.length) {
        archCard.hidden = true;
        return;
      }
      archCard.hidden = false;
      const shown = archived.slice(0, ARCHIVED_CAP);
      const offset = Math.min(active.length, ACTIVE_CAP);
      archEl.dataset.state = 'populated';
      setHtml(archEl, html`${shown.map((p, i) => html`<div class="os-row prj-arow">
        <span class="rt"><b>${p.name}</b> <span class="muted">${sessionCount(p)} ${sessionsWord(p)}${p.last_active ? ' · last active ' + agoLabel(p.last_active) : ' · never active'}</span>${p.exists === false ? html` <span class="tag bad">path gone</span>` : ''}</span>
        <span class="os-row-actions">
          <button type="button" class="os-btn" data-open="${offset + i}" ${p.exists === false ? 'disabled' : ''} data-spec="${p.exists === false ? 'The folder is gone from disk, so this project cannot be opened.' : 'Gives this project its own chat thread until you leave it.'}">OPEN</button>
          <button type="button" class="os-btn os-btn--danger" data-stop="${offset + i}" data-spec="Stops tracking this project after asking. The folder and the session history are never touched.">STOP</button>
        </span></div>`)}
        ${archived.length > shown.length ? html`<div class="muted prj-cap">Showing the newest ${shown.length} of ${archived.length}.</div>` : ''}`);
    }

    function paintHonest() {
      const v = st.view;
      const src = sourceStatusLines(v);
      honestEl.dataset.state = 'populated';
      setHtml(honestEl, html`
        <div class="muted">This is a summary of the Claude Code and Codex history on this Mac, plus the projects you create here. OPEN gives a project its own chat thread and scopes every chat and confirmation to it. It is not an isolated environment: research, coding and security tools are not sandboxed per project. That is item OS-6 and is not built.</div>
        <div class="prj-src">${src.map((s) => html`<div class="kv"><span>${s.label}</span><b><span class="tag ${s.found ? 'ok' : 'warn'}">${s.found ? 'found' : 'not found'}</span></b></div>${s.where ? html`<div class="muted mono prj-where">${s.where}</div>` : ''}`)}
        <div class="kv"><span>Last refreshed</span><b>${v.last_refreshed ? agoLabel(v.last_refreshed) : 'never'}</b></div></div>`);
    }

    /* ---------------- actions ---------------- */
    async function reload() {
      states.loading(activeEl, 'Reading the project list');
      await load();
    }

    async function refreshHistory() {
      st.refreshing = true;
      paintActions();
      note('Reading the Claude Code and Codex history. This can take a few seconds.', 'info');
      try {
        await ctx.api.post('/api/projects/bookkeeper/refresh', {});
        note('History read.', 'ok');
        await load();
      } catch (err) {
        if (!isAbort(err)) note('Could not read the history: ' + err.message, 'error');
      } finally {
        st.refreshing = false;
        if (!ctx.signal.aborted) paintActions();
      }
    }

    async function openProject(p) {
      note('Opening ' + p.name + '.', 'info');
      try {
        const r = await ctx.api.post('/api/projects/open', { path: p.path });
        if (ctx.signal.aborted) return;
        scopeWriter.setProject(scopeFrom(p, r.project));
        note('Scoped to ' + p.name + '. Chats and confirmations now carry this project. Open HOME to talk inside it.', 'ok');
        paintScope();
        paintActive();
        paintArchived();
      } catch (err) {
        if (!isAbort(err)) note('Could not open ' + p.name + ': ' + err.message, 'error');
      }
    }

    function stopTracking(p) {
      confirmHere(confirmEl, stopPrompt(p), async () => {
        await ctx.api.post('/api/projects/delete', { path: p.path });
        if (isActiveScope(p, ctx.scope.project())) ctx.scope.leaveProject();
        note('Stopped tracking ' + p.name + '. Its folder was not touched.', 'ok');
        await load();
      });
    }

    /* ---------------- new project form ---------------- */
    let offEsc = null;
    function toggleForm(force) {
      st.formOpen = typeof force === 'boolean' ? force : !st.formOpen;
      paintActions();
      if (offEsc) {
        offEsc();
        offEsc = null;
      }
      if (!st.formOpen) {
        formEl.replaceChildren();
        return;
      }
      offEsc = ctx.keys.pushEsc(() => toggleForm(false));
      setHtml(formEl, html`<form class="card prj-form" novalidate>
        <div class="lbl">New project</div>
        <label class="prj-f"><span class="muted">Name</span><input type="text" name="name" maxlength="${NAME_MAX}" autocomplete="off" data-spec="The project's name. It also names the folder that will be created."></label>
        <label class="prj-f"><span class="muted">What it is for (optional)</span><textarea name="description" rows="3" maxlength="${DESCRIPTION_MAX}" data-spec="Kept as the project's context, shown on its card. It is never sent anywhere by this form."></textarea></label>
        <div class="prj-msg" role="alert" data-msg></div>
        <div class="prj-btns"><button type="submit" class="os-btn os-btn--primary" data-spec="Shows the exact folder it will create and asks you to approve. Nothing is created until you approve.">CREATE</button>
        <button type="button" class="os-btn" data-cancel data-spec="Closes this form without creating anything.">CANCEL</button></div></form>`);
      formEl.querySelector('input[name="name"]').focus();
    }

    async function submitForm(form) {
      const msg = form.querySelector('[data-msg]');
      msg.textContent = '';
      const v = validateName(form.elements.name.value);
      if (!v.ok) {
        msg.textContent = v.message;
        return;
      }
      const description = form.elements.description.value.trim();
      let plan;
      try {
        plan = await ctx.api.get('/api/os/projects/plan?name=' + encodeURIComponent(v.name));
      } catch (err) {
        if (!isAbort(err)) msg.textContent = err.message;
        return;
      }
      confirmHere(confirmEl, createPrompt(plan, description), async () => {
        const r = await ctx.api.post('/api/os/projects/create', { name: v.name, description });
        toggleForm(false);
        note('Created ' + r.project.name + ' at ' + r.project.path + '.', 'ok');
        await load();
      });
    }

    /* ---------------- events on my own elements ---------------- */
    root.addEventListener('click', (e) => {
      if (e.target.closest('[data-leave]')) {
        ctx.scope.leaveProject();
        note('Left the project. This tab is back to its own conversation.', 'ok');
        paintScope();
        if (st.view) {
          paintActive();
          paintArchived();
        }
        return;
      }
      const open = e.target.closest('[data-open]');
      if (open) {
        const p = st.shown[Number(open.dataset.open)];
        if (p) openProject(p);
        return;
      }
      const stop = e.target.closest('[data-stop]');
      if (stop) {
        const p = st.shown[Number(stop.dataset.stop)];
        if (p) stopTracking(p);
        return;
      }
      if (e.target.closest('[data-cancel]')) toggleForm(false);
    });
    root.addEventListener('submit', (e) => {
      e.preventDefault();
      if (e.target.closest('.prj-form')) submitForm(e.target.closest('.prj-form'));
    });

    ctx.scope.onChange(() => {
      paintScope();
      if (st.view) {
        paintActive();
        paintArchived();
      }
    });

    paintActions();
    states.loading(activeEl, 'Reading the project list');
    await load();
  },

  async refresh(ctx, reason) {
    if (reason === 'manual') ctx.notify({ level: 'info', title: 'PROJECTS', detail: 'Use REFRESH to read the Claude Code and Codex history again.', ttl: 3000 });
  },
};
