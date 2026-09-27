/* The launcher (Command K, or Ctrl K). Every screen, the panels, a few quick
   actions and a fall-through: whatever else you type can be sent to Dourmouse
   from HOME, and an address opens in BROWSER. The classic console had this
   (finding #121); the swap made the shell the default page, so it lives here
   now, with real ranking, recent items, and full keyboard use. */

import { html, setHtml } from '../kit/html.js';
import { icon } from '../kit/icons.js';
import { SCREENS, CONSOLE_LINKS } from '../core/registry.js';
import { ACCENTS } from '../core/prefs.js';
import { putPaneRequest } from '../core/pane-inbox.js';
import { rank, withRecentFirst } from './palette-search.js';

const RECENT_KEY = 'dm.os.palRecent';
const RECENT_MAX = 6;
const RESULT_MAX = 40;
const KIND = { SCREEN: 'Screen', ACTION: 'Action', PANEL: 'Panel', CONSOLE: 'Classic console', ASK: 'Ask', WEB: 'Web' };
const GROUP = { SCREEN: 'Screens', ACTION: 'Actions', PANEL: 'Panels', CONSOLE: 'Classic console' };

export function createPalette({ root, go, refresh, openPanel, api, chat, prefs, toasts }) {
  let def = null;
  let shown = [];
  let sel = 0;
  let opener = null;

  setHtml(root, html`
    <div class="pal-card" role="presentation">
      <div class="pal-field">
        ${icon('SEARCH', '', { width: 1.6 })}
        <input id="palq" type="text" role="combobox" aria-expanded="true" aria-controls="pallist" aria-autocomplete="list" autocomplete="off" spellcheck="false" maxlength="300" aria-label="Launcher" placeholder="Go to a screen, run an action, or ask Dourmouse" data-spec="Type to search screens and actions. Anything else can be sent to Dourmouse, and an address opens in BROWSER.">
      </div>
      <div id="pallist" role="listbox" aria-label="Results"></div>
      <div class="pal-foot" aria-hidden="true"><span><kbd>&uarr;</kbd><kbd>&darr;</kbd> choose</span><span><kbd>Enter</kbd> run</span><span><kbd>Esc</kbd> close</span></div>
      <div class="sr-only" id="palcount" role="status" aria-live="polite"></div>
    </div>`);
  const input = root.querySelector('#palq');
  const list = root.querySelector('#pallist');
  const count = root.querySelector('#palcount');

  function readRecent() {
    try {
      const v = JSON.parse(prefs.read(RECENT_KEY) || '[]');
      return Array.isArray(v) ? v.filter((x) => typeof x === 'string').slice(0, RECENT_MAX) : [];
    } catch (_err) {
      return [];
    }
  }
  function pushRecent(id) {
    const next = [id, ...readRecent().filter((x) => x !== id)].slice(0, RECENT_MAX);
    prefs.write(RECENT_KEY, JSON.stringify(next));
  }

  function askHome(text) {
    go('home');
    chat.thread('HOME').send(text).catch((err) => toasts.show({ level: 'error', title: 'Send failed', detail: err && err.message }));
  }

  async function scanNow() {
    go('security');
    toasts.show({ level: 'info', title: 'Scanning this Mac', detail: 'A read-only scan. It takes a few seconds.', ttl: 4000 });
    try {
      const r = await api.post('/api/security/action', { action: 'scan' });
      toasts.show({ level: 'ok', title: 'Scan finished', detail: r.findings + ' findings, ' + r.new + ' new.' });
    } catch (err) {
      toasts.show({ level: 'error', title: 'Scan failed', detail: err && err.message });
    }
  }

  async function newConversation() {
    go('home');
    const thread = chat.thread('HOME');
    if (thread.busy()) {
      toasts.show({ level: 'warn', title: 'A run is in progress', detail: 'Stop it first, then start a new conversation.' });
      return;
    }
    try {
      await api.post('/api/os/session/new', { tab_id: chat.tabId ? chat.tabId() : undefined });
      thread.clear();
    } catch (err) {
      toasts.show({ level: 'error', title: 'Could not start a new conversation', detail: err && err.message });
    }
  }

  function buildItems() {
    const items = [];
    SCREENS.forEach((s) => items.push({ id: 'screen:' + s.id, label: s.id, help: s.sub, kind: 'SCREEN', glyph: s.icon, run: () => go(s.slug) }));
    items.push(
      { id: 'act:scan', label: 'Scan this Mac now', help: 'a read-only security scan', kind: 'ACTION', run: scanNow },
      { id: 'act:new', label: 'New conversation', help: 'start a fresh thread on HOME', kind: 'ACTION', run: newConversation },
      { id: 'act:refresh', label: 'Refresh this screen', help: 'read it again', kind: 'ACTION', run: () => refresh() },
      {
        id: 'act:auto', label: chat.autonomous() ? 'Turn autonomous mode off' : 'Turn autonomous mode on', help: 'more steps per run; every gated action still asks', kind: 'ACTION',
        run: () => { chat.setAutonomous(!chat.autonomous()); toasts.show({ level: 'info', title: 'Autonomous mode ' + (chat.autonomous() ? 'on' : 'off'), detail: 'This tab only. It is not auto-approve.' }); },
      },
      {
        id: 'act:dnd', label: prefs.dnd() ? 'Turn Do Not Disturb off' : 'Turn Do Not Disturb on', help: 'silences in-app toasts only', kind: 'ACTION',
        run: () => { prefs.setDnd(!prefs.dnd()); toasts.show({ level: 'info', title: 'Do Not Disturb ' + (prefs.dnd() ? 'on' : 'off'), detail: 'Native desktop notifications are not affected.' }); },
      },
    );
    ACCENTS.forEach((a) => items.push({
      id: 'accent:' + a.id, label: 'Accent: ' + a.name, help: 'colour of buttons and highlights', kind: 'ACTION',
      run: () => { prefs.applyAccent(a.hex); prefs.save('accent', a.hex); },
    }));
    items.push(
      { id: 'panel:cc', label: 'Control Centre', help: 'agents, security, network, brain', kind: 'PANEL', run: () => openPanel('cc') },
      { id: 'panel:notifications', label: 'Notifications', help: 'alerts and this session', kind: 'PANEL', run: () => openPanel('notifications') },
      { id: 'panel:wallpaper', label: 'Wallpaper', help: 'gradients or your own photo', kind: 'PANEL', run: () => openPanel('wallpaper') },
    );
    CONSOLE_LINKS.forEach(([id, label]) => items.push({ id: 'console:' + id, label: label, help: 'opens the classic console', kind: 'CONSOLE', run: () => { window.location.href = '/console'; } }));
    return items;
  }

  function typedItems(text) {
    if (!text) return [];
    if (/^https?:\/\/\S+$/i.test(text)) {
      return [{ id: 'web:' + text, label: 'Open ' + text, help: 'in BROWSER', kind: 'WEB', run: () => { putPaneRequest(text); go('browser'); } }];
    }
    return [{ id: 'ask', label: 'Ask Dourmouse: ' + text, help: 'sent from HOME', kind: 'ASK', run: () => askHome(text) }];
  }

  function paint() {
    const q = input.value.trim();
    const all = buildItems();
    const recent = readRecent();
    let rows;
    const groups = new Map();
    if (q) {
      rows = [...rank(all, q, recent).slice(0, RESULT_MAX), ...typedItems(q)];
    } else {
      const { recent: head, rest } = withRecentFirst(all, recent);
      rows = [...head, ...rest];
      head.forEach((i) => groups.set(i.id, 'Recent'));
      rest.forEach((i) => { if (!groups.has(i.id)) groups.set(i.id, GROUP[i.kind] || ''); });
    }
    shown = rows;
    sel = Math.min(sel, Math.max(0, rows.length - 1));
    let last = '';
    const parts = [];
    rows.forEach((it, i) => {
      const g = q ? '' : groups.get(it.id) || '';
      if (g && g !== last) parts.push(html`<div class="pal-group" role="presentation">${g}</div>`);
      last = g || last;
      parts.push(html`<div class="pal-item" id="palopt${i}" role="option" aria-selected="${String(i === sel)}" data-i="${i}">
        <span class="gl" aria-hidden="true">${it.glyph ? icon(it.glyph, '', { width: 1.5 }) : ''}</span>
        <span class="lb">${it.label}</span><span class="hp">${it.help || ''}</span><span class="kd">${KIND[it.kind] || ''}</span></div>`);
    });
    if (!rows.length) parts.push(html`<div class="pal-empty">Nothing matches. Press Enter to ask Dourmouse instead.</div>`);
    setHtml(list, html`${parts}`);
    count.textContent = rows.length + (rows.length === 1 ? ' result' : ' results');
    input.setAttribute('aria-activedescendant', rows.length ? 'palopt' + sel : '');
    const active = list.querySelector('[aria-selected="true"]');
    if (active && active.scrollIntoView) active.scrollIntoView({ block: 'nearest' });
  }

  function run(i) {
    const it = shown[i];
    if (!it) {
      const q = input.value.trim();
      if (q) {
        def.close();
        askHome(q);
      }
      return;
    }
    if (it.kind !== 'ASK' && it.kind !== 'WEB') pushRecent(it.id);
    def.close();
    try {
      const r = it.run();
      if (r && typeof r.catch === 'function') r.catch((err) => console.error(err));
    } catch (err) {
      console.error(err);
      toasts.show({ level: 'error', title: 'That did not work', detail: err && err.message });
    }
  }

  input.addEventListener('input', () => {
    sel = 0;
    paint();
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      sel = shown.length ? (sel + 1) % shown.length : 0;
      paint();
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      sel = shown.length ? (sel - 1 + shown.length) % shown.length : 0;
      paint();
    } else if (e.key === 'Enter' && !e.isComposing) {
      e.preventDefault();
      run(sel);
    } else if (e.key === 'Tab') {
      e.preventDefault(); /* the launcher is one field: Tab must not wander behind it */
    }
  });
  list.addEventListener('click', (e) => {
    const row = e.target.closest('[data-i]');
    if (row) run(Number(row.dataset.i));
  });
  list.addEventListener('mousemove', (e) => {
    const row = e.target.closest('[data-i]');
    if (row && Number(row.dataset.i) !== sel) {
      sel = Number(row.dataset.i);
      list.querySelectorAll('.pal-item').forEach((n, i) => n.setAttribute('aria-selected', String(i === sel)));
      input.setAttribute('aria-activedescendant', 'palopt' + sel);
    }
  });
  /* a click on the dimmed backdrop (not the card) closes it */
  root.addEventListener('click', (e) => {
    if (e.target === root && def) def.close();
  });

  return {
    bind(panelDef) {
      def = panelDef;
    },
    onOpen() {
      opener = document.activeElement;
      input.value = '';
      sel = 0;
      paint();
      input.focus();
    },
    onClose() {
      opener = null;
    },
    /* test hook */
    _items: buildItems,
    get opener() {
      return opener;
    },
  };
}
