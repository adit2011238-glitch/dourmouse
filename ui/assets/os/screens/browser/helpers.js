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
