/* PROJECTS: three views in one screen, told apart by the address.
     #/projects              the list, with covers
     #/projects/<id>         one project: threads, instructions, work, files, what it knows, agents, outputs, activity
     #/projects/<id>/<thread> one thread, with the composer

   Everything is read from and written to /api/os/project/* (the store from plan 1.4). A project works the way a
   Claude project does: its instructions and files are used by every thread in it, and only this project's own
   store is searched. A quick ask from HOME is never stored here.

   A thread talks on the tab "pj:<project>:<thread>". While a thread is open this screen makes that the scope, so
   the chat, the approvals and Private mode all carry the same tab, and it leaves the scope again when the thread
   is closed. A finished answer is saved into the thread (POST thread/turn) by a listener that outlives this
   screen, so an answer that lands after you moved on is still kept. */

import { html, setHtml } from '../../kit/html.js';
import { states } from '../../kit/states.js';
import { confirmHere } from '../../kit/confirm-card.js';
import { mountThreadView } from '../../kit/thread-view.js';
import { agoLabel, whenShort } from '../../kit/format.js';
import { md } from '../../kit/md.js';
import { isAbort } from '../../core/api.js';
import {
  NAME_MAX, GOAL_MAX, INSTRUCTIONS_MAX, NOTE_MAX, parseHash, hashFor, projectTab, isProjectTab, coverVar, metaLine, filterProjects,
  agentLabel, sizeLabel, validateName, validateText, isTextFile, fileProblem, workState, workToggle, workLine, turnRecords, answered,
  threadLine, plural,
} from './helpers.js';

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function button(label, cls, spec) {
  const b = el('button', cls || 'os-btn', label);
  b.type = 'button';
  if (spec) b.dataset.spec = spec;
  return b;
}

/* ---------------- saving finished answers (outlives the screen) ---------------- */

/* One listener on the PROJECTS thread while a project thread is open, or while its last run is still going after
   the thread was closed. It saves each finished answer once, then removes itself. */
const saver = { off: null, raw: null, target: null, closed: true, saved: new WeakSet(), onIdle: null, notify: null, api: null, onSaved: null };

function saverDetach(runIdle) {
  if (saver.off) saver.off();
  const idle = saver.onIdle;
  Object.assign(saver, { off: null, raw: null, target: null, closed: true, onIdle: null, onSaved: null });
  if (runIdle && idle) idle();
}

async function saveTurn(turn, target) {
  saver.saved.add(turn);
  try {
    await saver.api.post('/api/os/project/thread/turn', { id: target.pid, thread: target.tid, user: turn.text, assistant: turn.reply });
    if (saver.onSaved) saver.onSaved(target);
  } catch (err) {
    if (saver.notify) saver.notify({ level: 'error', title: 'This answer was not saved to the thread', detail: err && err.message });
  }
}

function flush() {
  if (!saver.raw || !saver.target) return;
  saver.raw.turns().slice(-4).forEach((t) => {
    if (!t.restored && !saver.saved.has(t) && answered(t)) saveTurn(t, saver.target);
  });
}

/* api is not bound to the screen's own abort signal, so a save that is still due after the screen is left goes through */
function saverAttach(raw, target, notify, api, onIdle) {
  saverDetach(false);
  Object.assign(saver, { raw, target, closed: false, onIdle, notify, api });
  saver.off = raw.subscribe(() => {
    flush();
    if (saver.closed && !raw.busy()) saverDetach(true);
  });
}

function saverClose() {
  saver.closed = true;
  if (!saver.raw || !saver.raw.busy()) {
    flush();
    saverDetach(true);
  }
}

let current = null; /* { reload } of the mounted screen, for refresh() */

export default {
  id: 'PROJECTS',
  sub: 'projects',
  css: true,
  thread: true,

  async mount(root, ctx) {
    const raw = Object.getPrototypeOf(ctx.chat) && typeof Object.getPrototypeOf(ctx.chat).subscribe === 'function' ? Object.getPrototypeOf(ctx.chat) : ctx.chat;
    const keepApi = ctx.api.withSignal(new AbortController().signal);
    const st = { seq: 0, cleanup: [], showArchived: false, query: '', formOpen: false };
    root.dataset.state = 'loading';

    const leaveScopeIfOurs = () => {
      const p = ctx.scope.project();
      if (p && isProjectTab(p.tab_id) && !saver.target) ctx.scope.leaveProject();
    };
    /* a window reloaded mid-thread can come back with a project tab as its scope: drop it unless a run owns it */
    if (!saver.target) leaveScopeIfOurs();

    const goto = (pid, tid) => {
      const h = hashFor(pid, tid);
      if (window.location.hash === h) render();
      else window.location.hash = h;
    };
    const fail = (title, err) => {
      if (!isAbort(err)) ctx.notify({ level: 'error', title, detail: err && err.message });
    };

    function teardown() {
      st.cleanup.splice(0).forEach((fn) => {
        try {
          fn();
        } catch (err) {
          console.error(err);
        }
      });
    }

    /* ================= the list ================= */
    async function showList(my) {
      ctx.chrome.setComposer(null);
      ctx.chrome.setLive(false);
      const wrap = el('div', 'pj');
      const bar = el('div', 'pj-bar');
      const search = el('input', 'os-field pj-search');
      search.type = 'search';
      search.placeholder = 'Search projects';
      search.setAttribute('aria-label', 'Search projects');
      search.value = st.query;
      bar.append(search);
      const formHost = el('div', 'pj-formhost');
      const grid = el('div', 'pj-grid');
      grid.dataset.region = '';
      wrap.append(bar, formHost, grid);
      root.replaceChildren(wrap);
      states.loading(grid, 'Reading projects');
      let list = null;

      const paintActions = () => ctx.chrome.setActions([
        { id: 'new', label: 'New project', kind: 'primary', spec: 'Opens a short form. A project starts empty or from a starter kit and is stored on this Mac.', onClick: () => toggleForm(true) },
        { id: 'archived', label: 'Archived', pressed: st.showArchived, spec: 'Shows projects you archived as well. Nothing is deleted by archiving.', onClick: () => { st.showArchived = !st.showArchived; paintActions(); load(); } },
      ]);

      function paintGrid() {
        if (!list) return;
        const shown = filterProjects(list, st.query);
        if (!shown.length) {
          const none = !list.length;
          states.empty(grid, none ? 'No projects yet.' : 'No project matches.', none
            ? { hint: 'A project keeps instructions, files and threads together.', action: { label: 'New project', onClick: () => toggleForm(true) } }
            : { hint: 'Try fewer words.' });
          return;
        }
        grid.dataset.state = 'populated';
        grid.replaceChildren(...shown.map((p) => {
          const card = el('button', 'pj-card');
          card.type = 'button';
          card.dataset.project = p.id;
          card.dataset.spec = 'Opens this project: its threads, instructions, files and what it knows.';
          const cover = el('span', 'pj-cover');
          cover.style.background = coverVar(p);
          const body = el('span', 'pj-card-body');
          const name = el('span', 'pj-name', p.name);
          if (p.pinned) name.append(el('span', 'pj-flag', 'Pinned'));
          if (p.archived) name.append(el('span', 'pj-flag', 'Archived'));
          body.append(name);
          if (p.goal) body.append(el('span', 'pj-goal', p.goal));
          const meta = [metaLine(p), p.last_active ? agoLabel(p.last_active) : ''].filter(Boolean).join(', ');
          body.append(el('span', 'pj-meta', meta || 'No activity yet'));
          card.append(cover, body);
          card.addEventListener('click', () => goto(p.id));
          return card;
        }));
      }

      async function load() {
        try {
          const d = await ctx.api.get('/api/os/project/list?archived=' + (st.showArchived ? '1' : '0'));
          if (my !== st.seq) return;
          list = d.projects || [];
          root.dataset.state = list.length ? 'populated' : 'empty';
          paintGrid();
        } catch (err) {
          if (isAbort(err) || my !== st.seq) return;
          root.dataset.state = err.offline ? 'unavailable' : 'error';
          states.error(grid, err, { title: 'Could not read the projects', retry: () => load() });
        }
      }

      async function toggleForm(on) {
        st.formOpen = on;
        if (!on) {
          formHost.replaceChildren();
          return;
        }
        let kits = [{ key: 'blank', label: 'Blank' }];
        try {
          const t = await ctx.api.get('/api/os/project/templates');
          if (Array.isArray(t.templates) && t.templates.length) kits = t.templates;
        } catch (err) {
          fail('Could not read the starter kits', err);
        }
        if (my !== st.seq) return;
        setHtml(formHost, html`<form class="pj-form" novalidate>
          <label class="pj-f"><span>Name</span><input class="os-field" name="name" maxlength="${NAME_MAX}" autocomplete="off" data-spec="The project's name."></label>
          <label class="pj-f"><span>Starter</span><select class="os-field" name="template" data-spec="A starter kit fills in a goal, instructions, agents and first work items. You can change all of them afterwards.">${kits.map((k) => html`<option value="${k.key}">${k.label}</option>`)}</select></label>
          <div class="pj-msg" role="alert" data-msg></div>
          <div class="pj-row-actions"><button type="submit" class="os-btn os-btn--primary" data-spec="Creates the project and opens it.">Create</button><button type="button" class="os-btn" data-cancel data-spec="Closes the form without creating anything.">Cancel</button></div></form>`);
        const form = formHost.querySelector('form');
        form.elements.name.focus();
        form.querySelector('[data-cancel]').addEventListener('click', () => toggleForm(false));
        form.addEventListener('submit', async (e) => {
          e.preventDefault();
          const msg = form.querySelector('[data-msg]');
          const v = validateName(form.elements.name.value);
          if (!v.ok) {
            msg.textContent = v.message;
            return;
          }
          msg.textContent = '';
          try {
            const d = await ctx.api.post('/api/os/project/create', { name: v.name, template: form.elements.template.value });
            st.formOpen = false;
            goto(d.project.id);
          } catch (err) {
            if (!isAbort(err)) msg.textContent = err.message;
          }
        });
      }

      search.addEventListener('input', () => {
        st.query = search.value;
        paintGrid();
      });
      paintActions();
      current = { reload: () => load() };
      await load();
      if (st.formOpen && my === st.seq) toggleForm(true);
    }

    /* ================= one project ================= */
    async function showProject(my, pid) {
      ctx.chrome.setComposer(null);
      ctx.chrome.setLive(false);
      const wrap = el('div', 'pj pj-page');
      wrap.dataset.region = '';
      root.replaceChildren(wrap);
      states.loading(wrap, 'Reading the project');
      let p = null;
      const confirmHost = el('div', 'pj-confirm');

      async function fetchProject() {
        const d = await ctx.api.get('/api/os/project/get?id=' + encodeURIComponent(pid));
        p = d.project;
      }
      async function mutate(path, body, ...parts) {
        try {
          await ctx.api.post('/api/os/project/' + path, { id: pid, ...body });
          await fetchProject();
          if (my !== st.seq) return true;
          (parts.length ? parts : ['head']).forEach((n) => paint[n]());
          paintActions();
          return true;
        } catch (err) {
          fail('That did not work', err);
          return false;
        }
      }

      const sec = (title, action) => {
        const s = el('section', 'pj-sec');
        const head = el('div', 'pj-sec-head');
        head.append(el('h2', 'pj-h2', title));
        if (action) head.append(action);
        const body = el('div', 'pj-sec-body');
        s.append(head, body);
        return { s, body, head };
      };

      /* ----- sections (each paints only itself) ----- */
      const paint = {};
      const refs = {};

      refs.head = el('header', 'pj-head');
      paint.head = () => {
        const cover = el('span', 'pj-cover pj-cover--big');
        cover.style.background = coverVar(p);
        const text = el('div', 'pj-head-t');
        const name = el('h1', 'pj-title', p.name);
        const goal = el('p', p.goal ? 'pj-goalline' : 'pj-goalline pj-muted', p.goal || 'No goal line yet.');
        const rename = button('Rename', 'pj-link', 'Changes the project name.');
        const regoal = button('Edit goal', 'pj-link', 'Changes the one line under the name.');
        const links = el('div', 'pj-links');
        links.append(rename, regoal);
        text.append(name, goal, links);
        const swatches = el('div', 'pj-swatches');
        swatches.setAttribute('role', 'group');
        swatches.setAttribute('aria-label', 'Cover colour');
        ['1', '2', '3', '4', '5', '6'].forEach((c) => {
          const b = el('button', 'pj-swatch');
          b.type = 'button';
          b.style.background = 'var(--tk-cover-' + c + ')';
          b.setAttribute('aria-label', 'Cover ' + c);
          b.setAttribute('aria-pressed', String(coverVar(p) === 'var(--tk-cover-' + c + ')'));
          b.dataset.spec = 'Sets this project\'s cover colour.';
          b.addEventListener('click', () => mutate('update', { cover: c }, 'head'));
          swatches.append(b);
        });
        refs.head.replaceChildren(cover, text, swatches);
        const edit = (node, value, max, field, label) => {
          const inp = el('input', 'os-field pj-inline');
          inp.value = value;
          inp.maxLength = max;
          inp.setAttribute('aria-label', label);
          node.replaceWith(inp);
          inp.focus();
          inp.select();
          let done = false;
          const finish = async (save) => {
            if (done) return;
            done = true;
            if (!save || inp.value.trim() === value) return paint.head();
            if (field === 'name' && !validateName(inp.value).ok) {
              ctx.notify({ level: 'warn', title: validateName(inp.value).message });
              return paint.head();
            }
            await mutate('update', { [field]: inp.value.trim() }, 'head');
          };
          inp.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') finish(true);
            else if (e.key === 'Escape') finish(false);
          });
          inp.addEventListener('blur', () => finish(true));
        };
        rename.addEventListener('click', () => edit(name, p.name, NAME_MAX, 'name', 'Project name'));
        regoal.addEventListener('click', () => edit(goal, p.goal || '', GOAL_MAX, 'goal', 'Goal line'));
      };

      /* threads */
      const threads = sec('Threads');
      paint.threads = () => {
        if (!p.threads.length) {
          threads.body.replaceChildren(el('p', 'pj-muted', 'No threads yet.'));
          return;
        }
        threads.body.replaceChildren(...p.threads.map((t) => {
          const row = el('div', 'pj-item');
          const open = el('button', 'pj-item-main');
          open.type = 'button';
          open.dataset.thread = t.id;
          open.dataset.spec = 'Opens this thread.';
          open.append(el('span', 'pj-item-t', t.title), el('span', 'pj-item-s', threadLine(t, t.updated_at ? agoLabel(t.updated_at) : '')));
          open.addEventListener('click', () => goto(pid, t.id));
          const arch = button('Archive', 'pj-link', 'Hides this thread from the list. Its messages stay stored.');
          arch.addEventListener('click', () => mutate('thread/archive', { thread: t.id }, 'threads'));
          row.append(open, arch);
          return row;
        }));
      };

      /* work */
      const work = sec('Work');
      paint.work = () => {
        const rows = p.work_items.map((w) => {
          const row = el('div', 'pj-item pj-work');
          row.dataset.work = w.id;
          const box = el('input');
          box.type = 'checkbox';
          box.checked = workState(w) === 'done';
          box.setAttribute('aria-label', 'Done: ' + w.title);
          box.dataset.spec = 'Marks this item done, or back to to do.';
          box.addEventListener('change', () => mutate('work/update', { work: w.id, status: workToggle(w) }, 'work'));
          const text = el('div', 'pj-item-main pj-static');
          text.append(el('span', workState(w) === 'done' ? 'pj-item-t pj-done' : 'pj-item-t', w.title));
          const line = workLine(w);
          if (line) text.append(el('span', 'pj-item-s', line));
          row.append(box, text);
          if (workState(w) === 'todo') {
            const run = button('Run', 'pj-link', 'Starts a job for this item, run by its agent under this project\'s instructions. It shows in the Office.');
            run.addEventListener('click', async () => {
              run.disabled = true;
              if (await mutate('work/run', { work: w.id }, 'work')) ctx.notify({ level: 'ok', title: 'Job started', detail: w.title, ttl: 3000 });
              else run.disabled = false;
            });
            row.append(run);
          }
          const rm = button('Remove', 'pj-link', 'Removes this item from the list.');
          rm.addEventListener('click', () => mutate('work/remove', { work: w.id }, 'work'));
          row.append(rm);
          return row;
        });
        const add = el('form', 'pj-add');
        const inp = el('input', 'os-field');
        inp.placeholder = 'Add an item';
        inp.maxLength = 200;
        inp.setAttribute('aria-label', 'New work item');
        const go = button('Add', 'os-btn', 'Adds the item to the list.');
        go.type = 'submit';
        add.append(inp, go);
        add.addEventListener('submit', async (e) => {
          e.preventDefault();
          const v = validateText(inp.value, 'item', 200);
          if (!v.ok) return;
          if (await mutate('work/add', { title: v.text }, 'work')) {
            const next = work.body.querySelector('input.os-field');
            if (next) next.focus();
          }
        });
        work.body.replaceChildren(...(rows.length ? rows : [el('p', 'pj-muted', 'Nothing to do yet.')]), add);
      };

      /* outputs */
      const outputs = sec('Outputs');
      paint.outputs = () => {
        if (!p.outputs.length) {
          outputs.s.hidden = true;
          return;
        }
        outputs.s.hidden = false;
        outputs.body.replaceChildren(...p.outputs.map((o) => {
          const d = el('details', 'pj-out');
          const sum = el('summary', 'pj-out-sum');
          sum.append(el('span', 'pj-item-t', o.title), el('span', 'pj-item-s', [o.kind, whenShort(o.created_at)].filter(Boolean).join(', ')));
          const body = el('div', 'pj-out-body md');
          d.append(sum, body);
          let loaded = false;
          d.addEventListener('toggle', async () => {
            if (!d.open || loaded) return;
            loaded = true;
            try {
              const r = await ctx.api.get('/api/os/project/output?id=' + encodeURIComponent(pid) + '&output=' + encodeURIComponent(o.id));
              setHtml(body, md(r.output.body || '') || html`<span class="pj-muted">Empty.</span>`);
            } catch (err) {
              loaded = false;
              body.textContent = err.message;
            }
          });
          return d;
        }));
      };

      /* activity */
      const activity = sec('Activity');
      paint.activity = () => {
        const d = el('details', 'pj-out');
        d.append(el('summary', 'pj-out-sum', 'Recent changes'));
        const list = el('div', 'pj-tl');
        d.append(list);
        let loaded = false;
        d.addEventListener('toggle', async () => {
          if (!d.open || loaded) return;
          loaded = true;
          try {
            const r = await ctx.api.get('/api/os/project/timeline?id=' + encodeURIComponent(pid));
            list.replaceChildren(...(r.timeline.length ? r.timeline.slice(0, 40).map((e) => {
              const row = el('div', 'pj-tl-row');
              row.append(el('span', 'pj-tl-at', whenShort(e.at)), el('span', '', e.summary));
              return row;
            }) : [el('p', 'pj-muted', 'Nothing yet.')]));
          } catch (err) {
            loaded = false;
            list.textContent = err.message;
          }
        });
        activity.body.replaceChildren(d);
      };

      /* instructions */
      const instr = sec('Instructions');
      paint.instructions = () => {
        const show = () => {
          const t = el('p', p.instructions ? 'pj-instr' : 'pj-muted', p.instructions || 'No instructions. Every thread in this project follows them.');
          const edit = button('Edit', 'pj-link', 'Changes the standing instructions every thread in this project follows.');
          edit.addEventListener('click', editing);
          instr.body.replaceChildren(t, edit);
        };
        const editing = () => {
          const ta = el('textarea', 'os-field pj-ta');
          ta.rows = 8;
          ta.maxLength = INSTRUCTIONS_MAX;
          ta.value = p.instructions;
          ta.setAttribute('aria-label', 'Instructions');
          const msg = el('div', 'pj-msg');
          msg.setAttribute('role', 'alert');
          const save = button('Save', 'os-btn os-btn--primary', 'Saves the instructions. They apply to new threads and the next message in open ones.');
          const cancel = button('Cancel', 'os-btn', 'Discards the change.');
          const acts = el('div', 'pj-row-actions');
          acts.append(save, cancel);
          instr.body.replaceChildren(ta, msg, acts);
          ta.focus();
          cancel.addEventListener('click', show);
          save.addEventListener('click', async () => {
            if (ta.value.length > INSTRUCTIONS_MAX) {
              msg.textContent = 'The limit is ' + INSTRUCTIONS_MAX + ' characters.';
              return;
            }
            if (!(await mutate('update', { instructions: ta.value }, 'instructions'))) msg.textContent = 'Could not save.';
          });
        };
        show();
      };

      /* files */
      const fileInput = el('input');
      fileInput.type = 'file';
      fileInput.multiple = true;
      fileInput.hidden = true;
      const addFile = button('Add file', 'pj-link', 'Copies a file into this project. Text files are searched by the project; the original is not changed.');
      const files = sec('Files', addFile);
      addFile.addEventListener('click', () => fileInput.click());
      fileInput.addEventListener('change', async () => {
        const picked = Array.from(fileInput.files || []);
        fileInput.value = '';
        for (const f of picked) {
          const problem = fileProblem(f);
          if (problem) {
            ctx.notify({ level: 'warn', title: problem });
            continue;
          }
          try {
            let body;
            if (isTextFile(f.name)) body = { name: f.name, text: await f.text() };
            else {
              const bytes = new Uint8Array(await f.arrayBuffer());
              let bin = '';
              for (let i = 0; i < bytes.length; i += 32768) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 32768));
              body = { name: f.name, data_base64: btoa(bin) };
            }
            await ctx.api.post('/api/os/project/file/add', { id: pid, ...body });
          } catch (err) {
            fail('Could not add ' + f.name, err);
          }
        }
        try {
          await fetchProject();
          if (my === st.seq) {
            paint.files();
            paint.memory();
          }
        } catch (err) {
          fail('Could not re-read the project', err);
        }
      });
      paint.files = () => {
        files.body.replaceChildren(...(p.files.length ? p.files.map((f) => {
          const row = el('div', 'pj-item');
          const main = el('div', 'pj-item-main pj-static');
          main.append(el('span', 'pj-item-t', f.name), el('span', 'pj-item-s', [sizeLabel(f.size), f.indexed ? '' : 'Not searched'].filter(Boolean).join(', ')));
          const rm = button('Remove', 'pj-link', 'Removes the project\'s copy of this file. The original is not touched.');
          rm.addEventListener('click', () => mutate('file/remove', { file: f.id }, 'files', 'memory'));
          row.append(main, rm);
          return row;
        }) : [el('p', 'pj-muted', 'No files. Add notes, papers or code the project should know.')]), fileInput);
      };

      /* what this project knows */
      const know = sec('What this project knows');
      paint.memory = () => {
        const parts = [];
        const n = p.passages || 0;
        parts.push(el('p', 'pj-muted', n ? plural(n, 'passage') + ' from instructions, files and memory can be searched.' : 'Nothing is searchable yet.'));
        const sForm = el('form', 'pj-add');
        const sIn = el('input', 'os-field');
        sIn.type = 'search';
        sIn.placeholder = 'Search this project';
        sIn.setAttribute('aria-label', 'Search this project');
        const sGo = button('Search', 'os-btn', 'Searches only this project\'s own instructions, files and memory.');
        sGo.type = 'submit';
        sForm.append(sIn, sGo);
        const hits = el('div', 'pj-hits');
        sForm.addEventListener('submit', async (e) => {
          e.preventDefault();
          const q = sIn.value.trim();
          if (!q) {
            hits.replaceChildren();
            return;
          }
          try {
            const r = await ctx.api.get('/api/os/project/search?id=' + encodeURIComponent(pid) + '&q=' + encodeURIComponent(q));
            hits.replaceChildren(...(r.results.length ? r.results.map((h) => {
              const row = el('div', 'pj-hit');
              row.append(el('div', 'pj-item-s', h.citation), el('div', '', String(h.text).slice(0, 280)));
              return row;
            }) : [el('p', 'pj-muted', 'Nothing found.')]));
          } catch (err) {
            fail('Search failed', err);
          }
        });
        parts.push(sForm, hits);
        p.memory.forEach((m) => {
          const row = el('div', 'pj-item');
          row.append(el('div', 'pj-item-main pj-static pj-note', m.text));
          const forget = button('Forget', 'pj-link', 'Removes this note from what the project remembers.');
          forget.addEventListener('click', () => mutate('memory/forget', { memory: m.id }, 'memory'));
          row.append(forget);
          parts.push(row);
        });
        const add = el('form', 'pj-add');
        const inp = el('input', 'os-field');
        inp.placeholder = 'Remember something about this project';
        inp.setAttribute('aria-label', 'A note for this project to remember');
        const go = button('Remember', 'os-btn', 'Keeps this note in this project only. Other projects never see it.');
        go.type = 'submit';
        add.append(inp, go);
        add.addEventListener('submit', async (e) => {
          e.preventDefault();
          const v = validateText(inp.value, 'note', NOTE_MAX);
          if (!v.ok) return;
          await mutate('memory/add', { text: v.text }, 'memory');
        });
        parts.push(add);
        know.body.replaceChildren(...parts);
      };

      /* agents */
      const agents = sec('Agents');
      paint.agents = () => {
        agents.body.replaceChildren(p.agents.length
          ? el('p', 'pj-agents', p.agents.map(agentLabel).join(', '))
          : el('p', 'pj-muted', 'No agents assigned. The orchestrator takes any work.'));
      };

      /* ----- toolbar ----- */
      function paintActions() {
        ctx.chrome.setActions([
          { id: 'thread', label: 'New thread', kind: 'primary', spec: 'Starts a thread in this project. It follows the project\'s instructions and can use its files.', onClick: newThread },
          { id: 'pin', label: 'Pin', pressed: p.pinned, spec: 'Keeps this project at the top of the list.', onClick: () => mutate('update', { pinned: !p.pinned }, 'head') },
          { id: 'archive', label: p.archived ? 'Restore' : 'Archive', spec: p.archived ? 'Puts this project back in the list.' : 'Hides this project from the list. Nothing is deleted.', onClick: async () => { if (await mutate('update', { archived: !p.archived })) goto(); } },
          { id: 'delete', label: 'Delete', kind: 'danger', spec: 'Deletes the project with its files, memory and threads, after you confirm.', onClick: askDelete },
        ]);
      }
      async function newThread() {
        try {
          const d = await ctx.api.post('/api/os/project/thread/new', { id: pid, title: 'New thread' });
          goto(pid, d.thread.id);
        } catch (err) {
          fail('Could not start a thread', err);
        }
      }
      function askDelete() {
        confirmHere(confirmHost, 'Delete "' + p.name + '"? Its files, memory and ' + plural(p.threads.length, 'thread') + ' are removed for good. Nothing outside this project changes.', async () => {
          await ctx.api.post('/api/os/project/delete', { id: pid, confirm: true });
          goto();
        });
      }

      try {
        await fetchProject();
      } catch (err) {
        if (isAbort(err) || my !== st.seq) return;
        root.dataset.state = err.status === 404 ? 'empty' : err.offline ? 'unavailable' : 'error';
        states.error(wrap, err, { title: err.status === 404 ? 'This project is gone' : 'Could not read the project', retry: err.status === 404 ? null : () => render() });
        const back = el('a', 'pj-back', 'Projects');
        back.href = '#/projects';
        wrap.prepend(back);
        ctx.chrome.setActions([]);
        return;
      }
      if (my !== st.seq) return;
      root.dataset.state = 'populated';
      wrap.dataset.state = 'populated';
      const back = el('a', 'pj-back', 'Projects');
      back.href = '#/projects';
      const cols = el('div', 'pj-cols');
      const left = el('div', 'pj-col');
      const right = el('div', 'pj-col');
      left.append(threads.s, work.s, outputs.s, activity.s);
      right.append(instr.s, files.s, know.s, agents.s);
      cols.append(left, right);
      wrap.replaceChildren(back, confirmHost, refs.head, cols);
      Object.values(paint).forEach((fn) => fn());
      paintActions();
      current = { reload: async () => { await fetchProject(); Object.values(paint).forEach((fn) => fn()); paintActions(); } };
    }

    /* ================= one thread ================= */
    async function showThread(my, pid, tid) {
      const busyElsewhere = saver.raw && saver.raw.busy() && saver.target && (saver.target.pid !== pid || saver.target.tid !== tid);
      if (busyElsewhere) {
        ctx.notify({ level: 'warn', title: 'A run is still going in another thread', detail: 'Stop it or wait for it to finish first.' });
        window.location.hash = hashFor(saver.target.pid, saver.target.tid);
        return;
      }
      ctx.chrome.setActions([]);
      const wrap = el('div', 'pj pj-thread');
      wrap.dataset.region = '';
      root.replaceChildren(wrap);
      states.loading(wrap, 'Opening the thread');
      let proj;
      let stored;
      try {
        proj = (await ctx.api.get('/api/os/project/get?id=' + encodeURIComponent(pid))).project;
        stored = (await ctx.api.get('/api/os/project/thread?id=' + encodeURIComponent(pid) + '&thread=' + encodeURIComponent(tid))).thread;
      } catch (err) {
        if (isAbort(err) || my !== st.seq) return;
        root.dataset.state = err.status === 404 ? 'empty' : 'error';
        states.error(wrap, err, { title: err.status === 404 ? 'This thread is gone' : 'Could not open the thread', retry: err.status === 404 ? null : () => render() });
        const back = el('a', 'pj-back', 'Projects');
        back.href = '#/projects';
        wrap.prepend(back);
        return;
      }
      if (my !== st.seq) return;
      root.dataset.state = 'populated';
      const resumed = saver.raw && saver.raw.busy() && saver.target && saver.target.pid === pid && saver.target.tid === tid;
      if (!resumed) {
        saverAttach(raw, { pid, tid }, ctx.notify, keepApi, () => {
          const p = ctx.scope.project();
          if (p && isProjectTab(p.tab_id) && !st.threadOpen) ctx.scope.leaveProject();
        });
      } else {
        saver.closed = false;
      }
      st.threadOpen = true;
      const tab = projectTab(pid, tid);
      const sc = ctx.scope.project();
      if (!sc || sc.tab_id !== tab) ctx.scope.setProject({ tab_id: tab, name: proj.name, path: '' });

      const back = el('a', 'pj-back', proj.name);
      back.href = hashFor(pid);
      const title = el('h1', 'pj-title pj-thread-title', stored.title);
      const region = el('div', 'pj-conv');
      wrap.replaceChildren(back, title, region);

      /* the first question names the thread on the server; show the name it got */
      saver.onSaved = async (t) => {
        if (t.pid !== pid || t.tid !== tid || my !== st.seq) return;
        try {
          const d = await ctx.api.get('/api/os/project/thread?id=' + encodeURIComponent(pid) + '&thread=' + encodeURIComponent(tid));
          title.textContent = d.thread.title;
        } catch (err) {
          if (!isAbort(err)) console.error(err);
        }
      };
      const view = mountThreadView(region, ctx, {
        placeholder: 'Ask in ' + proj.name,
        emptyMessage: 'Nothing in this thread yet.',
        emptyHint: 'The instructions and files of ' + proj.name + ' are used. Enter sends.',
        newThread: false,
        restoreSource: async () => turnRecords((await ctx.api.get('/api/os/project/thread?id=' + encodeURIComponent(pid) + '&thread=' + encodeURIComponent(tid))).thread.messages),
        actions: [
          { id: 'project', label: 'Project', spec: 'Goes back to the project page. A run that is going keeps going.', onClick: () => goto(pid) },
          { id: 'archive-thread', label: 'Archive', spec: 'Hides this thread from the project page. Its messages stay stored.', onClick: async () => {
            try {
              await ctx.api.post('/api/os/project/thread/archive', { id: pid, thread: tid });
              goto(pid);
            } catch (err) {
              fail('Could not archive the thread', err);
            }
          } },
        ],
      });
      st.cleanup.push(() => {
        view.destroy();
        st.threadOpen = false;
        saverClose();
      });
      await view.start({ fresh: !resumed });
    }

    /* ================= routing ================= */
    async function render() {
      const my = ++st.seq;
      teardown();
      current = null;
      const { pid, tid } = parseHash(window.location.hash);
      if (!pid) {
        ctx.chrome.setSub('');
        await showList(my);
      } else if (!tid) {
        await showProject(my, pid);
      } else {
        await showThread(my, pid, tid);
      }
    }

    /* the back and forward buttons change only the hash, and the router keeps this screen mounted for it */
    globalThis.addEventListener('hashchange', () => {
      if (/^#\/projects(\/|$)/.test(window.location.hash)) render();
    }, { signal: ctx.signal });
    ctx.signal.addEventListener('abort', () => {
      teardown();
      if (current) current = null;
    });
    await render();
  },

  async refresh(ctx, reason) {
    if (current && current.reload && reason === 'manual') await current.reload();
  },
};
