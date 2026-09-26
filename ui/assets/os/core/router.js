/* Hash routes (#/home, #/security) and the mount/unmount lifecycle with error
   isolation: a screen that fails to import, throws in mount, or is missing
   renders an honest state in the stage and never takes the shell down.

   resolveRoute is pure so the deep-link rules can be tested without a DOM. */

import { bySlug } from './registry.js';

/* Destinations desktop.py / deeplink.py may append as a hash. */
const ALIASES = { world: 'ATLAS', atlas: 'ATLAS', settings: 'SETTINGS' };
const UNBACKED = new Set(['portfolio', 'markets', 'intelligence', 'command']);

export function parseHash(hash) {
  const h = String(hash || '').replace(/^#/, '').replace(/^\/+/, '');
  const [slug, ...rest] = h.split('/');
  return { slug: slug.toLowerCase(), rest: rest.filter(Boolean) };
}

/* -> { id, openPanel, notice } */
export function resolveRoute(hash, current = 'HOME') {
  const { slug } = parseHash(hash);
  if (!slug) return { id: 'HOME', openPanel: null, notice: '' };
  if (!/^[a-z0-9_-]{1,64}$/.test(slug)) {
    return { id: 'HOME', openPanel: null, notice: 'Unknown destination. Showing HOME.' };
  }
  if (slug === 'alerts') return { id: current || 'HOME', openPanel: 'notifications', notice: '' };
  if (ALIASES[slug]) return { id: ALIASES[slug], openPanel: null, notice: '' };
  const entry = bySlug(slug);
  if (entry) return { id: entry.id, openPanel: null, notice: '' };
  if (UNBACKED.has(slug)) {
    return { id: 'HOME', openPanel: null, notice: 'The ' + slug + ' destination has no screen in the new shell yet. Showing HOME.' };
  }
  return { id: 'HOME', openPanel: null, notice: 'Unknown destination "' + slug + '". Showing HOME.' };
}

const IMPORT_FAIL = /dynamically imported module|Importing a module script failed|Failed to fetch|error loading/i;

export function createRouter({ registry, win = globalThis.window, stage, nav, ctxFactory, states, toasts, panels, events, kit, getBuilt }) {
  let seq = 0;
  let current = null; /* { id, entry, screen, handle, css } */
  let started = false;

  function slugUrl(entry) {
    return '/assets/os/screens/' + entry.slug + '/index.js';
  }

  async function classifyFailure(entry, err) {
    const isImport = err && (err.name === 'TypeError' || err.name === 'SyntaxError') && IMPORT_FAIL.test(String(err.message || ''));
    if (!isImport) return { kind: 'error', err };
    try {
      const res = await win.fetch(slugUrl(entry), { cache: 'no-store' });
      if (res.status === 404) return { kind: 'missing' };
      if (res.ok) return { kind: 'error', err };
      return { kind: 'error', err: new Error('The screen module answered HTTP ' + res.status) };
    } catch (_e) {
      return { kind: 'offline' };
    }
  }

  async function unmountCurrent() {
    const cur = current;
    current = null;
    if (!cur) return;
    try {
      if (cur.screen && typeof cur.screen.unmount === 'function') await cur.screen.unmount(cur.handle.ctx);
    } catch (err) {
      console.error(err);
    }
    cur.handle.dispose();
    if (cur.css) cur.css.remove();
    nav.setLive(cur.id, false, 'screen');
    stage.reset();
  }

  async function show(id) {
    const my = ++seq;
    const entry = registry.byId(id) || registry.byId('HOME');
    await unmountCurrent();
    if (my !== seq) return;
    nav.setCurrent(entry.id);
    stage.setTitle(entry.id);
    stage.setSub(entry.sub || '');
    const root = stage.newRoot(entry.id);
    states.loading(root, 'Loading ' + entry.id);

    /* Ask which screen folders exist BEFORE importing: importing a module
       that is not there makes the browser log a 404 as a console error. If the
       question cannot be answered, import anyway and classify a failure. */
    if (getBuilt) {
      let built = null;
      try {
        built = await getBuilt();
      } catch (_err) {
        built = null;
      }
      if (my !== seq) return;
      if (built && !built.has(entry.slug)) {
        states.unavailable(root, entry.id + ' is not built yet.', {
          detail: 'There is no screen folder for ' + entry.id + ' in this build.',
        });
        stage.focusTitle();
        return;
      }
    }

    let mod;
    try {
      mod = await entry.load();
    } catch (err) {
      if (my !== seq) return;
      const f = await classifyFailure(entry, err);
      if (my !== seq) return;
      if (f.kind === 'missing') {
        states.unavailable(root, entry.id + ' is not built yet.', {
          detail: 'The screen module ' + slugUrl(entry) + ' does not exist in this build.',
        });
      } else if (f.kind === 'offline') {
        states.unavailable(root, entry.id + ' is unavailable offline.', {
          detail: 'Its module was never fetched while the server was reachable.',
          retry: () => show(entry.id),
        });
      } else {
        states.error(root, f.err, { title: entry.id + ' failed to load', retry: () => show(entry.id) });
      }
      stage.focusTitle();
      return;
    }
    if (my !== seq) return;

    const screen = mod && mod.default;
    if (!screen || typeof screen.mount !== 'function') {
      states.error(root, new Error('The ' + entry.id + ' module has no default export with a mount() function.'), { title: entry.id + ' is not a valid screen' });
      stage.focusTitle();
      return;
    }
    const handle = ctxFactory({ id: entry.id, root, screen });
    let css = null;
    if (screen.css) {
      css = win.document.createElement('link');
      css.rel = 'stylesheet';
      css.href = '/assets/os/screens/' + entry.slug + '/' + entry.slug + '.css';
      css.dataset.screenCss = entry.id;
      win.document.head.append(css);
    }
    current = { id: entry.id, entry, screen, handle, css };
    if (screen.sub) stage.setSub(screen.sub);
    try {
      await screen.mount(root, handle.ctx);
    } catch (err) {
      if (my === seq) states.error(root, err, { title: entry.id + ' failed to start', retry: () => show(entry.id) });
      console.error(err);
    }
    if (my === seq) stage.focusTitle();
  }

  function route() {
    const r = resolveRoute(win.location.hash, current ? current.id : 'HOME');
    if (r.notice) toasts.show({ level: 'warn', title: 'Navigation', detail: r.notice });
    if (r.openPanel && panels[r.openPanel]) panels[r.openPanel].open();
    if (current && current.id === r.id) return Promise.resolve();
    return show(r.id);
  }

  return {
    start() {
      if (started) return route();
      started = true;
      win.addEventListener('hashchange', route);
      if (events) {
        /* The server can ask every window to navigate (allow-listed href). */
        events.on('navigate', (e) => {
          if (typeof e.href === 'string' && /^#[A-Za-z0-9_\/#-]{0,120}$/.test(e.href)) win.location.hash = e.href;
        });
      }
      return route();
    },
    go(slug) {
      const target = '#/' + String(slug || 'home').toLowerCase();
      if (win.location.hash === target) return route();
      win.location.hash = target;
      return Promise.resolve();
    },
    current: () => (current ? current.id : null),
    currentCtx: () => (current ? current.handle.ctx : null),
    counts: () => (current ? current.handle.counts() : { timers: 0 }),
    /* menubar / keyboard "refresh": re-read what this screen shows */
    async refresh(reason = 'manual') {
      if (current && current.screen && typeof current.screen.refresh === 'function') {
        try {
          await current.screen.refresh(current.handle.ctx, reason);
        } catch (err) {
          console.error(err);
        }
      }
    },
    /* the event stream reconnected: refresh screens that did not subscribe themselves */
    onResync() {
      if (current && current.handle.wantsResync()) return this.refresh('resync');
      return Promise.resolve();
    },
  };
}
