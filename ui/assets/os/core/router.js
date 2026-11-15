/* Hash routes (#/home, #/security) and the mount/unmount lifecycle with error
   isolation: a screen that fails to import, throws in mount, or is missing
   renders an honest state in the stage and never takes the shell down.

   resolveRoute is pure so the deep-link rules can be tested without a DOM. */

import { bySlug, placeBySlug } from './registry.js';

/* Destinations desktop.py / deeplink.py may append as a hash. */
const ALIASES = { world: 'ATLAS', atlas: 'ATLAS', settings: 'SETTINGS' };
const UNBACKED = new Set(['portfolio', 'markets', 'intelligence', 'command']);

/* Link arguments (plan 1.8, USE1-37): #/office?goal=g12&anchor=steps. At most six simple keys with short
   values survive; anything else is dropped, never passed on. "anchor" is its own field. */
const PARAM_KEY = /^[a-z][a-z0-9_]{0,31}$/;
const ANCHOR = /^[A-Za-z0-9_.-]{1,64}$/;
const MAX_PARAMS = 6;
const MAX_VALUE = 120;

export function parseQuery(text) {
  const query = {};
  let anchor = '';
  let n = 0;
  for (const chunk of String(text || '').split('&')) {
    if (!chunk) continue;
    const eq = chunk.indexOf('=');
    const key = eq < 0 ? chunk : chunk.slice(0, eq);
    let val = '';
    try {
      val = eq < 0 ? '' : decodeURIComponent(chunk.slice(eq + 1));
    } catch (_err) {
      continue;
    }
    if (!PARAM_KEY.test(key) || val.length > MAX_VALUE || /[\u0000-\u001f\u007f]/.test(val)) continue;
    if (key === 'anchor') {
      if (ANCHOR.test(val)) anchor = val;
      continue;
    }
    if (n >= MAX_PARAMS || Object.prototype.hasOwnProperty.call(query, key)) continue;
    query[key] = val;
    n += 1;
  }
  return { query, anchor };
}

export function parseHash(hash) {
  const h = String(hash || '').replace(/^#/, '').replace(/^\/+/, '');
  const q = h.indexOf('?');
  const pathPart = q < 0 ? h : h.slice(0, q);
  const [slug, ...rest] = pathPart.split('/');
  const { query, anchor } = parseQuery(q < 0 ? '' : h.slice(q + 1));
  return { slug: slug.toLowerCase(), rest: rest.filter(Boolean), query, anchor };
}

export function sameParams(a, b) {
  if (!a || !b) return a === b;
  if (a.anchor !== b.anchor || a.path.join('/') !== b.path.join('/')) return false;
  const ka = Object.keys(a.query);
  const kb = Object.keys(b.query);
  return ka.length === kb.length && ka.every((k) => a.query[k] === b.query[k]);
}

/* The hash for a destination with arguments: hashFor('office', { goal: 'g12' }, 'steps') */
export function hashFor(slug, query = {}, anchor = '') {
  const pairs = Object.keys(query || {}).sort().map((k) => k + '=' + encodeURIComponent(query[k]));
  if (anchor) pairs.push('anchor=' + anchor);
  return '#/' + String(slug || 'home').toLowerCase() + (pairs.length ? '?' + pairs.join('&') : '');
}

/* -> { id, openPanel, notice, params: { path, query, anchor } }
   A destination that is not shown (an unknown or unbacked one) falls back to HOME with no arguments. */
export function resolveRoute(hash, current = 'HOME') {
  const { slug, rest, query, anchor } = parseHash(hash);
  const none = { path: [], query: {}, anchor: '' };
  const at = (id) => ({ id, openPanel: null, notice: '', params: { path: rest, query, anchor } });
  const home = (notice) => ({ id: 'HOME', openPanel: null, notice, params: none });
  if (!slug) return home('');
  if (!/^[a-z0-9_-]{1,64}$/.test(slug)) return home('Unknown destination. Showing HOME.');
  if (slug === 'alerts') return { id: current || 'HOME', openPanel: 'notifications', notice: '', params: none };
  if (ALIASES[slug]) return at(ALIASES[slug]);
  /* T6: a place's own slug (#/office, #/library) opens its first view */
  const place = placeBySlug(slug);
  if (place && !bySlug(slug)) return at(place.views[0]);
  const entry = bySlug(slug);
  if (entry) return at(entry.id);
  if (UNBACKED.has(slug)) return home('The ' + slug + ' destination has no screen in the new shell yet. Showing HOME.');
  return home('Unknown destination "' + slug + '". Showing HOME.');
}

/* the server's navigate event and the Electron shell both carry only this much of a hash */
export const NAV_HREF = /^#[A-Za-z0-9_\/#?&=.:@%-]{0,240}$/;

const IMPORT_FAIL = /dynamically imported module|Importing a module script failed|Failed to fetch|error loading/i;

export function createRouter({ registry, win = globalThis.window, stage, nav, ctxFactory, states, toasts, panels, events, kit, getBuilt }) {
  let seq = 0;
  let current = null; /* { id, entry, screen, handle, css } */
  let wanted = { path: [], query: {}, anchor: '' }; /* the arguments of the route being shown */
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

  async function show(id, params = wanted) {
    const my = ++seq;
    const entry = registry.byId(id) || registry.byId('HOME');
    await unmountCurrent();
    if (my !== seq) return;
    nav.setCurrent(entry.id);
    /* T6 (finding #194): the toolbar title is the place, the switcher shows the view, and there is no
       tagline under it (a view may still put a live line there through ctx.chrome.setSub) */
    const place = stage.setViews ? stage.setViews(entry.id) : null;
    const title = place ? place.label : entry.label || entry.id;
    stage.setTitle(title);
    stage.setSub('');
    if (win.document) win.document.title = title;
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
    const handle = ctxFactory({ id: entry.id, root, screen, params });
    let css = null;
    if (screen.css) {
      css = win.document.createElement('link');
      css.rel = 'stylesheet';
      css.href = '/assets/os/screens/' + entry.slug + '/' + entry.slug + '.css';
      css.dataset.screenCss = entry.id;
      win.document.head.append(css);
    }
    current = { id: entry.id, entry, screen, handle, css };
    try {
      await screen.mount(root, handle.ctx);
    } catch (err) {
      if (my === seq) states.error(root, err, { title: entry.id + ' failed to start', retry: () => show(entry.id) });
      console.error(err);
    }
    if (my === seq) {
      stage.focusTitle();
      handle.ctx.params.reveal();
    }
  }

  function route() {
    const r = resolveRoute(win.location.hash, current ? current.id : 'HOME');
    if (r.notice) toasts.show({ level: 'warn', title: 'Navigation', detail: r.notice });
    if (r.openPanel && panels[r.openPanel]) panels[r.openPanel].open();
    const before = wanted;
    wanted = r.params;
    if (current && current.id === r.id) {
      /* the same screen with other arguments: tell it, do not remount it */
      if (!sameParams(before, r.params)) current.handle.setParams(r.params);
      return Promise.resolve();
    }
    return show(r.id, r.params);
  }

  return {
    start() {
      if (started) return route();
      started = true;
      win.addEventListener('hashchange', route);
      if (events) {
        /* The server can ask every window to navigate (allow-listed href). */
        events.on('navigate', (e) => {
          if (typeof e.href === 'string' && NAV_HREF.test(e.href)) win.location.hash = e.href;
        });
      }
      return route();
    },
    /* go('office') or go('office', { goal: 'g12' }, 'steps') */
    go(slug, query = {}, anchor = '') {
      const target = hashFor(slug, query, anchor);
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
