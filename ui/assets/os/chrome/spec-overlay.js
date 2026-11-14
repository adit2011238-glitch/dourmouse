/* The interaction-spec overlay: pure CSS on body[data-annotate] labels every control with what it
   does. Kept for review builds, but not on a bare A key: the mockup fired it while typing in a
   textarea (pitfall 9). It is Option A through the keymap, which ignores editable targets, and
   View, Developer, Label Every Control in the real menu bar (T6: the on-screen SPEC button is
   retired). */

export function createSpecOverlay({ keymap, body = document.body }) {
  function set(on) {
    body.dataset.annotate = on ? 'true' : 'false';
  }
  const toggle = () => set(body.dataset.annotate !== 'true');
  const off = keymap.bind('alt+a', toggle);
  return { toggle, set, off };
}
