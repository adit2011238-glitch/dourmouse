/* Floating panels (Control Centre, Notification Centre, wallpaper picker).
   Each open panel pushes a closer onto the keymap's Esc stack, moves focus in,
   and gives focus back to whatever opened it. An outside click closes it. The
   mockup closed panels only by click, with no Esc and no focus handling
   (pitfall 9). One document click listener serves all of them. */

export function createPanels({ keymap, doc = document }) {
  const defs = new Map();
  const listeners = new Set();

  const anyOpen = () => Array.from(defs.values()).some((d) => d.isOpen);
  const emit = () => listeners.forEach((fn) => fn(anyOpen()));

  function make(name, { el, trigger, exclusive = [], onOpen, onClose }) {
    const def = { name, el, trigger, isOpen: false, popEsc: null, returnFocus: null };
    def.open = () => {
      if (def.isOpen) return;
      exclusive.forEach((n) => defs.get(n) && defs.get(n).close());
      def.isOpen = true;
      def.returnFocus = doc.activeElement;
      el.hidden = false;
      if (trigger) {
        trigger.setAttribute('aria-expanded', 'true');
        trigger.classList.add('is-on');
      }
      def.popEsc = keymap.pushEsc(() => def.close());
      if (onOpen) onOpen();
      const first = el.querySelector('button:not([disabled]), input, [tabindex]');
      if (first) first.focus();
      emit();
    };
    def.close = () => {
      if (!def.isOpen) return;
      def.isOpen = false;
      el.hidden = true;
      if (trigger) {
        trigger.setAttribute('aria-expanded', 'false');
        trigger.classList.remove('is-on');
      }
      if (def.popEsc) def.popEsc();
      def.popEsc = null;
      if (onClose) onClose();
      const back = def.returnFocus && def.returnFocus.isConnected ? def.returnFocus : trigger;
      if (back && back.focus) back.focus();
      def.returnFocus = null;
      emit();
    };
    def.toggle = () => (def.isOpen ? def.close() : def.open());
    defs.set(name, def);
    return def;
  }

  doc.addEventListener('click', (e) => {
    defs.forEach((d) => {
      if (!d.isOpen) return;
      const t = e.target;
      if (d.el.contains(t)) return;
      if (d.trigger && d.trigger.contains(t)) return;
      if (t && t.closest && t.closest('[data-panel-trigger]')) return;
      d.close();
    });
  });

  return {
    make,
    get: (n) => defs.get(n),
    anyOpen,
    closeAll() {
      defs.forEach((d) => d.close());
    },
    onChange(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    listenerCount: () => listeners.size,
  };
}
