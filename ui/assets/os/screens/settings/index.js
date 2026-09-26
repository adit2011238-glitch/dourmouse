/* SETTINGS: how Dourmouse is set up on this Mac. Every value is a real read of
   /api/os/settings/summary (the socket the server is bound to, the config
   file, the loaded models) and of the orchestrator-model route. A section
   that cannot be read says so in its own words; nothing is filled in.

   Nothing changes with one click. A switch, the shell choice, the librarian
   folders, RESET and above all AUTO APPROVE each open a card that says what
   will happen and what it does not touch; only APPROVE saves. Background
   switches take effect at the next launch and say so. Reduced motion has no
   override in the shell yet, so it shows what the OS says and nothing more.
   TEST ALL checks keys and reachability and sends no prompt. */

import { html, setHtml } from '../../kit/html.js';
import { states } from '../../kit/states.js';
import { confirmHere } from '../../kit/confirm-card.js';
import { isAbort } from '../../core/api.js';
import {
  groupFeatures, featurePrompt, foldersPrompt, parseFolders, togglePrompt, shellPrompt, resetPrompt, shellChoices,
  keyTag, tokenTag, bindLine, orchestratorSource, localModelRow, backendRow, osReducedMotion, SHELL_WORD,
} from './helpers.js';

const reloaders = new WeakMap();

export default {
  id: 'SETTINGS',
  sub: 'preferences',
  css: true,
  thread: false,

  async mount(root, ctx) {
    const st = { sum: null, sumError: null, staleMsg: '', orch: null, orchError: null, orchLoading: true, test: null, testing: false };

    root.dataset.state = 'loading';
    setHtml(root, html`
      <div class="set-note" id="setNote" role="status" hidden></div>
      <div id="setConfirm"></div>
      <div class="grid2">
        <div class="card"><div class="lbl">Shell</div><div id="setShell" data-region></div></div>
        <div class="card"><div class="lbl">Models &middot; large cloud only</div><div id="setModels" data-region></div></div>
      </div>
      <div class="card set-gap"><div class="lbl">Remote access</div><div id="setAccess" data-region></div></div>
      <div class="card set-gap"><div class="lbl">Background switches</div><div id="setFeatures" data-region></div></div>
      <div class="card set-gap"><div class="lbl">Behaviour</div><div id="setBehaviour" data-region></div></div>`);
    const $ = (id) => root.querySelector('#' + id);
    const noteEl = $('setNote');
    const confirmEl = $('setConfirm');
    const shellEl = $('setShell');
    const modelsEl = $('setModels');
    const accessEl = $('setAccess');
    const featEl = $('setFeatures');
    const behEl = $('setBehaviour');
    const regions = [shellEl, modelsEl, accessEl, featEl, behEl];

    const note = (text, tone) => {
      noteEl.hidden = !text;
      noteEl.textContent = text || '';
      noteEl.dataset.tone = tone || '';
    };

    /* ---------------- stage bar ---------------- */
    function paintActions() {
      const items = st.sum && st.sum.features && Array.isArray(st.sum.features.items) ? st.sum.features.items : null;
      ctx.chrome.setActions([
        {
          id: 'reset', label: 'RESET', kind: 'danger', disabled: !items,
          spec: 'Puts the background switches back to their defaults, after asking. It does not touch API keys, the model choice, auto approve or the shell.',
          onClick: () => resetSwitches(items),
        },
      ]);
    }

    /* ---------------- reads ---------------- */
    async function loadSummary() {
      try {
        const data = await ctx.api.get('/api/os/settings/summary');
        if (ctx.signal.aborted) return;
        st.sum = data;
        st.staleMsg = ctx.api.isStale(data) ? 'Showing the last copy this window kept. The server did not answer.' : '';
        st.sumError = null;
      } catch (err) {
        if (isAbort(err)) return;
        st.sumError = err;
      }
      paint();
    }

    async function loadOrch() {
      st.orchLoading = true;
      try {
        const data = await ctx.api.get('/api/settings/orchestrator-model');
        if (ctx.signal.aborted) return null;
        if (data && data.error) throw Object.assign(new Error(String(data.error)), { status: 200, path: '/api/settings/orchestrator-model' });
        st.orch = data;
        st.orchError = null;
      } catch (err) {
        if (isAbort(err)) return null;
        st.orchError = err;
      }
      st.orchLoading = false;
      paintModels();
      return st.orch;
    }

    /* ---------------- painting ---------------- */
    function sectionError(sec) {
      return sec && typeof sec.error === 'string' ? sec.error : '';
    }

    function paint() {
      if (!st.sum) {
        if (!st.sumError) return;
        root.dataset.state = st.sumError.offline ? 'unavailable' : 'error';
        regions.forEach((el, i) => states.error(el, st.sumError, { title: 'Could not read the settings', retry: i === 0 ? () => reload() : null }));
        paintActions();
        return;
      }
      paintShell();
      paintModels();
      paintAccess();
      paintFeatures();
      paintBehaviour();
      paintActions();
      if (st.sumError) {
        states.stale(root, 'Could not refresh: ' + st.sumError.message + ' The values below are from the last read.');
      } else if (st.staleMsg) {
        states.stale(root, st.staleMsg);
      } else {
        root.querySelectorAll(':scope > .st-stale').forEach((n) => n.remove());
        root.dataset.state = 'populated';
      }
    }

    function sw(attr, key, on, label, spec, disabled) {
      return html`<button type="button" class="os-switch" role="switch" aria-checked="${String(Boolean(on))}" aria-label="${label}" ${attr}="${key}" ${disabled ? 'disabled' : ''} data-spec="${spec}"></button>`;
    }

    function toggleRow(t) {
      const disabled = t.value === null || t.value === undefined;
      return html`<div class="kv set-tg"><span>${t.label}${t.value ? html` <span class="tag ${t.danger ? 'bad' : 'ok'}">on</span>` : ''}${disabled ? html` <span class="tag warn">unreadable</span>` : ''}</span>
        ${sw('data-toggle', t.id, t.value, t.label, t.help, disabled)}</div>${disabled && t.error ? html`<div class="muted set-help">${t.error}</div>` : ''}`;
    }

    function paintShell() {
      const sh = st.sum.shell;
      const tg = st.sum.toggles;
      const err = sectionError(sh);
      const tgErr = sectionError(tg);
      if (err && tgErr) {
        states.error(shellEl, new Error(err), { title: 'Could not read the shell settings', retry: () => reload() });
        return;
      }
      const parts = [];
      if (err) {
        parts.push(html`<div class="muted set-err">Shell choice unavailable: ${err}</div>`);
      } else {
        const choices = shellChoices(sh);
        const running = ctx.host && ctx.host.kind ? ctx.host.kind : 'browser';
        parts.push(html`<div class="kv"><span>Native shell</span><span class="os-seg" role="group" aria-label="Native shell">${choices.map((c) => html`<button type="button" aria-selected="${String(c.selected)}" data-shell="${c.value}" ${c.disabled ? 'disabled' : ''} data-spec="${c.reason || (c.value === 'auto' ? 'Electron when it is installed, pywebview otherwise. Read at the next launch, after asking.' : c.value === 'electron' ? 'The Electron shell with the browser pane. Read at the next launch, after asking.' : 'The older pywebview window. Always available. Read at the next launch, after asking.')}">${c.label}</button>`)}</span></div>
          <div class="muted set-help">Running now: ${running}. The next launch will use ${SHELL_WORD[sh.effective_next_launch] || sh.effective_next_launch}.${sh.invalid_value ? ' The saved value ' + sh.invalid_value + ' is not one of auto, electron or pywebview, so it is read as auto.' : ''}${sh.electron_available === false ? ' Electron is not installed in this checkout.' : ''}</div>`);
      }
      const rm = osReducedMotion(globalThis);
      parts.push(html`<div class="kv"><span>Reduced motion</span><span class="tag" data-spec="This window follows the operating system's reduced-motion setting. A forced override is not available yet: the shell stylesheet has no switch for it.">${rm === null ? 'unknown' : rm ? 'OS: reduce' : 'OS: no preference'}</span></div>
        <div class="muted set-help">Follows the OS setting. Forcing it on from here is not available yet. The wallpaper animation has its own switch in the WALLPAPER menu.</div>`);
      if (tgErr) {
        parts.push(html`<div class="muted set-err">Auto approve unavailable: ${tgErr}</div>`);
      } else {
        const auto = tg.items.find((t) => t.id === 'auto_approve');
        if (auto) parts.push(toggleRow(auto));
        parts.push(html`<div class="muted set-help">Off is the safe default. Turning it on asks first and says what it does.</div>`);
      }
      shellEl.dataset.state = 'populated';
      setHtml(shellEl, html`${parts}`);
    }

    function paintModels() {
      const m = st.sum ? st.sum.models : null;
      const k = st.sum ? st.sum.keys : null;
      if (!st.sum) return;
      const mErr = sectionError(m);
      const kErr = sectionError(k);
      const rows = [];
      if (st.orchError) {
        rows.push(html`<div class="kv"><span>Orchestrator brain</span><b><span class="tag warn">unreadable</span></b></div><div class="muted set-err">${st.orchError.message}</div>`);
      } else if (st.orch && st.orch.current) {
        const src = orchestratorSource(st.orch.source);
        rows.push(html`<div class="kv"><span>Orchestrator brain</span><b><span class="tag ok">${st.orch.current}</span></b></div>${src ? html`<div class="muted set-help">${src}</div>` : ''}`);
      } else if (st.orchLoading) {
        rows.push(html`<div class="kv"><span>Orchestrator brain</span><b class="muted">reading</b></div>`);
      } else {
        rows.push(html`<div class="kv"><span>Orchestrator brain</span><b><span class="tag warn">none chosen</span></b></div>`);
      }
      if (mErr) {
        rows.push(html`<div class="muted set-err">Backend and researcher unavailable: ${mErr}</div>`);
      } else {
        rows.push(html`<div class="kv"><span>Backend in use</span><b>${m.backend || 'none'}${m.base_url ? html` <span class="muted mono">${m.base_url}</span>` : ''}</b></div>`);
        rows.push(html`<div class="kv"><span>Researcher</span><b><span class="tag ok">${m.researcher.model}</span></b></div>`);
      }
      if (kErr) {
        rows.push(html`<div class="muted set-err">Keys unavailable: ${kErr}</div>`);
      } else {
        const o = keyTag(k.OLLAMA_API_KEY);
        const g = keyTag(k.GEMINI_API_KEY);
        rows.push(html`<div class="kv"><span>Ollama Cloud</span><b><span class="tag ${o.tone}">${o.word}</span>${o.where ? html` <span class="muted">${o.where}</span>` : ''}</b></div>`);
        rows.push(html`<div class="kv"><span>Gemini</span><b><span class="tag ${g.tone}">${g.word}</span>${g.where ? html` <span class="muted">${g.where}</span>` : ''}</b></div>`);
      }
      if (!mErr) {
        const lm = localModelRow(m);
        rows.push(html`<div class="kv"><span>Local / small models</span><b><span class="tag ${lm.tone}">${lm.word}</span></b></div><div class="muted set-help">${lm.text}</div>`);
      }
      const tests = st.test;
      const testBlock = st.testing
        ? html`<div class="muted set-help" role="status">Checking every backend.</div>`
        : tests && tests.error
          ? html`<div class="muted set-err">${tests.error}</div>`
          : tests
            ? html`<div class="set-test"><div class="muted set-help">Checked ${tests.at}. Keys and reachability only: no prompt was sent, so nothing was spent and no model answer was tested.</div>${tests.rows.map((r) => html`<div class="kv"><span>${r.name}${r.model ? html` <span class="muted mono">${r.model}</span>` : ''}</span><b><span class="tag ${r.tone}">${r.word}</span></b></div>${r.detail ? html`<div class="muted set-help">${r.detail}</div>` : ''}`)}</div>`
            : '';
      modelsEl.dataset.state = 'populated';
      setHtml(modelsEl, html`${rows}<div class="set-btns"><button type="button" class="os-btn" data-testall ${st.testing ? 'disabled' : ''} data-spec="Checks that each backend has its key or is reachable, and shows the real reason when it is not. It sends no prompt to any model, so it costs nothing and does not prove a model will answer.">${st.testing ? 'TESTING' : 'TEST ALL'}</button></div>${testBlock}`);
    }

    function paintAccess() {
      const a = st.sum.access;
      const err = sectionError(a);
      if (err) {
        states.error(accessEl, new Error(err), { title: 'Could not read the access settings', retry: () => reload() });
        return;
      }
      const tk = tokenTag(a);
      accessEl.dataset.state = 'populated';
      setHtml(accessEl, html`
        <div class="kv"><span>Bind host</span><b class="mono">${bindLine(a)}</b></div>
        <div class="kv"><span>Access token</span><b><span class="tag ${tk.tone}">${tk.word}</span></b></div>
        <div class="muted set-help">${tk.note}${a.insecure_override ? ' The insecure-bind override is on, so the server may start beyond this Mac without a token.' : ''}</div>
        <div class="muted set-help">The server refuses to start on an address other than this Mac without an access token, unless an explicit insecure-bind override is set. The token itself is never shown here.</div>`);
    }

    function featureRow(f) {
      const on = f.value;
      return html`<div class="os-row set-feat"><span class="rt"><b>${f.label}</b><span class="muted set-block">${f.help}</span></span>${sw('data-feature', f.key, on, f.label, f.help + ' Applies at the next launch, after asking.', typeof on !== 'boolean')}</div>`;
    }

    function paintFeatures() {
      const sec = st.sum.features;
      const err = sectionError(sec);
      if (err) {
        states.error(featEl, new Error(err), { title: 'Could not read the background switches', retry: () => reload() });
        return;
      }
      const groups = groupFeatures(sec.items);
      if (!groups.length) {
        states.empty(featEl, 'The server listed no background switches.');
        return;
      }
      featEl.dataset.state = 'populated';
      setHtml(featEl, html`<div class="muted set-help">These run in the background from the moment Dourmouse starts, so a change is saved now and applied at the next launch.</div>
        ${groups.map((g) => html`<div class="lbl set-sec">${g.title}</div>${g.switches.map(featureRow)}${g.folders.map((f) => html`<div class="set-folders"><label for="setFolders"><b>${f.label}</b><span class="muted set-block">${f.help}</span></label>
          <textarea id="setFolders" rows="3" data-folders="${f.key}" spellcheck="false" data-spec="One full folder path per line. Empty means Documents, Desktop and Downloads.">${(f.value || []).join('\n')}</textarea>
          <div class="set-btns"><button type="button" class="os-btn" data-folders-save="${f.key}" data-spec="Saves this list of folders for the librarian after asking. Each must be a real folder on this Mac. Applies at the next launch.">SAVE FOLDERS</button></div></div>`)}`)}`);
    }

    function paintBehaviour() {
      const tg = st.sum.toggles;
      const err = sectionError(tg);
      if (err) {
        states.error(behEl, new Error(err), { title: 'Could not read these settings', retry: () => reload() });
        return;
      }
      const rest = tg.items.filter((t) => t.id !== 'auto_approve');
      behEl.dataset.state = 'populated';
      setHtml(behEl, html`<div class="muted set-help">These apply at once, with no restart, and are saved to your config file.</div>${rest.map((t) => html`${toggleRow(t)}<div class="muted set-help">${t.help}</div>`)}`);
    }

    /* ---------------- actions ---------------- */
    async function reload() {
      regions.forEach((el) => states.loading(el, 'Reading the settings'));
      await Promise.all([loadSummary(), loadOrch()]);
    }

    /* the owner's click plus a card that says what will happen; the write runs only after APPROVE */
    function ask(prompt, run, doneText) {
      confirmHere(confirmEl, prompt, async () => {
        await run();
        note(doneText, 'ok');
        await loadSummary();
      });
    }

    function flipToggle(id) {
      const items = st.sum && st.sum.toggles ? st.sum.toggles.items : [];
      const t = items.find((x) => x.id === id);
      if (!t || typeof t.value !== 'boolean') return;
      const next = !t.value;
      ask(togglePrompt(t, next), () => ctx.api.post('/api/os/settings/toggle', { id, enabled: next }),
        t.label + ' is now ' + (next ? 'on' : 'off') + '.');
    }

    function flipFeature(key) {
      const items = st.sum && st.sum.features ? st.sum.features.items : [];
      const f = items.find((x) => x.key === key);
      if (!f || typeof f.value !== 'boolean') return;
      const next = !f.value;
      ask(featurePrompt(f, next), () => ctx.api.post('/api/os/settings/feature', { key, value: next }),
        f.label + ' is saved as ' + (next ? 'on' : 'off') + '. It applies at the next launch.');
    }

    function saveFolders(key) {
      const items = st.sum && st.sum.features ? st.sum.features.items : [];
      const f = items.find((x) => x.key === key);
      const ta = root.querySelector('textarea[data-folders]');
      if (!f || !ta) return;
      const lines = parseFolders(ta.value);
      ask(foldersPrompt(f, lines), () => ctx.api.post('/api/os/settings/feature', { key, value: lines }),
        'The librarian folders are saved. They apply at the next launch.');
    }

    function chooseShell(value) {
      const sh = st.sum && st.sum.shell;
      if (!sh || sh.requested === value) return;
      ask(shellPrompt(value, sh), () => ctx.api.post('/api/os/settings/shell', { value }),
        'The shell is saved as ' + (SHELL_WORD[value] || value) + '. It applies at the next launch.');
    }

    function resetSwitches(items) {
      if (!items) return;
      confirmHere(confirmEl, resetPrompt(items), async () => {
        const r = await ctx.api.post('/api/os/settings/reset', { scope: 'features' });
        const stuck = Array.isArray(r.still_overridden) ? r.still_overridden : [];
        note('Reset done: ' + (r.cleared || []).length + ' saved values cleared. It applies at the next launch.' +
          (stuck.length ? ' ' + stuck.length + ' switch' + (stuck.length === 1 ? ' is' : 'es are') + ' also set by the environment and stay as they are: ' + stuck.join(', ') + '.' : ''), stuck.length ? 'info' : 'ok');
        await loadSummary();
      });
    }

    async function testAll() {
      st.testing = true;
      st.test = null;
      paintModels();
      try {
        const data = await ctx.api.get('/api/settings/orchestrator-model');
        if (ctx.signal.aborted) return;
        if (data && data.error) throw new Error(String(data.error));
        st.orch = data;
        st.orchError = null;
        st.test = { at: new Date().toLocaleTimeString(), rows: (Array.isArray(data.backends) ? data.backends : []).map(backendRow) };
      } catch (err) {
        if (isAbort(err)) return;
        st.test = { error: 'Could not check the backends: ' + err.message };
      } finally {
        st.testing = false;
        if (!ctx.signal.aborted) paintModels();
      }
    }

    root.addEventListener('click', (e) => {
      const t = e.target.closest('[data-toggle]');
      if (t) return flipToggle(t.dataset.toggle);
      const f = e.target.closest('[data-feature]');
      if (f) return flipFeature(f.dataset.feature);
      const s = e.target.closest('[data-shell]');
      if (s) return chooseShell(s.dataset.shell);
      const fs = e.target.closest('[data-folders-save]');
      if (fs) return saveFolders(fs.dataset.foldersSave);
      if (e.target.closest('[data-testall]')) return testAll();
      return undefined;
    });

    reloaders.set(ctx, reload);
    paintActions();
    regions.forEach((el) => states.loading(el, 'Reading the settings'));
    loadSummary();
    await loadOrch();
  },

  async refresh(ctx, reason) {
    const fn = reloaders.get(ctx);
    if (fn && reason !== 'show') fn();
  },
};
