/* BROWSER: pure helpers (no DOM at import time) so node can test them.

   Nothing here fetches or navigates. It decides what an address means, what the
   pane's real state says, where the native view belongs, and what to tell the
   owner about who else is attached. */

export const MAX_ADDRESS = 2000;
export const MIN_WIDTH = 280;
export const MIN_HEIGHT = 240;

/* Layout presets only: the native view is made this wide, so the page's own
   media queries really do reflow. It is not device emulation (no touch, no
   user agent, no pixel ratio). */
export const PRESETS = [
  { id: 'phone', label: 'Phone', width: 380 },
  { id: 'tablet', label: 'Tablet', width: 560 },
  { id: 'desktop', label: 'Desktop', width: 840 },
  { id: 'fill', label: 'Fill', width: null },
];

const REFUSED_SCHEME = /^(javascript|data|file|about|chrome|blob|vbscript|view-source|ftp|mailto|tel|ws|wss|intent):/i;
const EXPLICIT_SCHEME = /^([a-z][a-z0-9+.-]{0,30}):\/\//i;
const LOCAL_HOST = /^(localhost|127\.\d{1,3}\.\d{1,3}\.\d{1,3}|\[::1\])(:\d{1,5})?([/?#]|$)/i;
const BAD_CHARS = /[\s\u0000-\u001f\u007f]/;

/* What the owner typed in the address bar, as an http(s) address or the reason
   it is refused. Typing an address is the owner's own action, but only web
   pages open here: file:, javascript:, data: and the rest are refused. */
export function normalizeAddress(raw) {
  const s = String(raw === undefined || raw === null ? '' : raw).trim();
  if (!s) return { ok: false, reason: 'Type an address first.' };
  if (s.length > MAX_ADDRESS) return { ok: false, reason: 'That address is longer than ' + MAX_ADDRESS + ' characters.' };
  if (BAD_CHARS.test(s)) return { ok: false, reason: 'An address cannot contain spaces or control characters. This is not a search box.' };
  let candidate = s;
  const explicit = EXPLICIT_SCHEME.exec(s);
  if (explicit) {
    const scheme = explicit[1].toLowerCase();
    if (scheme !== 'http' && scheme !== 'https') return { ok: false, reason: scheme + ': addresses are refused. Only http and https pages open here.' };
  } else if (REFUSED_SCHEME.test(s)) {
    const scheme = s.slice(0, s.indexOf(':')).toLowerCase();
    return { ok: false, reason: scheme + ': addresses are refused. Only http and https pages open here.' };
  } else if (s.startsWith('//')) {
    candidate = 'https:' + s;
  } else {
    candidate = (LOCAL_HOST.test(s) ? 'http://' : 'https://') + s;
  }
  let u;
  try {
    u = new URL(candidate);
  } catch (err) {
    return { ok: false, reason: 'That is not a valid web address.' };
  }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') return { ok: false, reason: 'Only http and https pages open here.' };
  if (!u.hostname) return { ok: false, reason: 'That address has no host name.' };
  if (u.username || u.password) return { ok: false, reason: 'Addresses with a user name or password inside them are refused.' };
  return { ok: true, url: u.href };
}

/* The url of a browser_pane_open event. The server accepts an http(s) address
   or a path on this app's own server (a file preview, the media player). */
export function eventTarget(raw) {
  const s = String(raw === undefined || raw === null ? '' : raw).trim();
  if (!s || s.length > MAX_ADDRESS || BAD_CHARS.test(s)) return { ok: false, reason: 'The request carried no usable address.' };
  if (s.startsWith('/') && !s.startsWith('//') && !s.includes('\\')) return { ok: true, url: s, kind: 'app' };
  if (!EXPLICIT_SCHEME.test(s)) return { ok: false, reason: 'The request carried an address that is neither http(s) nor a path on this server.' };
  const n = normalizeAddress(s);
  return n.ok ? { ok: true, url: n.url, kind: 'web' } : n;
}

/* The lock in the address bar: it says what the scheme really is. */
export function lockInfo(url) {
  const u = String(url || '');
  if (!u) return { kind: 'none', label: 'No page loaded' };
  if (/^https:\/\//i.test(u)) return { kind: 'secure', label: 'Encrypted connection (https)' };
  if (/^http:\/\//i.test(u)) return { kind: 'plain', label: 'Not encrypted (http)' };
  if (u.startsWith('/') && !u.startsWith('//')) return { kind: 'app', label: "This app's own page" };
  return { kind: 'other', label: 'Unknown address type' };
}

export function hostOf(url) {
  try {
    return new URL(String(url || '')).host;
  } catch (err) {
    return '';
  }
}

/* Whether an address can be handed to the system browser. */
export function isWebUrl(url) {
  return /^https?:\/\/[^\s/]/i.test(String(url || ''));
}

const CODE_MEANING = {
  '-2': 'The request failed',
  '-3': 'The load was cancelled',
  '-6': 'The file was not found',
  '-7': 'The connection timed out',
  '-21': 'The network changed while loading',
  '-100': 'The connection was closed',
  '-101': 'The connection was reset',
  '-102': 'The server refused the connection',
  '-105': 'The host name could not be found',
  '-106': 'This computer is offline',
  '-109': 'The address cannot be reached',
  '-200': 'The certificate is not valid',
  '-201': 'The certificate date is wrong',
  '-202': 'The certificate is not trusted',
  '-501': 'The connection is not secure',
};

/* The pane's state as main.js reports it, with anything odd made safe. `error`
   only exists once main.js carries did-fail-load's text; an older shell simply
   has none, and then nothing is claimed about a failure. */
export function paneModel(raw) {
  const s = raw && typeof raw === 'object' ? raw : {};
  const err = s.error && typeof s.error === 'object' ? s.error : null;
  return {
    open: s.open === true,
    url: typeof s.url === 'string' ? s.url.slice(0, MAX_ADDRESS) : '',
    title: typeof s.title === 'string' ? s.title.slice(0, 300) : '',
    loading: s.loading === true,
    canGoBack: s.canGoBack === true,
    canGoForward: s.canGoForward === true,
    error: err
      ? {
          code: Number.isFinite(err.code) ? err.code : null,
          description: typeof err.description === 'string' ? err.description.slice(0, 200) : '',
          url: typeof err.url === 'string' ? err.url.slice(0, MAX_ADDRESS) : '',
        }
      : null,
  };
}

/* The page failed to load: the engine's own words, plus a plain reading of the
   code when this file knows one. */
export function failLine(error) {
  if (!error) return '';
  const meaning = error.code !== null && CODE_MEANING[String(error.code)] ? CODE_MEANING[String(error.code)] : '';
  const raw = [error.description, error.code !== null ? 'code ' + error.code : ''].filter(Boolean).join(', ');
  if (meaning && raw) return meaning + ' (' + raw + ').';
  if (meaning) return meaning + '.';
  return raw ? 'The page did not load (' + raw + ').' : 'The page did not load.';
}

export function tabTitle(model) {
  if (!model.url) return 'No page';
  if (model.title && model.title !== model.url) return model.title;
  return hostOf(model.url) || (model.url ? model.url : 'No page');
}

/* Where the native view belongs: the page area, clipped to the stage body it
   scrolls inside. Null when nothing of it is visible (a collapsed stage, a
   hidden screen, a zero-size box). Both rects are viewport rects. */
export function viewBounds(page, clip) {
  if (!page) return null;
  const c = clip || { left: -Infinity, top: -Infinity, right: Infinity, bottom: Infinity };
  const left = Math.max(page.left, c.left);
  const top = Math.max(page.top, c.top);
  const right = Math.min(page.right, c.right);
  const bottom = Math.min(page.bottom, c.bottom);
  const width = Math.floor(right) - Math.ceil(left);
  const height = Math.floor(bottom) - Math.ceil(top);
  if (!(width >= 2 && height >= 2)) return null;
  return { x: Math.ceil(left), y: Math.ceil(top), width, height };
}

export function sameBounds(a, b) {
  return Boolean(a && b && a.x === b.x && a.y === b.y && a.width === b.width && a.height === b.height);
}

export function clampWidth(w, avail) {
  const n = Number(w);
  const max = Math.max(MIN_WIDTH, Number.isFinite(avail) ? Math.floor(avail) : 4000);
  if (!Number.isFinite(n)) return max;
  return Math.max(MIN_WIDTH, Math.min(max, Math.round(n)));
}

export function clampHeight(h, max) {
  const n = Number(h);
  const top = Math.max(MIN_HEIGHT, Number.isFinite(max) ? Math.floor(max) : 2000);
  if (!Number.isFinite(n)) return top;
  return Math.max(MIN_HEIGHT, Math.min(top, Math.round(n)));
}

/* Which preset (if any) a stored width means. */
export function presetForWidth(w) {
  if (w === null || w === undefined) return 'fill';
  const hit = PRESETS.find((p) => p.width === w);
  return hit ? hit.id : '';
}

/* The remembered width: 'fill', or a whole number of pixels. Anything else is
   'fill' (a bad stored value never breaks the layout). */
export function parseStoredWidth(v) {
  if (v === null || v === undefined || v === '' || v === 'fill') return null;
  const n = Number(v);
  return Number.isInteger(n) && n >= MIN_WIDTH && n <= 4000 ? n : null;
}

export function parseStoredHeight(v) {
  if (v === null || v === undefined || v === '' || v === 'fill') return null;
  const n = Number(v);
  return Number.isInteger(n) && n >= MIN_HEIGHT && n <= 4000 ? n : null;
}

function bare(u) {
  return String(u || '').replace(/#.*$/, '').replace(/\/+$/, '');
}

export function sameAddress(a, b) {
  return bare(a) === bare(b);
}

/* What the strip under the page says about the browser agent. `att` is the
   answer of GET /api/os/browser/attached. It names the agent's own page, so
   "this pane" is only claimed inside Electron, where the agent drives the
   pane, and only when both addresses agree. Nothing here says which agent. */
export function watchLine(att, paneUrl, hostKind, agoFn) {
  if (!att) return { tone: '', text: '' };
  const age = agoFn && att.last && att.last.at ? agoFn(att.last.at) : '';
  const last = att.last && att.last.text
    ? ' Last action' + (age ? ', ' + (age === 'now' ? 'just now' : age + ' ago') : '') + ': ' + att.last.kind + ', ' + att.last.text
    : '';
  if (!att.attached) {
    if (att.launch_error) return { tone: 'warn', text: 'The browser agent is not attached. Its last launch failed: ' + att.launch_error };
    if (!att.engine_ready) return { tone: 'warn', text: 'The browser agent cannot attach here: ' + (att.engine || 'no browser engine') + '.' };
    return { tone: '', text: 'The browser agent is not attached to any page right now.' + last };
  }
  if (hostKind === 'electron' && sameAddress(att.page_url, paneUrl)) {
    return { tone: 'live', text: 'The browser agent is attached to this pane and can read and drive the page you are looking at.' + last };
  }
  if (hostKind === 'electron') {
    return { tone: 'live', text: 'The browser agent has a page open at ' + (att.page_url || 'a blank address') + ', which is not the address shown here.' + last };
  }
  return { tone: '', text: 'The browser agent has its own page open at ' + (att.page_url || 'a blank address') + '. It is not this window.' + last };
}

/* The three footnotes, worded for what this window really is. */
export function footnotes(kind) {
  const electron = kind === 'electron';
  return [
    {
      head: 'Adjustable',
      text: 'drag the edge or pick a preset, the page reflows',
      spec: 'Drag the right or bottom edge, or pick a preset, to see a page at another size. The page reflows because the view really is that size. This is layout only, not device emulation.',
    },
    electron
      ? {
          head: 'Shared with the AI',
          text: 'one surface, driven over CDP',
          spec: 'You browse and the browser agent drives the SAME native view through the local bridge and reads it over CDP. There is one page, not a copy.',
        }
      : {
          head: 'Not shared',
          text: 'the agent uses its own Chrome',
          spec: 'This window is not the Electron app, so it has no native view to share. The browser agent drives its own Chrome, and this page is a separate, proxied view.',
        },
    electron
      ? {
          head: 'No proxy',
          text: 'a real engine, cookies and logins work',
          spec: 'The native view is a top-level browsing context, so X-Frame-Options and frame-ancestors do not apply to it and nothing is rewritten.',
        }
      : {
          head: 'Proxied',
          text: 'no logins, links inside do not load here',
          spec: 'This server fetches the page for you and serves it from its own origin inside a sandbox. Cookies and logins do not reach it, only HTML is fetched, and some sites still fail.',
        },
  ];
}

/* The address list of the proxied view. A proxied page cannot report its own
   history to this page, so back and forward walk the addresses the owner
   opened in this window. */
export function makeHistory(cap = 50) {
  let items = [];
  let at = -1;
  return {
    push(url) {
      if (at >= 0 && items[at] === url) return;
      items = items.slice(0, at + 1);
      items.push(url);
      if (items.length > cap) items = items.slice(items.length - cap);
      at = items.length - 1;
    },
    back() {
      if (at > 0) at -= 1;
      return items[at] || '';
    },
    forward() {
      if (at < items.length - 1) at += 1;
      return items[at] || '';
    },
    current: () => items[at] || '',
    canBack: () => at > 0,
    canForward: () => at >= 0 && at < items.length - 1,
    size: () => items.length,
    clear() {
      items = [];
      at = -1;
    },
  };
}

/* ---------------- Phase B1: tabs, find, zoom, downloads, history ---------------- */

const FAVICON = /^data:image\/(png|jpeg|gif|webp|x-icon|vnd\.microsoft\.icon);base64,[A-Za-z0-9+/=]{1,40000}$/;

/* A favicon the shell sent as a data address, or '' when it is anything else. The page
   area never sets an image source from a string that did not pass this. */
export function safeFavicon(v) {
  return typeof v === 'string' && FAVICON.test(v) ? v : '';
}

export function tabLabel(tab) {
  if (!tab || !tab.url) return 'New tab';
  if (tab.title && tab.title !== tab.url) return tab.title;
  return hostOf(tab.url) || tab.url;
}

/* The tab list and per-tab extras in the pane's state, with anything odd made safe.
   paneModel above reads the active tab only and stays as it was. */
export function tabsModel(raw) {
  const s = raw && typeof raw === 'object' ? raw : {};
  const list = Array.isArray(s.tabs) ? s.tabs : [];
  const tabs = list
    .filter((t) => t && typeof t === 'object' && Number.isInteger(t.id))
    .slice(0, 60)
    .map((t) => ({
      id: t.id,
      url: typeof t.url === 'string' ? t.url.slice(0, MAX_ADDRESS) : '',
      title: typeof t.title === 'string' ? t.title.slice(0, 300) : '',
      favicon: safeFavicon(t.favicon),
      loading: t.loading === true,
      active: t.active === true,
      audible: t.audible === true,
    }));
  const f = s.find && typeof s.find === 'object' ? s.find : null;
  const zoom = typeof s.zoom === 'number' && Number.isFinite(s.zoom) && s.zoom >= 0.25 && s.zoom <= 5 ? s.zoom : 1;
  return {
    tabs,
    activeId: Number.isInteger(s.activeTab) ? s.activeTab : 0,
    zoom,
    find: f ? { text: typeof f.text === 'string' ? f.text.slice(0, 200) : '', active: Number.isInteger(f.active) ? f.active : 0, matches: Number.isInteger(f.matches) ? f.matches : 0 } : null,
    closed: Number.isInteger(s.closedTabs) && s.closedTabs > 0 ? s.closedTabs : 0,
    blocked: Number.isInteger(s.blockedPopups) && s.blockedPopups > 0 ? s.blockedPopups : 0,
    /* the profile in use (B3); an older shell sends none, which is the default profile */
    profile: typeof s.profile === 'string' && /^[a-z0-9][a-z0-9_-]{0,23}$/.test(s.profile) ? s.profile : 'default',
  };
}

/* One string that changes when anything the strip draws changes, so it is rebuilt only then. */
export function tabsKey(model, agentUrl) {
  return model.tabs.map((t) => [t.id, t.active ? 1 : 0, t.loading ? 1 : 0, t.audible ? 1 : 0, t.title, t.url, t.favicon.length, agentUrl && sameAddress(t.url, agentUrl) ? 1 : 0].join('|')).join('~');
}

export function zoomLabel(z) {
  return Math.round((Number.isFinite(z) ? z : 1) * 100) + '%';
}

export function findLabel(find, typed) {
  if (!typed) return '';
  if (!find || find.text !== typed) return '';
  if (find.matches === 0) return 'No matches';
  return find.active + ' of ' + find.matches;
}

export function formatBytes(n) {
  const v = Number(n);
  if (!Number.isFinite(v) || v < 0) return '';
  if (v < 1024) return v + ' B';
  const units = ['KB', 'MB', 'GB', 'TB'];
  let x = v / 1024;
  let i = 0;
  while (x >= 1024 && i < units.length - 1) {
    x /= 1024;
    i += 1;
  }
  return (x >= 100 ? x.toFixed(0) : x.toFixed(1)) + ' ' + units[i];
}

const DL_STATES = new Set(['progressing', 'completed', 'cancelled', 'interrupted']);

/* The downloads list as the shell sends it. */
export function downloadsModel(raw) {
  const list = Array.isArray(raw) ? raw : [];
  return list
    .filter((d) => d && typeof d === 'object' && typeof d.id === 'string' && typeof d.filename === 'string')
    .slice(0, 100)
    .map((d) => ({
      id: d.id,
      filename: d.filename.slice(0, 200),
      url: typeof d.url === 'string' ? d.url.slice(0, 500) : '',
      state: DL_STATES.has(d.state) ? d.state : 'interrupted',
      paused: d.paused === true,
      received: Number.isFinite(d.received) ? d.received : 0,
      total: Number.isFinite(d.total) && d.total > 0 ? d.total : 0,
      percent: Number.isFinite(d.percent) ? d.percent : null,
      error: typeof d.error === 'string' ? d.error.slice(0, 200) : '',
      openable: d.openable === true,
      quarantined: d.quarantined === true ? true : d.quarantined === false ? false : null,
    }));
}

export function activeDownloads(list) {
  return list.filter((d) => d.state === 'progressing').length;
}

/* The one line under a download's name. */
export function downloadLine(d) {
  if (d.state === 'progressing') {
    const have = formatBytes(d.received);
    const of = d.total ? ' of ' + formatBytes(d.total) : '';
    const pct = d.percent !== null ? ', ' + d.percent + '%' : '';
    return (d.paused ? 'Paused, ' : '') + have + of + pct;
  }
  if (d.state === 'completed') {
    const size = d.total || d.received ? formatBytes(d.total || d.received) : '';
    const flag = d.quarantined === true ? 'flagged for Gatekeeper' : d.quarantined === false ? 'not flagged for Gatekeeper' : '';
    return ['Done', size, flag].filter(Boolean).join(', ');
  }
  if (d.state === 'cancelled') return 'Cancelled';
  return 'Did not finish' + (d.error ? ': ' + d.error : '');
}

/* History grouped by day, newest first: "Today", "Yesterday", then the date. */
export function groupHistory(list, now = Date.now()) {
  const day = (ms) => {
    const d = new Date(ms);
    return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  };
  const today = day(now);
  const groups = [];
  const byKey = new Map();
  for (const e of Array.isArray(list) ? list : []) {
    if (!e || typeof e.url !== 'string' || !Number.isFinite(e.at)) continue;
    const k = day(e.at);
    let g = byKey.get(k);
    if (!g) {
      const diff = Math.round((today - k) / 86400000);
      g = { label: diff === 0 ? 'Today' : diff === 1 ? 'Yesterday' : new Date(k).toDateString(), items: [] };
      byKey.set(k, g);
      groups.push(g);
    }
    g.items.push({ id: String(e.id || ''), url: e.url.slice(0, MAX_ADDRESS), title: typeof e.title === 'string' ? e.title.slice(0, 300) : '', at: e.at });
  }
  return groups;
}

export function bookmarksModel(raw) {
  return (Array.isArray(raw) ? raw : [])
    .filter((b) => b && typeof b.id === 'string' && typeof b.url === 'string' && isWebUrl(b.url))
    .slice(0, 1000)
    .map((b) => ({ id: b.id, url: b.url.slice(0, MAX_ADDRESS), title: typeof b.title === 'string' && b.title ? b.title.slice(0, 300) : hostOf(b.url) || b.url }));
}

export function bookmarkFor(list, url) {
  return list.find((b) => b.url === url) || null;
}
