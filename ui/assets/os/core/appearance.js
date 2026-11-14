/* Applies the macOS appearance to the page (T6 milestone 1, finding #194).

   Inside the Electron app, window.dourmouseShell.appearance hands over what System Settings says
   (light or dark, accent colour, increased contrast, reduced transparency and motion, the dynamic
   system colours) and resends it on every change. In a plain browser the theme follows
   prefers-color-scheme and the colours stay at the fallbacks in tokens.css.

   The contrast guard: Apple's secondary label and white text on its blue accent read at about
   4:1, under the 4.5:1 this app requires, so every colour that carries text is darkened (light
   theme) or lightened (dark theme) here until it passes against the surface it sits on.

   Everything above createAppearance is pure, so node can test it with no window. */

import { contrast, isHex, luminance } from './prefs.js';

export const MIN_TEXT = 4.5;

function toRgb(hex) {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function toHex([r, g, b]) {
  const c = (v) => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, '0');
  return '#' + c(r) + c(g) + c(b);
}

/* a colour with alpha drawn over an opaque background, as one opaque hex */
export function composite(hex, alpha, bg) {
  if (!isHex(hex)) return null;
  const a = Number.isFinite(alpha) ? Math.max(0, Math.min(1, alpha)) : 1;
  if (a >= 1 || !isHex(bg)) return hex.toLowerCase();
  const f = toRgb(hex);
  const b = toRgb(bg);
  return toHex(f.map((v, i) => v * a + b[i] * (1 - a)));
}

/* Moves fg toward black (on a light bg) or white (on a dark bg) in small steps until it reads
   at `min` against bg. Returns the first passing colour, or the extreme if none passes. */
export function ensureContrast(fg, bg, min = MIN_TEXT) {
  if (!isHex(fg) || !isHex(bg)) return fg;
  if (contrast(fg, bg) >= min) return fg.toLowerCase();
  const target = contrast('#000000', bg) >= contrast('#ffffff', bg) ? [0, 0, 0] : [255, 255, 255];
  const start = toRgb(fg);
  for (let t = 0.05; t <= 1.0001; t += 0.05) {
    const c = toHex(start.map((v, i) => v + (target[i] - v) * t));
    if (contrast(c, bg) >= min) return c;
  }
  return toHex(target);
}

/* A fill that white text sits on (primary buttons, badges): darkened until white passes. */
export function accentFill(accent) {
  if (!isHex(accent)) return null;
  return contrast('#ffffff', accent) >= MIN_TEXT ? accent.toLowerCase() : darkenFor('#ffffff', accent);
}

function darkenFor(text, fill) {
  const start = toRgb(fill);
  for (let t = 0.05; t <= 1.0001; t += 0.05) {
    const c = toHex(start.map((v) => v * (1 - t)));
    if (contrast(text, c) >= MIN_TEXT) return c;
  }
  return '#000000';
}

const FALLBACK = {
  light: { content: '#ffffff', accent: '#007aff' },
  dark: { content: '#1e1e1e', accent: '#0a84ff' },
};

function color(colors, key) {
  const c = colors && colors[key];
  return c && isHex(c.hex) ? c : null;
}

/* -> { theme, contrast, reduceMotion, reduceTransparency, vars: { '--tk-...': value } }
   a: what main.js sent (or null in a plain browser), prefersDark: the media query's answer */
export function tokensFor(a, prefersDark = false) {
  const dark = a && typeof a.dark === 'boolean' ? a.dark : Boolean(prefersDark);
  const theme = dark ? 'dark' : 'light';
  const fb = FALLBACK[theme];
  let colors = (a && a.colors) || {};
  const vars = {};
  /* Guard: the neutrals must belong to the theme being drawn. macOS can hand over the colours of the
     other appearance (seen live: an app pinned to light still read dark system colours), and dark
     text colours on a light page would fail every contrast check. Then only the accent and the
     status colours are used and the neutrals stay at the fallbacks. */
  const bgProbe = colors.controlBackground && isHex(colors.controlBackground.hex) ? colors.controlBackground.hex : null;
  if (bgProbe && (luminance(bgProbe) < 0.4) !== dark) {
    const { good, warning, critical } = colors;
    colors = { good, warning, critical };
  }
  const contentC = color(colors, 'controlBackground');
  const content = contentC ? composite(contentC.hex, contentC.alpha, fb.content) : fb.content;
  if (contentC) vars['--tk-bg-content'] = content;
  const win = color(colors, 'windowBackground');
  if (win) vars['--tk-bg-window'] = composite(win.hex, win.alpha, content);

  const label = color(colors, 'label');
  if (label) vars['--tk-text'] = ensureContrast(composite(label.hex, label.alpha, content), content);
  const second = color(colors, 'secondaryLabel');
  if (second) vars['--tk-text-2'] = ensureContrast(composite(second.hex, second.alpha, content), content);
  const ph = color(colors, 'placeholder') || color(colors, 'tertiaryLabel');
  if (ph) vars['--tk-text-3'] = ensureContrast(composite(ph.hex, ph.alpha, content), content);
  const sep = color(colors, 'separator');
  if (sep) vars['--tk-separator'] = composite(sep.hex, sep.alpha, content);
  const link = color(colors, 'link');
  if (link) vars['--tk-link'] = ensureContrast(composite(link.hex, link.alpha, content), content);

  const accent = a && isHex(a.accent) ? a.accent.toLowerCase() : fb.accent;
  vars['--tk-accent'] = accent;
  vars['--tk-accent-fill'] = accentFill(accent);
  vars['--tk-accent-text'] = ensureContrast(accent, content);

  for (const [key, token] of [['good', 'good'], ['warning', 'warning'], ['critical', 'critical']]) {
    const c = color(colors, key);
    if (!c) continue;
    vars['--tk-' + token] = c.hex;
    vars['--tk-' + token + '-text'] = ensureContrast(c.hex, content);
  }
  return {
    theme,
    contrast: a && a.highContrast ? 'more' : 'normal',
    reduceMotion: Boolean(a && a.reduceMotion),
    reduceTransparency: Boolean(a && a.reduceTransparency),
    vars,
  };
}

/* Only a hex colour may reach a custom property (a hostile value cannot add CSS). */
const SAFE = /^#[0-9a-f]{6}$/i;

export function createAppearance({ win = globalThis.window, root } = {}) {
  const el = root || (win && win.document ? win.document.documentElement : null);
  const shell = win && win.dourmouseShell && win.dourmouseShell.appearance ? win.dourmouseShell.appearance : null;
  const mq = win && typeof win.matchMedia === 'function' ? win.matchMedia('(prefers-color-scheme: dark)') : null;
  const mqMotion = win && typeof win.matchMedia === 'function' ? win.matchMedia('(prefers-reduced-motion: reduce)') : null;
  const listeners = new Set();
  let last = null;
  let applied = [];
  let system = null;

  function apply(a) {
    system = a || null;
    const t = tokensFor(system, mq ? mq.matches : false);
    if (el) {
      applied.forEach((name) => el.style.removeProperty(name));
      applied = [];
      Object.entries(t.vars).forEach(([name, value]) => {
        if (typeof value === 'string' && SAFE.test(value)) {
          el.style.setProperty(name, value);
          applied.push(name);
        }
      });
      el.dataset.theme = t.theme;
      el.dataset.contrast = t.contrast;
      const motion = t.reduceMotion || Boolean(mqMotion && mqMotion.matches);
      el.dataset.reduceMotion = motion ? 'true' : 'false';
      el.dataset.reduceTransparency = t.reduceTransparency ? 'true' : 'false';
      el.dataset.appearance = system ? 'system' : 'browser';
    }
    last = t;
    listeners.forEach((fn) => {
      try {
        fn(t);
      } catch (err) {
        console.error(err);
      }
    });
    return t;
  }

  return {
    source: shell ? 'system' : 'browser',
    current: () => last,
    onChange(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    /* paints the fallbacks at once, then asks macOS; resolves with the applied tokens */
    async start() {
      apply(null);
      if (mq && typeof mq.addEventListener === 'function') mq.addEventListener('change', () => apply(system));
      if (!shell) return last;
      shell.onChange((a) => apply(a));
      try {
        const a = await shell.get();
        if (a) apply(a);
      } catch (err) {
        console.warn('appearance: macOS colours not read, using the fallbacks:', err && err.message);
      }
      return last;
    },
    apply,
  };
}
