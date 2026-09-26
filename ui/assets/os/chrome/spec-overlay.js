/* The interaction-spec overlay: pure CSS on body[data-annotate] labels every
   control with what it does. Kept for review builds, but not on a bare A key:
   the mockup fired it while typing in a textarea (pitfall 9). It is Alt+A,
   through the keymap, which ignores editable targets. */

export function createSpecOverlay({ button, legend, keymap, body = document.body }) {
  function set(on) {
    body.dataset.annotate = on ? 'true' : 'false';
    button.classList.toggle('is-on', on);
    button.setAttribute('aria-pressed', String(on));
    legend.textContent = on ? 'Every control is labelled with what it does' : 'Alt+A labels every control';
  }
  const toggle = () => set(body.dataset.annotate !== 'true');
  button.setAttribute('aria-pressed', 'false');
  button.addEventListener('click', toggle);
  const off = keymap.bind('alt+a', toggle);
  return { toggle, set, off };
}
