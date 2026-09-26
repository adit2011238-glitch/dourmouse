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
import { createDock } from './chrome/dock.js';
import { createStage } from './chrome/stage.js';
import { createMenubar } from './chrome/menubar.js';
import { createComposer } from './chrome/composer.js';
import { createSpecOverlay } from './chrome/spec-overlay.js';
import { createControlCentre } from './chrome/control-centre.js';
import { createNotifCentre } from './chrome/notif-centre.js';
import { createWallpaperPicker } from './chrome/wallpaper.js';

const $ = (id) => document.getElementById(id);

function boot() {
  const reducedMotion = Boolean(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);

  /* core services. prefs first: the accent and wallpaper are painted from
     localStorage before anything asks the network for them. */
  const api = createApi();
  const prefs = createPrefs({ api });
  prefs.applyAll();
  if (reducedMotion) prefs.applyMotion(false);
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

  /* chrome */
  const panels = createPanels({ keymap });
  const dock = createDock({ root: $('dock'), registry, go: (slug) => router.go(slug), notif: { toggle: () => defs.notifications.toggle() } });
  const sidebar = createSidebar({ root: $('sidebar'), registry, go: (slug) => router.go(slug) });
  const stage = createStage({
    shell: $('shell'), stage: $('stage'), bar: $('stagebar'), titleEl: $('stagetitle'), subEl: $('stagesub'),
    actionsEl: $('stageactions'), body: $('body'), lights: $('lights'), go: (slug) => router.go(slug),
  });
  const composer = createComposer({ root: $('composer') });
  composer.reset();
  const menubar = createMenubar({ root: $('menubar'), status, timers, panels });
  createSpecOverlay({ button: $('specBtn'), legend: $('speclegend'), keymap });

  const notif = createNotifCentre({ root: $('notifcenter'), api, events, toasts, prefs, dock, timers });
  const picker = createWallpaperPicker({ root: $('wallpicker'), prefs, toasts, reducedMotion });
  const cc = createControlCentre({ root: $('controlcenter'), status, prefs, chat, go: (slug) => router.go(slug), toasts, onChange: () => defs.cc.close() });
  const defs = {
    cc: panels.make('cc', { el: $('controlcenter'), trigger: $('ccBtn'), exclusive: ['notifications', 'wallpaper'], onOpen: () => cc.paint() }),
    notifications: panels.make('notifications', { el: $('notifcenter'), trigger: dock.alertsButton, exclusive: ['cc', 'wallpaper'], onOpen: () => notif.onOpen(), onClose: () => notif.onClose() }),
    wallpaper: panels.make('wallpaper', { el: $('wallpicker'), trigger: $('wallBtn'), exclusive: ['cc', 'notifications'], onOpen: () => picker.sync() }),
  };
  $('ccBtn').addEventListener('click', () => defs.cc.toggle());
  $('wallBtn').addEventListener('click', () => defs.wallpaper.toggle());
  picker.sync();

  /* the nav highlight and running dots live in two places (sidebar and dock) */
  const nav = {
    setCurrent(id) {
      sidebar.setCurrent(id);
      dock.setCurrent(id);
    },
    setLive(id, on, source) {
      sidebar.setLive(id, on, source);
      dock.setLive(id, on, source);
    },
  };
  chat.onBusy((key, on) => nav.setLive(key, on, 'chat'));

  /* between screens nothing of the old one may linger, composer included */
  const stageReset = stage.reset.bind(stage);
  stage.reset = () => {
    stageReset();
    composer.reset();
  };

  /* the chrome surface a screen sees as ctx.chrome. Calls from a screen that
     has already been left (a slow fetch finishing late) do nothing. */
  function makeChrome(id) {
    const mine = () => router.current() === id;
    return {
      setActions: (a) => mine() && stage.setActions(a),
      setSub: (t) => mine() && stage.setSub(t),
      setLive: (on) => nav.setLive(id, Boolean(on), 'screen'),
      setComposer: (o) => mine() && composer.set(o),
      focusComposer: () => mine() && composer.focus(),
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
    open: () => panels.anyOpen() || toasts.visibleCount() > 0,
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
    panels: { notifications: defs.notifications, cc: defs.cc },
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

  /* the durable copy of accent, wallpaper and dim follows other windows */
  events.on((e) => e.type === 'state_change' && e.section === 'prefs', () => {
    prefs.hydrate().then((changed) => {
      if (changed.length) picker.sync();
    }).catch((err) => console.warn('prefs hydrate', err && err.message));
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
        hostKind: host.kind,
      };
    },
    panels: defs,
    prefs,
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
  prefs.hydrate().then((changed) => {
    if (changed.length) picker.sync();
  }).catch((err) => console.warn('prefs hydrate', err && err.message));
  return router.start();
}

boot();
