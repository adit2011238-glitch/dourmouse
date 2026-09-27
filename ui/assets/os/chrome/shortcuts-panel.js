/* The shortcut help panel (Command slash, or "Keyboard shortcuts" in the
   launcher). It only lists what core/shortcuts.js defines. */

import { html, setHtml } from '../kit/html.js';

export function createShortcutsPanel({ root, list }) {
  const groups = [];
  list.forEach((s) => {
    let g = groups.find((x) => x.name === s.group);
    if (!g) groups.push((g = { name: s.group, rows: [] }));
    g.rows.push(s);
  });
  setHtml(root, html`
    <div class="cc-head"><span>Keyboard shortcuts</span></div>
    ${groups.map((g) => html`
      <div class="sc-group" role="group" aria-label="${g.name}">
        <div class="sc-name">${g.name}</div>
        ${g.rows.map((s) => html`<div class="sc-row"><span class="sc-label">${s.label}</span><kbd class="sc-keys">${s.keys}</kbd></div>`)}
      </div>`)}
    <p class="cc-note">Shortcuts marked with a command key also work while you type in a box. Esc always closes the newest thing that is open.</p>`);
  return { el: root };
}
