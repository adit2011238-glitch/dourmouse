/* Entry point. Wires core, renders the chrome once, registers the service
   worker named by this script tag's data-sw attribute (the CSP forbids inline
   script, so the path is read from the tag), starts the router, and exposes
   window.__dmShell.stats(): live subscriptions, timers, key bindings and the
   current screen, so a live check can prove a screen cleaned up after itself. */

import { createApi } from './core/api.js';
import { createEvents } from './core/events.js';
import { createScope } from './core/scope.js';
import { createApprovals } from './core/approvals.js';
import { createChat } from './core/chat.js';
import { createPrefs } from './core/prefs.js';
import { createKeymap } from './core/keymap.js';
import { createHost } from './core/host.js';
import { createTimers, createScreenCtx } from './core/ctx.js';
import { createRouter } from './core/router.js';
import * as registry from './core/registry.js';
import { html, esc, raw } from './kit/html.js';
import { states } from './kit/states.js';
import { approvalCard } from './kit/approval-card.js';
import { confirmHere } from './kit/confirm-card.js';
import { ring } from './core/ring.js';
import { icon, ICON } from './kit/icons.js';
import { flowSvg } from './kit/flow-svg.js';
import { md } from './kit/md.js';
import * as format from './kit/format.js';
import { createToasts } from './chrome/toasts.js';
import { createStatus } from './chrome/status.js';
import { createPanels } from './chrome/panels.js';
import { createSidebar } from './chrome/sidebar.js';
import { createStage } from './chrome/stage.js';
import { createComposer } from './chrome/composer.js';
import { createSpecOverlay } from './chrome/spec-overlay.js';
import { createNotifCentre } from './chrome/notif-centre.js';
import { createInspector } from './chrome/inspector.js';
import { createSheets } from './chrome/sheet.js';
import { createStartupCheck } from './chrome/startup-check.js';
import { createDrivingStrip } from './chrome/driving-strip.js';
import { putPaneRequest } from './core/pane-inbox.js';
import { createPalette } from './chrome/palette.js';
import { shortcutList, bindable } from './core/shortcuts.js';
import { createAppearance } from './core/appearance.js';
import { createCommands } from './core/commands.js';

const $ = (id) => document.getElementById(id);

function boot() {
  /* T6 (finding #194): light or dark, the accent and the system colours come from macOS before
     anything is drawn; in a plain browser they follow prefers-color-scheme */
  const appearance = createAppearance();
  const appearanceReady = appearance.start();

  /* core services. prefs first: the text size is painted from localStorage before the network. */
  const api = createApi();
  const prefs = createPrefs({ api });
  prefs.applyAll();
  const scope = createScope();
  scope.applyQuery(window.location.search);
  const events = createEvents();
  const timers = createTimers();
  const keymap = createKeymap();
  keymap.attach(document);
  const host = createHost();
  const approvals = createApprovals({ api, scope });
  const chat = createChat({ api, scope, approvals, threadScreens: registry.THREAD_SCREENS });
  const toasts = createToasts({ mount: $('toasts'), prefs, timers });
  const status = createStatus({ api, events, timers });
  const menuBridge = window.dourmouseShell && window.dourmouseShell.menu ? window.dourmouseShell.menu : null;

  /* chrome: sidebar, toolbar, inspector, sheets. The window frame, traffic lights and menu bar are macOS's own. */
  const panels = createPanels({ keymap });
  const sheets = createSheets({ layer: $('sheetlayer'), keymap });
  const shellEl = $('shell');
  const layout = {
    sidebarHidden: () => shellEl.dataset.sidebar === 'hidden',
    syncMenu() {
      if (menuBridge) menuBridge.setState({ sidebar: !layout.sidebarHidden(), inspector: inspector.shown() });
    },
  };
  function toggleSidebar() {
    const hide = !layout.sidebarHidden();
    shellEl.dataset.sidebar = hide ? 'hidden' : 'shown';
    stage.sidebarHidden(hide);
    sidebar.toggleLabel(hide);
    layout.syncMenu();
  }
  const sidebar = createSidebar({
    root: $('sidebar'), registry, go: (slug) => router.go(slug), onToggle: toggleSidebar,
    onEditPins: () => editPins(), onOpenPin: (path) => openPin(path),
  });
  const stage = createStage({
    shell: shellEl, stage: $('stage'), bar: $('stagebar'), titleEl: $('stagetitle'), subEl: $('stagesub'),
    actionsEl: $('stageactions'), viewsEl: $('viewswitch'), body: $('body'), go: (slug) => router.go(slug),
    onSidebar: toggleSidebar, onInspector: () => inspector.toggle(), onSearch: () => defs.palette.toggle(),
    scope, registry,
  });
  const inspector = createInspector({
    root: $('inspector'),
    onVisible: (on) => {
      stage.inspectorShown(on);
      layout.syncMenu();
    },
  });
  stage.inspectorShown(inspector.shown());
  const composer = createComposer({ root: $('composer') });
  composer.reset();
  const spec = createSpecOverlay({ keymap });
  /* F2: "Model is driving <App>" with STOP, across the top of the window while the app_driver indicator is on */
  const drivingStrip = createDrivingStrip({ after: $('wall'), api, events, toasts });

  /* the alerts list lives in the inspector (it replaced the floating Notification Centre and the dock) */
  const notif = createNotifCentre({
    root: inspector.alertsRoot, api, events, toasts, prefs, timers,
    dock: { setBadge: (n) => { inspector.setAlertCount(n); } },
  });
  let alertsOpen = false;
  inspector.onTab((tab) => {
    const want = tab === 'alerts';
    if (want === alertsOpen) return;
    alertsOpen = want;
    if (want) notif.onOpen();
    else notif.onClose();
  });

  /* S12: the sign-in check is a quiet banner on Home, not a floating dialog */
  const startup = createStartupCheck({ root: $('stagebanner'), api, toasts, prefs });
  const shortcuts = shortcutList(registry.PLACES);

  function showShortcuts() {
    const groups = [];
    shortcuts.forEach((s) => {
      let g = groups.find((x) => x.name === s.group);
      if (!g) groups.push((g = { name: s.group, rows: [] }));
      g.rows.push(s);
    });
    sheets.open({
      title: 'Keyboard shortcuts',
      body: html`<div class="sc-list">${groups.map((g) => html`<h3 class="sc-name">${g.name}</h3>${g.rows.map((r) => html`<div class="sc-row"><span>${r.label}</span><kbd class="sc-keys">${r.keys}</kbd></div>`)}`)}</div>`,
      actions: [{ label: 'Done', kind: 'primary' }],
    });
  }

  /* panels the launcher, the #/alerts route and older callers can open by name */
  const defs = {
    notifications: { open: () => inspector.show('alerts'), close: () => inspector.hide(), toggle: () => inspector.toggleAlerts() },
    shortcuts: { open: showShortcuts, close: () => sheets.close(), toggle: showShortcuts },
    inspector: { open: () => inspector.show(), close: () => inspector.hide(), toggle: () => inspector.toggle() },
  };
  const palette = createPalette({
    scope, shortcuts,
    root: $('palette'), go: (slug) => router.go(slug), refresh: () => router.refresh('manual'),
    openPanel: (name) => defs[name] && defs[name].open(), api, chat, prefs, toasts, startup,
  });
  defs.palette = panels.make('palette', { el: $('palette'), trigger: $('palBtn'), exclusive: [], onOpen: () => palette.onOpen(), onClose: () => palette.onClose() });
  palette.bind(defs.palette);

  /* every menu command, keyboard shortcut and launcher action runs through one table (core/commands.js) */
  const commands = createCommands({
    settings: () => router.go('settings'),
    new: () => palette.newConversation(),
    search: () => defs.palette.toggle(),
    sidebar: toggleSidebar,
    inspector: () => inspector.toggle(),
    alerts: () => inspector.toggleAlerts(),
    refresh: () => router.refresh('manual'),
    spec: () => spec.toggle(),
    shortcuts: showShortcuts,
    place: (slug) => {
      const place = registry.placeBySlug(slug);
      if (place) router.go(place.views[0].toLowerCase());
    },
  });
  bindable(shortcuts).forEach((s) => {
    if (s.id === 'undo') {
      /* Finding #171: undo must leave Command Z to the text field the owner is typing in */
      keymap.bind(s.combo, () => toasts.undoLast(), { editable: false });
      return;
    }
    keymap.bind(s.combo, () => commands.run(s.id), { editable: true });
  });
  keymap.bind('Ctrl+k', () => commands.run('search'), { editable: true });
  if (menuBridge) menuBridge.onCommand((id) => commands.run(id));
  layout.syncMenu();
  /* S21: the first Tab stop skips the sidebar and lands on the view's title */
  $('skipLink').addEventListener('click', () => stage.focusTitle());

  /* pinned projects: folder paths saved in prefs, names and tab ids from the project list */
  let projects = null; /* null: not read yet */
  async function readProjects() {
    const data = await api.get('/api/os/projects/list');
    projects = Array.isArray(data && data.projects) ? data.projects : [];
    return projects;
  }
  function paintPins() {
    const active = scope.project();
    const pins = prefs.pinned();
    if (!pins.length) return sidebar.setPins([], '');
    if (projects === null) return sidebar.setPins(null);
    const byPath = new Map(projects.map((p) => [p.path, p]));
    sidebar.setPins(pins.filter((p) => byPath.has(p)).map((p) => ({ path: p, name: byPath.get(p).name || p, tab_id: byPath.get(p).tab_id })), active ? active.tab_id : '');
  }
  function loadPins() {
    if (!prefs.pinned().length) return paintPins();
    sidebar.setPins(null);
    return readProjects().then(paintPins, (err) => sidebar.setPins({ error: err && err.message ? err.message : String(err) }));
  }
  async function openPin(path) {
    const p = (projects || []).find((x) => x.path === path);
    if (!p) return;
    try {
      const r = await api.post('/api/projects/open', { path });
      const opened = r && r.project && r.project.tab_id ? r.project : p;
      scope.setProject({ tab_id: String(opened.tab_id), name: String(opened.name || p.name), path });
      router.go('projects');
    } catch (err) {
      toasts.show({ level: 'error', title: 'Could not open ' + p.name, detail: err && err.message });
    }
  }
  async function editPins() {
    const sheet = sheets.open({ title: 'Pinned projects', body: html`<div class="st st-loading" role="status"><span class="st-spin" aria-hidden="true"></span><span>Reading projects</span></div>`, actions: [{ label: 'Cancel', cancel: true }] });
    let list;
    try {
      list = await readProjects();
    } catch (err) {
      states.error(sheet.body, err, { title: 'Could not read projects', retry: () => editPins() });
      return;
    }
    if (!sheets.isOpen()) return;
    const pinned = new Set(prefs.pinned());
    const body = list.length
      ? html`<p class="sheet-note">Pinned projects show in the sidebar under the places.</p><div class="pin-list">${list.map((p, i) => html`<label class="pin-row"><input type="checkbox" data-pin-path="${p.path}" ${pinned.has(p.path) ? raw('checked') : ''} id="${'pinc' + i}"><span class="cover" data-cover="${String(1 + (i % 6))}" aria-hidden="true"></span><span>${p.name || p.path}</span></label>`)}</div><p class="sheet-error" id="pinErr" role="alert" hidden></p>`
      : html`<div class="st st-empty"><div class="st-m">No projects yet. Create one in Projects first.</div></div>`;
    sheets.open({
      title: 'Pinned projects',
      body,
      actions: list.length
        ? [{ label: 'Cancel', cancel: true }, {
          label: 'Save', kind: 'primary',
          onClick: async (s) => {
            const chosen = Array.from(s.body.querySelectorAll('[data-pin-path]')).filter((c) => c.checked).map((c) => c.dataset.pinPath);
            s.setBusy(true);
            const r = await prefs.setPinned(chosen);
            s.setBusy(false);
            paintPins();
            if (!r.ok) toasts.show({ level: 'warn', title: 'Pins saved on this Mac only', detail: 'The server said: ' + r.error });
            return true;
          },
        }]
        : [{ label: 'Done', kind: 'primary' }],
    });
  }
  scope.onChange(paintPins);
  prefs.onChange((name) => {
    if (name === 'pinned') loadPins();
  });

  /* badges: real current values only. Office: agents in a run now (feed polls do not count). Security: open high and medium findings. */
  function paintBadges(s) {
    sidebar.setBadge('OFFICE', s.agents && s.agents.known ? s.agents.working || 0 : 0, 'agents working');
    const sec = s.security || {};
    sidebar.setBadge('SECURITY', sec.known && sec.scanned ? (sec.high || 0) + (sec.med || 0) : 0, 'findings to look at');
  }
  status.onChange(paintBadges);
  paintBadges(status.state);

  /* the highlight and running dots live on the sidebar's places */
  const nav = {
    setCurrent(id) {
      sidebar.setCurrent(id);
      startup.setScreen(id);
    },
    setLive(id, on, source) {
      sidebar.setLive(id, on, source);
    },
  };
  chat.onBusy((key, on) => nav.setLive(key, on, 'chat'));

  /* between views nothing of the old one may linger: composer and inspector details included */
  const stageReset = stage.reset.bind(stage);
  stage.reset = () => {
    stageReset();
    composer.reset();
    inspector.inspect(null);
  };

  /* the chrome surface a view sees as ctx.chrome. Calls from a view that has already been left
     (a slow fetch finishing late) do nothing. */
  function makeChrome(id) {
    const mine = () => router.current() === id;
    return {
      setActions: (a) => mine() && stage.setActions(a),
      setSub: (t) => mine() && stage.setSub(t),
      setLive: (on) => nav.setLive(id, Boolean(on), 'screen'),
      setComposer: (o) => mine() && composer.set(o),
      focusComposer: () => mine() && composer.focus(),
      /* T6: details of the selected item in the inspector; inspect(null) clears them */
      inspect: (item, { show = false } = {}) => {
        if (!mine()) return;
        inspector.inspect(item);
        if (show && item) inspector.show('details');
      },
      /* T6: a sheet for anything that needs an answer */
      sheet: (o) => (mine() ? sheets.open(o) : null),
    };
  }

  function renderApproval(container, evt, screenId) {
    const key = chat.keyFor(screenId);
    const entry = approvals.add(key, evt) || approvals.get(evt.id);
    if (!entry) return null;
    const card = approvalCard(entry, (ok) => approvals.decide(entry.id, ok));
    const off = approvals.onChange((e) => {
      if (e.id === entry.id) card.paint();
    });
    container.append(card.el);
    return { ...card, entry, off };
  }

  /* what is drawn over the stage right now: a panel or a toast */
  const overlays = {
    open: () => panels.anyOpen() || sheets.isOpen() || toasts.visibleCount() > 0,
    onChange(fn) {
      const notify = () => fn(overlays.open());
      const offPanels = panels.onChange(notify);
      const offToasts = toasts.onVisible(notify);
      return () => {
        offPanels();
        offToasts();
      };
    },
  };

  const kit = { html, esc, raw, states, ring, icons: { icon, ICON }, flowSvg, md, format, approvalCard, confirmHere };

  const router = createRouter({
    registry,
    stage,
    nav,
    states,
    toasts,
    panels: { notifications: defs.notifications },
    events,
    kit,
    getBuilt: () => api.get('/api/os/screens').then((d) => new Set(d.built || [])),
    ctxFactory: ({ id, root }) =>
      createScreenCtx({
        id,
        root,
        deps: { api, events, chat, approvals, scope, host, prefs, keymap, timers, toasts, chrome: makeChrome(id), overlays, kit, renderApproval },
      }),
  });

  /* a page the server wants in the browser pane while Browser is not showing: keep it for Browser and say so */
  events.on('browser_pane_open', (evt) => {
    if (router.current() === 'BROWSER' || !evt || typeof evt.url !== 'string') return;
    putPaneRequest(evt.url);
    toasts.show({ level: 'info', title: 'A page is waiting in Browser', detail: 'Open Browser to see it: ' + evt.url.slice(0, 120), ttl: 9000 });
  });

  /* finding #183 (owner test): the model opens a page from any chat (browser_open shows the native pane);
     with another screen in front the page floated over that screen and covered its controls. Bring the
     BROWSER screen forward so the page sits in its own region next to the chat column. */
  host.onPaneState((state) => {
    if (state && state.open && router.current() !== 'BROWSER') router.go('browser');
  });

  /* the durable copy of the prefs (text size, pinned projects) follows other windows */
  events.on((e) => e.type === 'state_change' && e.section === 'prefs', () => {
    prefs.hydrate().catch((err) => console.warn('prefs hydrate', err && err.message));
  });

  window.__dmShell = {
    router,
    stats() {
      return {
        screen: router.current(),
        events: { subscriptions: events.count(), sources: events.sources(), connected: events.connected() },
        timers: timers.count(),
        keys: keymap.count(),
        escDepth: keymap.escDepth(),
        chatListeners: chat.listenerCount(),
        approvalListeners: approvals.count(),
        scopeListeners: scope.count(),
        paneListeners: host.paneListenerCount(),
        toasts: toasts.visibleCount(),
        overlayListeners: panels.listenerCount() + toasts.visibleListenerCount(),
        theme: document.documentElement.dataset.theme || '',
        appearance: appearance.source,
        inspector: inspector.shown(),
        sheet: sheets.isOpen(),
        hostKind: host.kind,
      };
    },
    panels: defs,
    prefs,
    appearance,
    appearanceReady,
    inspector,
    sheets,
    commands,
    toasts,
    chat,
  };

  /* service worker: the path is the data-sw attribute of THIS script tag */
  const tag = document.querySelector('script[type="module"][data-sw]');
  if (tag && 'serviceWorker' in navigator && tag.dataset.sw) {
    navigator.serviceWorker.register(tag.dataset.sw).catch((err) => console.warn('service worker not registered:', err && err.message));
  }

  events.start();
  status.start();
  drivingStrip.read();
  loadPins();
  prefs.hydrate().catch((err) => console.warn('prefs hydrate', err && err.message));
  /* after the first screen is up: a real sign-in check, a dialog only if something is missing */
  const started = router.start();
  startup.run().catch((err) => console.warn('startup check', err && err.message));
  return started;
}

boot();
