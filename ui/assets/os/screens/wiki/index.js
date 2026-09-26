/* WIKI: what the device wiki really holds, read only.

   Reads GET /api/device_wiki, which never triggers a scan. The three counts
   are computed from the entries the server returned. SCAN is shown disabled
   with the reason: no route runs a scan from this window (it summarizes file
   contents with a cloud model, so it stays behind the device_wiki agent in a
   conversation, where every model call is visible). OPEN shows the stored
   summary in place; no route returns a file's text. */

import { html, setHtml } from '../../kit/html.js';
import { states } from '../../kit/states.js';
import { agoLabel, plural } from '../../kit/format.js';
import { isAbort } from '../../core/api.js';
import { ROW_CAP, POLL_MS, STATUS_TAG, STATUS_WORD, counts, baseName, dirName, fmtSize, visibleRows, lastScan } from './helpers.js';

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

export default {
  id: 'WIKI',
  sub: 'device wiki',
  css: true,
  thread: false,

  async mount(root, ctx) {
    const st = { data: null, error: null, filter: 'ALL', query: '', open: new Set(), btns: new Map(), lastPath: '', focusBack: false };

    root.dataset.state = 'loading';
    setHtml(root, html`
      <div class="wik-note" id="wikNote" role="status" hidden></div>
      <div class="grid3" id="wikCounts" data-region></div>
      <div class="card wik-list"><div class="lbl">Files the wiki has seen</div><div id="wikList" data-region></div></div>`);
    const noteEl = root.querySelector('#wikNote');
    const countsEl = root.querySelector('#wikCounts');
    const listEl = root.querySelector('#wikList');

    ctx.chrome.setActions([
      {
        id: 'scan', label: 'SCAN', kind: 'primary', disabled: true,
        title: 'Not available from here',
        spec: 'Not available from this window: no route runs a wiki scan, and a scan sends file contents to a cloud model. Ask the device wiki agent in HOME to scan; the model calls are visible there.',
        onClick: () => {},
      },
    ]);

    function countCard(label, n, tone) {
      const c = el('div', 'card');
      c.append(el('div', 'lbl', label));
      c.append(el('div', 'big tone-' + tone, String(n)));
      return c;
    }

    function rowFor(e) {
      const wrap = document.createDocumentFragment();
      const row = el('div', 'os-row wik-row');
      row.append(el('span', 'tag ' + (STATUS_TAG[e.status] || ''), STATUS_WORD[e.status] || String(e.status || 'unknown').toLowerCase()));
      const name = el('span', 'wik-name');
      name.append(el('b', '', baseName(e.path)));
      const dir = dirName(e.path);
      if (dir) name.append(el('span', 'dir', dir));
      if (e.status === 'MISSING') name.append(el('span', 'muted', 'deleted on disk; kept, not dropped'));
      row.append(name);
      const size = fmtSize(e.size_bytes);
      if (size) row.append(el('span', 'muted', size));
      const open = st.open.has(e.path);
      const b = el('button', 'os-btn', open ? 'CLOSE' : 'OPEN');
      b.type = 'button';
      b.setAttribute('aria-expanded', String(open));
      b.dataset.spec = 'Shows what the wiki stored about this file: its summary, size, checksum and when it was last seen. It does not read the file itself.';
      b.addEventListener('click', () => {
        if (st.open.has(e.path)) st.open.delete(e.path);
        else st.open.add(e.path);
        st.lastPath = e.path;
        st.focusBack = true;
        paintRows();
      });
      st.btns.set(e.path, b);
      row.append(b);
      wrap.append(row);
      if (open) {
        const box = el('div', 'wik-open');
        box.append(el('div', 'wik-meta', e.path));
        const bits = [];
        if (e.content_hash) bits.push('sha256 ' + String(e.content_hash).slice(0, 16));
        if (e.summarized_at) bits.push('summarized ' + (agoLabel(e.summarized_at) || 'at an unknown time'));
        if (e.last_seen) bits.push('last seen ' + (agoLabel(e.last_seen) || 'at an unknown time'));
        if (bits.length) box.append(el('div', 'wik-meta', bits.join(' · ')));
        box.append(el('div', 'wik-sum', e.summary ? e.summary : e.status === 'MISSING' ? 'No summary is stored.' : 'Not summarized yet, so there is no summary to show.'));
        wrap.append(box);
      }
      return wrap;
    }

    function paintList() {
      const d = st.data;
      const entries = d.entries || [];
      const roots = d.configured_roots || [];
      if (!roots.length && !entries.length) {
        root.dataset.state = 'unavailable';
        states.unavailable(listEl, 'No folders are configured for the wiki, so it has nothing to hold.', {
          detail: 'It only ever walks the folders named in DOURMOUSE_WIKI_ROOTS, never the whole disk. Set that variable and restart the server.',
        });
        return;
      }
      if (!entries.length) {
        root.dataset.state = 'empty';
        states.empty(listEl, 'Nothing has been scanned yet.', {
          hint: 'Watching ' + plural(roots.length, 'folder', 'folders') + ': ' + roots.join(', ') + '. Scanning is not available from this window.',
        });
        return;
      }
      root.dataset.state = 'populated';
      const tools = el('div', 'wik-tools');
      [['ALL', 'ALL'], ['SUMMARIZED', 'SUMMARIZED'], ['UNSUMMARIZED', 'UNSUMMARIZED'], ['MISSING', 'MISSING']].forEach(([k, label]) => {
        const f = el('button', 'os-btn wik-f', label);
        f.type = 'button';
        f.setAttribute('aria-pressed', String(st.filter === k));
        f.dataset.spec = 'Shows only ' + (k === 'ALL' ? 'every' : k.toLowerCase()) + ' file' + (k === 'ALL' ? '' : 's') + ' in the list below.';
        f.addEventListener('click', () => {
          st.filter = k;
          paintList();
        });
        tools.append(f);
      });
      const q = el('input', 'wik-q');
      q.type = 'search';
      q.value = st.query;
      q.placeholder = 'Filter by path';
      q.setAttribute('aria-label', 'Filter files by path');
      q.dataset.spec = 'Filters the list by any part of the file path.';
      q.addEventListener('input', () => {
        st.query = q.value;
        paintRows();
      });
      tools.append(q);
      const rowsEl = el('div', 'wik-rows');
      const foot = el('div', 'muted');
      foot.style.marginTop = '8px';
      states.populated(listEl, [tools, rowsEl, foot]);
      st.rowsEl = rowsEl;
      st.footEl = foot;
      paintRows();
    }

    function paintRows() {
      const d = st.data;
      const v = visibleRows(d.entries || [], st.filter, st.query, ROW_CAP);
      const rowsEl = st.rowsEl;
      st.btns = new Map();
      if (!v.rows.length) {
        rowsEl.replaceChildren(el('div', 'muted', 'No file matches that filter.'));
      } else {
        rowsEl.replaceChildren(...v.rows.map(rowFor));
      }
      /* keep keyboard focus on the button that was just used */
      if (st.focusBack && st.btns.has(st.lastPath)) st.btns.get(st.lastPath).focus();
      st.focusBack = false;
      const last = lastScan(d.entries);
      st.footEl.textContent =
        (v.hidden ? v.hidden + ' more not shown; narrow the filter. ' : '') +
        'A deleted file is marked MISSING, never dropped, and comes back from its kept summary if it reappears. A content change reverts a file to UNSUMMARIZED instead of keeping a stale summary. ' +
        (last ? 'A scan last touched a file ' + (agoLabel(last) || 'at an unknown time') + '. ' : '') +
        'Scanning is not available from this window.';
    }

    function paint() {
      const c = counts(st.data.entries);
      countsEl.dataset.state = 'populated';
      countsEl.replaceChildren(countCard('Summarized', c.SUMMARIZED, 'ok'), countCard('Unsummarized', c.UNSUMMARIZED, 'active'), countCard('Missing', c.MISSING, 'dim'));
      const roots = st.data.configured_roots || [];
      const parts = [roots.length ? 'Watching ' + plural(roots.length, 'folder', 'folders') + ': ' + roots.join(', ') + '.' : 'No folders are configured.'];
      if (c.other) parts.push(c.other + ' entries have a status this screen does not know and are not counted above.');
      noteEl.hidden = false;
      noteEl.textContent = parts.join(' ');
      paintList();
    }

    let lastJson = '';
    async function load(reason) {
      try {
        const d = await ctx.api.get('/api/device_wiki');
        if (ctx.signal.aborted) return;
        const json = JSON.stringify(d);
        const same = json === lastJson && !st.error;
        lastJson = json;
        st.data = d;
        if (same) return; /* nothing changed: do not rebuild the list under the reader's focus */
        st.error = null;
        states.clearStale(root);
        paint();
        ctx.chrome.setLive(true);
      } catch (err) {
        if (isAbort(err) || ctx.signal.aborted) return;
        st.error = err;
        if (st.data) states.stale(root, 'Could not refresh: ' + err.message + ' The list below is from the last read.');
        else {
          root.dataset.state = 'error';
          states.error(listEl, err, { title: 'Could not read the device wiki', retry: () => load('manual') });
        }
      }
    }

    /* Esc closes the open summaries (listens on this screen's own root: the
       shell's Esc stack is not reachable from a screen). Not while typing. */
    root.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape' || e.target.closest('input, textarea')) return;
      if (!st.open.size || !st.data) return;
      st.open.clear();
      st.focusBack = true;
      paintRows();
    });
    ctx.events.onResync(() => load('resync'));
    ctx.every(POLL_MS, () => load('poll'));
    states.loading(listEl, 'Reading the device wiki');
    await load('show');
  },

  async refresh(ctx, reason) {
    if (reason === 'manual') ctx.notify({ level: 'info', title: 'WIKI', detail: 'The list re-reads every 15 seconds while this screen is open.', ttl: 2500 });
  },
};
