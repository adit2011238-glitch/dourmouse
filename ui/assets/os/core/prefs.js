/* Accent, wallpaper, dim and motion. localStorage is the FIRST-PAINT copy (it
   is readable before any network call, so there is no flash of the default
   amber). The durable, cross-client copy is POST /api/state/prefs {key,value},
   returned inside GET /api/state and broadcast as state_change section
   "prefs", so Electron, pywebview and a browser agree on one accent.

   The accent is applied by CSS custom properties only (--dm-active and
   --dm-active-ink), never by rewriting rules, so every colour-mix() on
   --dm-active follows it. No transition rides on the change: the console
   recorded that a transition on a custom-property change does not settle. */

export const ACCENTS = [
  { id: 'amber', name: 'Amber', hex: '#F59E0B' },
  { id: 'emerald', name: 'Emerald', hex: '#34D399' },
  { id: 'azure', name: 'Azure', hex: '#60A5FA' },
  { id: 'violet', name: 'Violet', hex: '#A78BFA' },
  { id: 'rose', name: 'Rose', hex: '#FB7185' },
];

export const WALLPAPERS = ['aurora', 'slate', 'ember', 'mono'];

/* S2: the Text size setting. The shell's type tokens are overridden per value in shell.css
   (:root[data-text-size=...]), so one attribute scales every token-sized text. 'default' sets none. */
export const TEXT_SIZES = [
  { id: 'small', name: 'Small' },
  { id: 'default', name: 'Default' },
  { id: 'large', name: 'Large' },
];

const LS = {
  accent: 'dm.accent',
  wall: 'dm.wall',
  wallPhoto: 'dm.wallPhoto',
  dim: 'dm.wallDim',
  motion: 'dm.wallAnim',
  textSize: 'dm.textSize',
  dnd: 'dm.os.dnd',
  pinned: 'dm.pinnedProjects',
  alertsSeen: 'dm.os.alertsSeen',
};
export const PREF_KEYS = { accent: 'os.accent', wall: 'os.wall', dim: 'os.dim', motion: 'os.motion', textSize: 'os.textSize', pinned: 'os.pinnedProjects' };

/* T6: pinned projects are project folder paths, at most 12, no repeats, no control characters */
export const PIN_MAX = 12;
export function cleanPins(v) {
  const list = Array.isArray(v) ? v : [];
  const out = [];
  list.forEach((p) => {
    if (typeof p !== 'string' || !p || p.length > 1024 || /[\u0000-\u001f]/.test(p) || out.includes(p)) return;
    if (out.length < PIN_MAX) out.push(p);
  });
  return out;
}

export function isHex(v) {
  return typeof v === 'string' && /^#[0-9a-f]{6}$/i.test(v);
}

function channel(v) {
  const c = v / 255;
  return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
}

export function luminance(hex) {
  const n = parseInt(hex.slice(1), 16);
  return 0.2126 * channel((n >> 16) & 255) + 0.7152 * channel((n >> 8) & 255) + 0.0722 * channel(n & 255);
}

export function contrast(a, b) {
  const la = luminance(a);
  const lb = luminance(b);
  const hi = Math.max(la, lb);
  const lo = Math.min(la, lb);
  return (hi + 0.05) / (lo + 0.05);
}

/* Text drawn ON the accent (primary buttons, the badge). The mockup used a
   fixed dark ink; choose whichever of dark or light reads at 4.5:1. */
export function pickInk(hex) {
  const dark = '#09090B';
  const light = '#FAFAFA';
  return contrast(hex, dark) >= contrast(hex, light) ? dark : light;
}

export function clampDim(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return 35;
  return Math.min(80, Math.max(0, Math.round(n)));
}

export function createPrefs({ storage, api, root } = {}) {
  const ls = storage === undefined ? (() => {
    try {
      return globalThis.localStorage;
    } catch (_err) {
      return null;
    }
  })() : storage;
  const el = root === undefined ? (globalThis.document ? globalThis.document.documentElement : null) : root;
  const listeners = new Set();

  const read = (key) => {
    try {
      return ls ? ls.getItem(key) : null;
    } catch (_err) {
      return null;
    }
  };
  const write = (key, value) => {
    try {
      if (ls) ls.setItem(key, value);
      return true;
    } catch (_err) {
      return false;
    }
  };
  const emit = (name, value) => listeners.forEach((fn) => {
    try {
      fn(name, value);
    } catch (err) {
      console.error(err);
    }
  });

  const prefs = {
    read,
    write,
    remove(key) {
      try {
        if (ls) ls.removeItem(key);
      } catch (_err) {
        /* nothing */
      }
    },
    keys: LS,
    accent: () => (isHex(read(LS.accent)) ? read(LS.accent) : ACCENTS[0].hex),
    wall: () => (WALLPAPERS.includes(read(LS.wall)) ? read(LS.wall) : 'aurora'),
    wallPhoto: () => read(LS.wallPhoto),
    dim: () => (read(LS.dim) === null ? 35 : clampDim(read(LS.dim))),
    motion: () => read(LS.motion) !== 'false',
    textSize: () => (TEXT_SIZES.some((t) => t.id === read(LS.textSize)) ? read(LS.textSize) : 'default'),
    dnd: () => read(LS.dnd) === '1',
    pinned() {
      try {
        return cleanPins(JSON.parse(read(LS.pinned) || '[]'));
      } catch (_err) {
        return [];
      }
    },
    /* saves the pins locally at once and on the server as the durable copy; resolves like save() */
    setPinned(list) {
      const clean = cleanPins(list);
      write(LS.pinned, JSON.stringify(clean));
      emit('pinned', clean);
      if (!api) return Promise.resolve({ ok: true, local: true });
      return api.post('/api/state/prefs', { key: PREF_KEYS.pinned, value: clean }).then(
        () => ({ ok: true, local: true }),
        (err) => ({ ok: false, local: true, error: err && err.message ? err.message : String(err) }),
      );
    },
    setDnd(on) {
      write(LS.dnd, on ? '1' : '0');
      emit('dnd', Boolean(on));
    },
    onChange(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    count: () => listeners.size,

    /* Apply (no save). Used for live preview and first paint. */
    applyAccent(hex) {
      if (!isHex(hex) || !el) return false;
      el.style.setProperty('--dm-active', hex);
      el.style.setProperty('--dm-active-ink', pickInk(hex));
      return true;
    },
    applyDim(n) {
      const d = document.getElementById('walldim');
      if (d) d.style.opacity = String(clampDim(n) / 100);
    },
    applyWall(name, photoUrl) {
      const w = document.getElementById('wall');
      if (!w) return;
      if (name === 'photo') {
        const photo = photoUrl || read(LS.wallPhoto);
        /* only a data:image URL with no quote in it may reach a CSS url("...") */
        if (!photo || !/^data:image\/[a-z0-9.+-]+;base64,[A-Za-z0-9+\/=]+$/i.test(photo)) return;
        w.style.backgroundImage = 'url("' + photo + '")';
        w.style.backgroundSize = 'cover';
        w.style.backgroundPosition = 'center';
        w.dataset.wall = 'photo';
      } else {
        w.style.backgroundImage = '';
        w.dataset.wall = WALLPAPERS.includes(name) ? name : 'aurora';
      }
    },
    applyMotion(on) {
      const w = document.getElementById('wall');
      if (w) w.dataset.animated = String(Boolean(on));
    },
    applyTextSize(id) {
      if (!el || !el.dataset) return false;
      if (!TEXT_SIZES.some((t) => t.id === id) || id === 'default') delete el.dataset.textSize;
      else el.dataset.textSize = id;
      return true;
    },
    /* First paint: everything readable without the network. */
    /* T6 (finding #194): the accent now comes from macOS (core/appearance.js) and the old gradient
       wallpapers are retired, so first paint sets only the text size. applyAccent, applyWall and
       applyDim stay for the wallpaper and appearance settings thread (1.2) to build on. */
    applyAll() {
      prefs.applyTextSize(prefs.textSize());
    },

    /* Save to this browser at once and to the server as the durable copy.
       Resolves { ok, error }: a failed server write is reported, never hidden,
       and the local copy still stands. */
    async save(name, value) {
      const localKey = LS[name];
      if (localKey) write(localKey, String(value));
      emit(name, value);
      if (!api || !PREF_KEYS[name]) return { ok: true, local: true };
      try {
        await api.post('/api/state/prefs', { key: PREF_KEYS[name], value });
        return { ok: true, local: true };
      } catch (err) {
        return { ok: false, local: true, error: err && err.message ? err.message : String(err) };
      }
    },

    /* Reads the durable copy and adopts it when it differs. Returns the list
       of names that changed, or throws the ApiError. */
    async hydrate() {
      const state = await api.get('/api/state');
      const p = (state && state.prefs) || {};
      const changed = [];
      if (isHex(p[PREF_KEYS.accent]) && p[PREF_KEYS.accent].toLowerCase() !== prefs.accent().toLowerCase()) {
        write(LS.accent, p[PREF_KEYS.accent]);
        changed.push('accent');
      }
      if (WALLPAPERS.includes(p[PREF_KEYS.wall]) && p[PREF_KEYS.wall] !== read(LS.wall) && read(LS.wall) !== 'photo') {
        write(LS.wall, p[PREF_KEYS.wall]);
        changed.push('wall');
      }
      if (Number.isFinite(Number(p[PREF_KEYS.dim])) && p[PREF_KEYS.dim] !== null && clampDim(p[PREF_KEYS.dim]) !== prefs.dim()) {
        write(LS.dim, String(clampDim(p[PREF_KEYS.dim])));
        changed.push('dim');
      }
      if (typeof p[PREF_KEYS.motion] === 'boolean' && p[PREF_KEYS.motion] !== prefs.motion()) {
        write(LS.motion, String(p[PREF_KEYS.motion]));
        changed.push('motion');
      }
      if (TEXT_SIZES.some((t) => t.id === p[PREF_KEYS.textSize]) && p[PREF_KEYS.textSize] !== prefs.textSize()) {
        write(LS.textSize, p[PREF_KEYS.textSize]);
        prefs.applyTextSize(p[PREF_KEYS.textSize]);
        changed.push('textSize');
      }
      if (Array.isArray(p[PREF_KEYS.pinned])) {
        const pins = cleanPins(p[PREF_KEYS.pinned]);
        if (JSON.stringify(pins) !== JSON.stringify(prefs.pinned())) {
          write(LS.pinned, JSON.stringify(pins));
          changed.push('pinned');
        }
      }
      changed.forEach((n) => emit(n, prefs[n] ? prefs[n]() : undefined));
      return changed;
    },
  };
  return prefs;
}
