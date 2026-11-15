/* The wallpaper (plan 1.2, finding 197): a photo Adit picked from their own files, drawn behind the
   window. It shows through the sidebar (blurred) and faintly behind the content. Reading surfaces
   (content, inspector, sidebar) are the theme's own colour laid over the photo at an opacity that is
   worked out here so the weakest text colour still reads at 4.5:1 against the WORST pixel the photo
   could hold (black under dark text, white under light text). That guarantee does not depend on
   which photo it is.

   Everything above createWallpaper is pure, so node can test it with no window. */

import { contrast, isHex } from './prefs.js';
import { composite, MIN_TEXT } from './appearance.js';
import { placeOf } from './registry.js';
import { resolveRoute } from './router.js';

export const DEFAULT_PLACES = ['HOME', 'PROJECTS', 'OFFICE'];
export const FLOOR = { content: 0.9, inspector: 0.9, sidebar: 0.55 };

/* '#rrggbb', 'rgb(r, g, b)' or 'rgba(r, g, b, a)' -> { hex, alpha }, or null */
export function parseColor(value) {
  const s = String(value || '').trim().toLowerCase();
  if (isHex(s)) return { hex: s, alpha: 1 };
  const m = /^rgba?\(\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*(?:[,/]\s*([\d.]+%?)\s*)?\)$/.exec(s);
  if (!m) return null;
  const c = (v) => Math.max(0, Math.min(255, Number(v))).toString(16).padStart(2, '0');
  let alpha = 1;
  if (m[4] !== undefined) alpha = m[4].endsWith('%') ? parseFloat(m[4]) / 100 : parseFloat(m[4]);
  return { hex: '#' + c(m[1]) + c(m[2]) + c(m[3]), alpha: Number.isFinite(alpha) ? Math.max(0, Math.min(1, alpha)) : 1 };
}

/* the opaque colour of a (possibly translucent) css colour drawn over an opaque background */
export function resolveColor(value, over) {
  const p = parseColor(value);
  return p ? composite(p.hex, p.alpha, over) : null;
}

/* The smallest opacity, from `floor` up in steps of 1%, at which `text` still reads at `min`
   against `bg` laid over the worst photo pixel. 1 when nothing below it passes (bg alone is the
   theme's own pairing, which appearance.js already guarantees). */
export function surfaceAlpha({ text, bg, floor = 0.9, min = MIN_TEXT }) {
  if (!isHex(text) || !isHex(bg)) return 1;
  const worst = contrast(text, '#000000') < contrast(text, '#ffffff') ? '#000000' : '#ffffff';
  const start = Math.max(0, Math.min(1, floor));
  for (let a = Math.ceil(start * 100); a <= 100; a += 1) {
    const over = composite(bg, a / 100, worst);
    if (contrast(text, over) >= min) return a / 100;
  }
  return 1;
}

export function placeIsOn(cfg, placeId) {
  return Boolean(cfg && cfg.mode !== 'off' && Array.isArray(cfg.places) && cfg.places.includes(placeId));
}

/* which of light, dark or rotation to draw: rotate mode uses the rotation; single uses the
   photo for the theme, or the other theme's photo when this one has none */
export function pickSlot(summary, theme) {
  if (!summary || !summary.config || summary.config.mode === 'off') return null;
  if (summary.config.mode === 'rotate') return summary.rotation > 0 ? 'rotation' : null;
  const mine = theme === 'dark' ? 'dark' : 'light';
  const other = mine === 'dark' ? 'light' : 'dark';
  if (summary[mine]) return mine;
  return summary[other] ? other : null;
}

const SAFE_URL = /^data:image\/jpeg;base64,[A-Za-z0-9+/=]+$/;
const OWN_VARS = ['--tk-wall-content', '--tk-wall-inspector', '--tk-wall-sidebar'];

export function createWallpaper({ api, appearance, win = globalThis.window, root } = {}) {
  const doc = win && win.document ? win.document : null;
  const el = root || (doc ? doc.documentElement : null);
  const layer = doc ? doc.getElementById('wall') : null;
  const listeners = new Set();
  let summary = null;
  let cache = { key: '', url: null };
  let seq = 0;

  const read = (name) => (win && win.getComputedStyle && el ? win.getComputedStyle(el).getPropertyValue(name).trim() : '');
  const emit = () => listeners.forEach((fn) => {
    try {
      fn(summary);
    } catch (err) {
      console.error(err);
    }
  });

  function clear() {
    if (layer) {
      layer.style.backgroundImage = '';
      layer.dataset.wall = 'none';
    }
    if (el) {
      delete el.dataset.wallpaper;
      OWN_VARS.forEach((n) => el.style.removeProperty(n));
    }
  }

  /* the surface colours at the opacities that keep text readable over any photo */
  function tint() {
    const bgContent = parseColor(read('--tk-bg-content'));
    if (!bgContent) return false;
    const solid = bgContent.hex;
    const text = resolveColor(read('--tk-text-3'), solid) || resolveColor(read('--tk-text-2'), solid);
    const surfaces = [
      ['--tk-wall-content', solid, FLOOR.content],
      ['--tk-wall-inspector', (parseColor(read('--tk-bg-inspector')) || bgContent).hex, FLOOR.inspector],
      ['--tk-wall-sidebar', (parseColor(read('--tk-bg-sidebar')) || bgContent).hex, FLOOR.sidebar, '--tk-text-2'],
    ];
    /* the sidebar holds labels only (text and secondary text); tertiary grey lives in the content */
    surfaces.forEach(([name, bg, floor, textVar = '--tk-text-3']) => {
      const t = resolveColor(read(textVar), bg) || text;
      const a = surfaceAlpha({ text: t, bg, floor });
      const h = bg.slice(1);
      el.style.setProperty(name, 'rgba(' + parseInt(h.slice(0, 2), 16) + ',' + parseInt(h.slice(2, 4), 16) + ',' + parseInt(h.slice(4, 6), 16) + ',' + a + ')');
    });
    return true;
  }

  function currentPlace() {
    const hash = win && win.location ? win.location.hash : '';
    const route = resolveRoute(hash, 'HOME');
    const p = placeOf(route.id);
    return p ? p.id : 'HOME';
  }

  async function imageFor(slot) {
    const key = slot + ':' + (summary ? summary.version : '') + ':' + new Date().toDateString();
    if (cache.key === key) return cache.url;
    const r = await api.get('/api/os/wallpaper/image?slot=' + encodeURIComponent(slot));
    const url = r && typeof r.data_url === 'string' && SAFE_URL.test(r.data_url) ? r.data_url : null;
    cache = { key, url };
    return url;
  }

  async function paint() {
    const mine = ++seq;
    if (!summary || !el || !layer) return clear();
    const theme = el.dataset.theme === 'dark' ? 'dark' : 'light';
    const off = el.dataset.reduceTransparency === 'true' || !placeIsOn(summary.config, currentPlace());
    const slot = off ? null : pickSlot(summary, theme);
    if (!slot) return clear();
    let url = null;
    try {
      url = await imageFor(slot);
    } catch (err) {
      console.warn('wallpaper: photo not read:', err && err.message);
    }
    if (mine !== seq) return undefined;
    if (!url || !tint()) return clear();
    layer.style.backgroundImage = 'url("' + url + '")';
    layer.dataset.wall = 'photo';
    el.dataset.wallpaper = 'on';
    return undefined;
  }

  const api2 = {
    summary: () => summary,
    onChange(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    paint,
    async load() {
      const s = await api.get('/api/os/wallpaper');
      summary = { ...s, version: Date.now() };
      emit();
      await paint();
      return summary;
    },
    async adopt(next) {
      summary = { ...next, version: Date.now() };
      emit();
      await paint();
      return summary;
    },
    async setMode(mode) {
      return api2.adopt(await api.post('/api/os/wallpaper/config', { mode }));
    },
    async setPlaces(places) {
      return api2.adopt(await api.post('/api/os/wallpaper/config', { places }));
    },
    async remove(slot) {
      return api2.adopt(await api.post('/api/os/wallpaper/remove', { slot }));
    },
    /* file: a File from <input type="file">; sent as base64, the server checks and shrinks it */
    async upload(slot, file) {
      const buf = new Uint8Array(await file.arrayBuffer());
      let bin = '';
      for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
      return api2.adopt(await api.post('/api/os/wallpaper/upload', { slot, data: win.btoa(bin) }));
    },
    async preview(slot) {
      const r = await api.get('/api/os/wallpaper/image?slot=' + encodeURIComponent(slot));
      return r && typeof r.data_url === 'string' && SAFE_URL.test(r.data_url) ? r.data_url : null;
    },
    start() {
      if (appearance && typeof appearance.onChange === 'function') appearance.onChange(() => paint());
      if (win && win.addEventListener) win.addEventListener('hashchange', () => paint());
      return api2.load().catch((err) => {
        console.warn('wallpaper: settings not read, none shown:', err && err.message);
        clear();
      });
    },
  };
  return api2;
}
