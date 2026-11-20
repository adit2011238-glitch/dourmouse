/* Settings > Ask panel (plan 4.9, PL-04): the shortcut, close on blur, the starting mode and how long a
   conversation is kept. The server checks every value (dourmouse/ask_panel.py) and says why it refused a
   shortcut; in the app the window then registers the new shortcut and says if another app already has it. */

import { isAbort } from '../../core/api.js';

const MODE_NAMES = { ask: 'Ask', note: 'Note', remind: 'Remind', do: 'Do' };
const KEYS = { ' ': 'Space', ArrowUp: 'Up', ArrowDown: 'Down', ArrowLeft: 'Left', ArrowRight: 'Right', Enter: 'Return', Escape: 'Escape', Tab: 'Tab' };

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

/* an Electron accelerator from a key press, or '' while only modifiers are down */
export function chordFromEvent(e) {
  if (['Meta', 'Control', 'Alt', 'Shift'].includes(e.key)) return '';
  if (!(e.metaKey || e.ctrlKey || e.altKey)) return '';
  const key = KEYS[e.key] || (e.key.length === 1 ? e.key.toUpperCase() : /^F\d{1,2}$/.test(e.key) ? e.key : '');
  if (!key) return '';
  const mods = [e.metaKey && 'Command', e.ctrlKey && 'Control', e.altKey && 'Alt', e.shiftKey && 'Shift'].filter(Boolean);
  return mods.concat(key).join('+');
}

export function mountAskPanel(root, ctx) {
  const shell = window.dourmouseShell && window.dourmouseShell.ask ? window.dourmouseShell.ask : null;
  const st = { data: null, line: '', tone: '' };

  async function load() {
    try {
      st.data = await ctx.api.get('/api/os/ask/settings');
    } catch (err) {
      if (isAbort(err)) return;
      root.replaceChildren(el('div', 'muted set-err', 'The Ask panel settings could not be read: ' + err.message));
      return;
    }
    paint();
  }

  async function save(change) {
    try {
      const r = await ctx.api.post('/api/os/ask/settings', change);
      st.data = { ...st.data, settings: r.settings, hotkey_label: r.hotkey_label };
      st.line = 'Saved.';
      st.tone = '';
      if (shell) {
        const applied = await shell.apply();
        if (applied && applied.ok === false) {
          st.line = applied.error;
          st.tone = 'error';
        }
      } else {
        st.line = 'Saved. The shortcut starts working in the app window.';
      }
    } catch (err) {
      st.line = err.message;
      st.tone = 'error';
    }
    paint();
  }

  function row(label, control) {
    const r = el('div', 'kv');
    r.append(el('span', '', label), control);
    return r;
  }

  function paint() {
    const s = st.data.settings;
    const modes = st.data.modes || {};
    const hot = el('input');
    hot.type = 'text';
    hot.value = st.data.hotkey_label;
    hot.setAttribute('aria-label', 'Ask panel shortcut');
    hot.dataset.spec = 'Press the keys you want, for example Option Space. Cmd Space is Spotlight and is refused.';
    hot.addEventListener('keydown', (e) => {
      const chord = chordFromEvent(e);
      if (chord) {
        e.preventDefault();
        hot.value = chord;
      }
    });
    const hotSave = el('button', 'os-btn', 'Save');
    hotSave.type = 'button';
    hotSave.addEventListener('click', () => save({ hotkey: hot.value }));
    const hotCell = el('span');
    hotCell.append(hot, document.createTextNode(' '), hotSave);

    const on = el('input');
    on.type = 'checkbox';
    on.checked = s.enabled;
    on.setAttribute('aria-label', 'Open the Ask panel with the shortcut');
    on.addEventListener('change', () => save({ enabled: on.checked }));

    const blur = el('input');
    blur.type = 'checkbox';
    blur.checked = s.close_on_blur;
    blur.setAttribute('aria-label', 'Close the Ask panel when you click away');
    blur.addEventListener('change', () => save({ close_on_blur: blur.checked }));

    const mode = el('select');
    mode.setAttribute('aria-label', 'The Ask panel starts in');
    Object.keys(MODE_NAMES).forEach((m) => {
      const o = el('option', '', MODE_NAMES[m] + (modes[m] === false ? ' (not built yet)' : ''));
      o.value = m;
      o.disabled = modes[m] === false;
      o.selected = m === s.default_mode;
      mode.append(o);
    });
    mode.addEventListener('change', () => save({ default_mode: mode.value }));

    const keep = el('input');
    keep.type = 'number';
    keep.min = '0';
    keep.max = '120';
    keep.value = String(s.keep_minutes);
    keep.setAttribute('aria-label', 'Minutes to keep the conversation');
    keep.dataset.spec = 'Reopen the panel within this many minutes and the last answer is still there. 0 starts fresh every time.';
    keep.addEventListener('change', () => save({ keep_minutes: Number(keep.value) }));

    const line = el('div', st.tone === 'error' ? 'muted set-err' : 'muted', st.line);
    line.setAttribute('role', 'status');
    root.replaceChildren(
      row('Open with the shortcut', on),
      row('Shortcut', hotCell),
      row('Close when you click away', blur),
      row('Starts in', mode),
      row('Keep the conversation (minutes)', keep),
      line,
    );
  }

  load();
}
