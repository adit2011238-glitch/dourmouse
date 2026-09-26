/* CODE: what the model changed in a repository, read from git, and the terminal
   output of what it ran. Read-only: every route behind this screen only reads
   (GET /api/os/code/*), the project is chosen by an id the server listed (never a
   path this page typed), and nothing here writes, stages, reverts or commits.

   Changes reach the disk through the model's own tools. Whether those tools ask
   first is read from the server (edit_gate), not assumed. */

import { html, setHtml } from '../../kit/html.js';
import { states } from '../../kit/states.js';
import { agoLabel } from '../../kit/format.js';
import { mountThreadView } from '../../kit/thread-view.js';
import { isAbort } from '../../core/api.js';
import { DiffWidget } from './diff-widget.js';
import { TerminalWidget } from './terminal-widget.js';
import { statusWord, statusTone, totalsLine, terminalSteps, pickProject } from './helpers.js';

const LOG_CAP = 20;
const OPEN_CAP = 6;
const EXPAND_CONTEXT = 200;

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function btn(label, spec, onClick, cls = '') {
  const b = el('button', 'os-btn' + (cls ? ' ' + cls : ''), label);
  b.type = 'button';
  b.dataset.spec = spec;
  b.addEventListener('click', onClick);
  return b;
}

const q = (o) => Object.entries(o).filter(([, v]) => v !== '' && v !== undefined).map(([k, v]) => k + '=' + encodeURIComponent(v)).join('&');

export default {
  id: 'CODE',
  sub: 'diff and run',
  css: true,
  thread: true,

  async mount(root, ctx) {
    const st = {
      projects: [], project: '', view: { kind: 'work', hash: '', meta: null }, data: null,
      open: new Map(), /* path -> { diff, context, loading, error, untracked } */
      commits: null, logError: null, statusError: null, first: true, busyPrev: false,
    };

    root.dataset.state = 'populated';
    setHtml(root, html`
      <div class="card code-bar" id="codeBar" data-region></div>
      <div class="card" id="codeTreeCard"><div class="lbl" id="codeTreeLbl">Changes in the working tree</div><div id="codeTree" data-region></div><div class="muted code-note" id="codeGate"></div></div>
      <div class="grid2 code-mid">
        <div class="card"><div class="lbl">Recent commits</div><div id="codeLog" data-region></div></div>
        <div class="card"><div class="lbl">Last run</div><div id="codeOut" data-region></div></div>
      </div>
      <div id="codeThread" class="code-thread"></div>`);
    const $ = (id) => root.querySelector('#' + id);
    const barEl = $('codeBar');
    const treeEl = $('codeTree');
    const treeLbl = $('codeTreeLbl');
    const gateEl = $('codeGate');
    const logEl = $('codeLog');
    const outEl = $('codeOut');

    const guard = () => ctx.signal.aborted;
    const fail = (err) => (err && err.message ? err.message : String(err));

    /* ---------------- stage bar ---------------- */
    const actions = () => [{
      id: 'refresh', label: 'REFRESH',
      spec: 'Reads the repository again: the changed files, the recent commits and any file diffs that are open. It only reads git; it changes nothing.',
      onClick: () => refreshAll('manual'),
    }];

    /* ---------------- project bar ---------------- */
    function paintBar() {
      const d = st.data;
      const sel = el('select', 'code-select');
      sel.setAttribute('aria-label', 'Project');
      sel.dataset.spec = 'Chooses which repository this screen reads. Only this app and the projects on your bookshelf that are git repositories are offered; a path is never typed here.';
      st.projects.forEach((p) => {
        const o = el('option', '', p.name);
        o.value = p.id;
        o.selected = p.id === st.project;
        sel.append(o);
      });
      sel.addEventListener('change', () => {
        st.project = sel.value;
        ctx.prefs.set('project', st.project);
        st.view = { kind: 'work', hash: '', meta: null };
        st.open.clear();
        st.data = null;
        st.commits = null;
        refreshAll('show');
      });
      const parts = [sel];
      if (d) {
        parts.push(el('span', 'tag', d.branch || 'no branch'));
        if (d.head) {
          const h = el('span', 'muted code-head', d.head.short + '  ' + d.head.subject);
          h.title = d.head.author + ', ' + d.head.date;
          parts.push(h);
        } else {
          parts.push(el('span', 'muted code-head', 'no commits yet'));
        }
        const t = el('span', 'code-tot', totalsLine(d));
        t.setAttribute('aria-label', totalsLine(d));
        parts.push(t);
      }
      barEl.replaceChildren(...parts);
      barEl.dataset.state = 'populated';
    }

    /* ---------------- files ---------------- */
    async function loadDiff(path, context, hash) {
      const key = hash ? hash + ':' + path : path;
      const cur = st.open.get(key) || {};
      st.open.set(key, { ...cur, path, hash, loading: true, error: '', context });
      paintFiles();
      try {
        const r = await ctx.api.get('/api/os/code/diff?' + q({ project: st.project, path, context, hash }));
        if (guard()) return;
        st.open.set(key, { path, hash, diff: r.diff, truncated: r.truncated, context: r.context, untracked: r.untracked, loading: false, error: '' });
      } catch (err) {
        if (isAbort(err)) return;
        st.open.set(key, { ...(st.open.get(key) || {}), loading: false, error: fail(err) });
      }
      paintFiles();
    }

    function fileCard(f, hash) {
      const key = hash ? hash + ':' + f.path : f.path;
      const o = st.open.get(key);
      const card = el('div', 'code-file');
      card.dataset.path = f.path;
      const head = el('div', 'os-row code-fhead');
      const status = hash ? '' : f.status;
      if (status) {
        const tag = el('span', 'tag ' + statusTone(status), statusWord(status));
        head.append(tag);
      }
      const name = el('span', 'rt mono', f.path);
      head.append(name);
      const counts = el('span', 'code-counts');
      if (f.binary) counts.textContent = 'binary';
      else if (f.untracked) counts.textContent = 'new';
      else {
        const a = el('span', 'code-add', '+' + f.added);
        const d = el('span', 'code-del', '-' + f.deleted);
        counts.append(a, document.createTextNode(' '), d);
      }
      head.append(counts);
      const showing = Boolean(o && (o.diff !== undefined || o.loading || o.error));
      const toggle = btn(showing ? 'HIDE' : 'SHOW DIFF', 'Shows or hides this file\'s diff. Reads it from git; nothing is changed.', () => {
        if (showing) {
          st.open.delete(key);
          paintFiles();
        } else if (!f.binary) {
          while (st.open.size >= OPEN_CAP) st.open.delete(st.open.keys().next().value);
          loadDiff(f.path, 3, hash);
        }
      });
      toggle.disabled = Boolean(f.binary);
      toggle.setAttribute('aria-expanded', String(showing));
      head.append(toggle);
      card.append(head);
      if (showing) {
        const body = el('div', 'code-fbody');
        if (o.loading && o.diff === undefined) body.append(el('div', 'muted', 'Reading the diff...'));
        else if (o.error && o.diff === undefined) {
          const e = el('div', 'st st-error');
          e.setAttribute('role', 'alert');
          e.append(el('div', 'st-m', o.error));
          body.append(e);
        } else {
          const w = DiffWidget({ diff: o.diff, label: 'diff of ' + f.path });
          body.append(w.el);
          const row = el('div', 'code-fact');
          if (!o.untracked) {
            const wide = (o.context || 3) >= EXPAND_CONTEXT;
            row.append(btn(wide ? 'COLLAPSE' : 'EXPAND', 'Shows the hunks with ' + EXPAND_CONTEXT + ' lines of surrounding context, or back to 3. Reads from git; it does not touch the file.', () => loadDiff(f.path, wide ? 3 : EXPAND_CONTEXT, hash)));
          }
          if (o.truncated) row.append(el('span', 'muted', 'This diff was cut at the server\'s size limit.'));
          if (o.loading) row.append(el('span', 'muted', 'Reading...'));
          if (row.childNodes.length) body.append(row);
        }
        card.append(body);
      }
      return card;
    }

    function paintFiles() {
      const d = st.data;
      const hash = st.view.kind === 'commit' ? st.view.hash : '';
      if (!d) return;
      const files = d.files || [];
      if (!files.length) {
        states.empty(treeEl, hash ? 'This commit changed no files.' : 'The working tree is clean.', { hint: hash ? '' : 'Nothing differs from ' + (d.head ? d.head.short : 'the index') + '. Ask the model to change something and it will show here.' });
        return;
      }
      const wrap = el('div', 'code-files');
      files.forEach((f) => wrap.append(fileCard(f, hash)));
      if (d.truncated) wrap.append(el('div', 'muted', 'Showing ' + files.length + ' of ' + d.total + ' files.'));
      states.populated(treeEl, wrap);
    }

    function paintGate() {
      const g = st.data && st.data.edit_gate;
      if (!g) {
        gateEl.textContent = 'This screen only reads git. Whether the model\'s file tools ask before writing is not known from this server.';
        return;
      }
      const open = Object.entries(g).filter(([, v]) => !v).map(([k]) => k);
      gateEl.textContent = 'This screen only reads git. It has no APPROVE, REVERT or staged diff, because no such store exists. ' + (open.length
        ? open.join(' and ') + ' write to disk at once without asking, so what you see above is already on disk; undoing it is a git action you take yourself.'
        : 'The model\'s edit tools ask for approval before they write.');
    }

    async function loadStatus() {
      const hash = st.view.kind === 'commit' ? st.view.hash : '';
      try {
        const r = hash
          ? await ctx.api.get('/api/os/code/commit?' + q({ project: st.project, hash }))
          : await ctx.api.get('/api/os/code/status?' + q({ project: st.project }));
        if (guard()) return;
        st.statusError = null;
        if (hash) st.data = { ...(st.data || {}), files: r.files, total: r.total, truncated: r.truncated, added: r.files.reduce((s, f) => s + f.added, 0), deleted: r.files.reduce((s, f) => s + f.deleted, 0) };
        else st.data = r;
        states.clearStale(treeEl);
        paintBar();
        paintGate();
        paintFiles();
        if (st.first && !hash && st.data.files.length) {
          st.first = false;
          const f = st.data.files.find((x) => !x.binary);
          if (f) loadDiff(f.path, 3, '');
        }
        st.first = false;
        /* keep open diffs fresh */
        [...st.open.entries()].forEach(([key, o]) => {
          if ((o.hash || '') !== hash) return;
          if (st.data.files.some((f) => f.path === o.path)) loadDiff(o.path, o.context || 3, hash);
          else st.open.delete(key);
        });
      } catch (err) {
        if (isAbort(err)) return;
        st.statusError = err;
        if (st.data && st.data.files) states.stale(treeEl, 'Could not refresh: ' + fail(err));
        else states.error(treeEl, err, { title: 'Could not read this repository', retry: () => refreshAll('manual') });
      }
    }

    /* ---------------- commits ---------------- */
    function paintLog() {
      const list = st.commits;
      if (!list) return;
      if (!list.length) {
        states.empty(logEl, 'No commits yet in this repository.');
        return;
      }
      const wrap = el('div', 'code-log');
      list.slice(0, LOG_CAP).forEach((c) => {
        const row = el('button', 'os-row code-commit');
        row.type = 'button';
        row.dataset.spec = 'Shows the files this commit changed, in place of the working tree, with their diffs. Reads from git only.';
        if (st.view.kind === 'commit' && st.view.hash === c.hash) row.setAttribute('aria-current', 'true');
        row.append(el('span', 'tag', c.short), el('span', 'rt', c.subject), el('span', 'muted', agoLabel(c.date) || ''));
        row.addEventListener('click', () => showCommit(c));
        wrap.append(row);
      });
      states.populated(logEl, wrap);
    }

    async function loadLog() {
      try {
        const r = await ctx.api.get('/api/os/code/log?' + q({ project: st.project, limit: LOG_CAP }));
        if (guard()) return;
        st.commits = r.commits;
        states.clearStale(logEl);
        paintLog();
      } catch (err) {
        if (isAbort(err)) return;
        if (st.commits) states.stale(logEl, 'Could not refresh: ' + fail(err));
        else states.error(logEl, err, { title: 'Could not read the commit list', retry: () => loadLog() });
      }
    }

    function showCommit(c) {
      st.view = { kind: 'commit', hash: c.hash, meta: c };
      st.open.clear();
      paintTitle();
      paintLog();
      loadStatus();
    }

    function backToWork() {
      st.view = { kind: 'work', hash: '', meta: null };
      st.open.clear();
      paintTitle();
      paintLog();
      loadStatus();
    }

    function paintTitle() {
      if (st.view.kind === 'commit') {
        treeLbl.replaceChildren(document.createTextNode('Commit ' + st.view.meta.short + ': ' + st.view.meta.subject + '  '));
        treeLbl.append(btn('WORKING TREE', 'Goes back to the uncommitted changes in the working tree.', backToWork));
      } else {
        treeLbl.textContent = 'Changes in the working tree';
      }
    }

    /* ---------------- last run: terminal blocks from this thread's real tool calls ---------------- */
    let outKey = '';
    function paintOut() {
      const steps = terminalSteps(ctx.chat.turns(), 3);
      const key = steps.map((s) => s.name + (s.running ? 'r' : '') + (s.result || '').length).join('|');
      if (key === outKey && outEl.dataset.state) return;
      outKey = key;
      if (!steps.length) {
        states.empty(outEl, 'No test run recorded.', { hint: 'This screen has no test runner. When the model runs something in this thread (run_python, claude_code, codex_code), its output appears here as it came back.' });
        return;
      }
      states.populated(outEl, steps.map((s) => TerminalWidget(s)));
    }

    /* ---------------- refresh ---------------- */
    async function refreshAll() {
      await Promise.all([loadStatus(), loadLog()]);
    }

    async function init() {
      states.loading(treeEl, 'Reading the repository');
      states.loading(logEl, 'Reading commits');
      try {
        const r = await ctx.api.get('/api/os/code/projects');
        if (guard()) return;
        st.projects = r.projects || [];
        st.project = pickProject(st.projects, ctx.scope.project(), ctx.prefs.get('project'));
        if (!st.project) {
          states.empty(treeEl, 'No repository is available.');
          return;
        }
        paintBar();
        await refreshAll();
      } catch (err) {
        if (isAbort(err)) return;
        states.error(treeEl, err, { title: 'Could not list projects', retry: () => init() });
        states.error(logEl, err, { title: 'Could not list projects' });
      }
    }

    /* ---------------- thread ---------------- */
    const view = mountThreadView($('codeThread'), ctx, {
      scroller: root.parentElement,
      placeholder: 'Ask for a code change, or a run. What it changes shows above.',
      emptyMessage: 'No coding conversation yet.',
      actions,
    });
    ctx.chat.subscribe((e) => {
      if (e.kind === 'busy') {
        const now = ctx.chat.busy();
        if (st.busyPrev && !now) refreshAll();
        st.busyPrev = now;
      }
      paintOut();
    });
    ctx.every(20000, () => refreshAll());
    ctx.events.onResync(() => refreshAll());
    ctx.scope.onChange(() => {
      const next = pickProject(st.projects, ctx.scope.project(), st.project);
      if (next && next !== st.project) {
        st.project = next;
        st.data = null;
        st.commits = null;
        st.open.clear();
        refreshAll();
      }
    });

    paintOut();
    await view.start();
    await init();
    paintOut();
    ctx.chrome.focusComposer();
  },

  async refresh(ctx, reason) {
    if (reason === 'manual') ctx.notify({ level: 'info', title: 'CODE', detail: 'Use REFRESH in the stage bar to re-read git.', ttl: 2000 });
  },
};
