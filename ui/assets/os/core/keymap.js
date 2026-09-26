/* The only global keydown listener in the shell. Screens bind through
   ctx.keys and never touch document. It ignores keys typed into editable
   targets (the mockup toggled its overlay on a bare A even while typing in a
   textarea, pitfall 9), and it owns an Esc stack so panels close in order.

   One deliberate exception: Escape reaches the stack even from an editable
   target, because a panel that has focus in its own text field must still
   close on Esc. Nothing else does. */

const EDITABLE = new Set(['INPUT', 'TEXTAREA', 'SELECT']);

export function isEditableTarget(t) {
  if (!t) return false;
  if (EDITABLE.has(String(t.tagName || '').toUpperCase())) return true;
  if (t.isContentEditable === true) return true;
  const attr = t.getAttribute ? t.getAttribute('contenteditable') : t.contenteditable;
  return attr === '' || attr === 'true' || attr === 'plaintext-only';
}

function parseCombo(combo) {
  const parts = String(combo).split('+').map((s) => s.trim());
  const key = parts.pop().toLowerCase();
  const mods = new Set(parts.map((p) => p.toLowerCase()));
  return { key, ctrl: mods.has('ctrl'), alt: mods.has('alt'), shift: mods.has('shift'), meta: mods.has('meta') || mods.has('cmd') };
}

function comboMatches(c, e) {
  if (Boolean(e.ctrlKey) !== c.ctrl || Boolean(e.altKey) !== c.alt || Boolean(e.shiftKey) !== c.shift || Boolean(e.metaKey) !== c.meta) {
    return false;
  }
  const k = String(e.key || '').toLowerCase();
  if (k === c.key) return true;
  /* Alt+letter types a different character on macOS, so also match the
     physical key. */
  return String(e.code || '').toLowerCase() === 'key' + c.key;
}

export function createKeymap() {
  const bindings = new Set();
  const escStack = [];
  let target = null;

  function handle(e) {
    if (e.defaultPrevented) return false;
    if (e.key === 'Escape') {
      if (escStack.length) {
        const top = escStack[escStack.length - 1];
        try {
          top.fn(e);
        } catch (err) {
          console.error(err);
        }
        if (e.preventDefault) e.preventDefault();
        return true;
      }
      return false;
    }
    if (isEditableTarget(e.target)) return false;
    for (const b of Array.from(bindings)) {
      if (comboMatches(b.combo, e)) {
        try {
          b.fn(e);
        } catch (err) {
          console.error(err);
        }
        if (e.preventDefault) e.preventDefault();
        return true;
      }
    }
    return false;
  }

  const onKey = (e) => handle(e);
  const map = {
    handle,
    bind(combo, fn) {
      const b = { combo: parseCombo(combo), fn };
      bindings.add(b);
      return () => bindings.delete(b);
    },
    /* Push a closer; the returned function removes it. Esc calls the newest. */
    pushEsc(fn) {
      const item = { fn };
      escStack.push(item);
      return () => {
        const i = escStack.indexOf(item);
        if (i >= 0) escStack.splice(i, 1);
      };
    },
    attach(doc) {
      if (target) return;
      target = doc;
      doc.addEventListener('keydown', onKey);
    },
    detach() {
      if (target) target.removeEventListener('keydown', onKey);
      target = null;
    },
    count: () => bindings.size,
    escDepth: () => escStack.length,
  };
  return map;
}
