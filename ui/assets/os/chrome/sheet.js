/* Sheets (T6 milestone 1, finding #194): a card attached below the toolbar that dims the window
   behind it, the macOS pattern for anything that needs an answer (approvals move here in PL-01
   S1-8). One sheet at a time; opening a second replaces the first.

   Focus moves into the sheet, Tab stays inside it, Escape and Cancel close it, and focus goes back
   to whatever opened it. The buttons sit at the bottom right with the default action last. */

import { html, setHtml, SafeHtml } from '../kit/html.js';

export function createSheets({ layer, keymap, doc = document }) {
  setHtml(layer, html`<div class="sheet-scrim" data-sheet-scrim></div><div class="sheet" role="dialog" aria-modal="true" aria-labelledby="sheetTitle"><h2 class="sheet-title" id="sheetTitle"></h2><div class="sheet-body" id="sheetBody"></div><div class="sheet-btns" id="sheetBtns"></div></div>`);
  const card = layer.querySelector('.sheet');
  const titleEl = layer.querySelector('#sheetTitle');
  const bodyEl = layer.querySelector('#sheetBody');
  const btnsEl = layer.querySelector('#sheetBtns');
  let current = null;

  function focusables() {
    return Array.from(card.querySelectorAll('button:not([disabled]), [href], input:not([disabled]), select, textarea, [tabindex]:not([tabindex="-1"])'));
  }
  card.addEventListener('keydown', (e) => {
    if (e.key !== 'Tab') return;
    const f = focusables();
    if (!f.length) return;
    const first = f[0];
    const last = f[f.length - 1];
    if (e.shiftKey && doc.activeElement === first) {
      last.focus();
      e.preventDefault();
    } else if (!e.shiftKey && doc.activeElement === last) {
      first.focus();
      e.preventDefault();
    }
  });

  function close(reason = 'cancel') {
    if (!current) return;
    const c = current;
    current = null;
    layer.hidden = true;
    if (c.popEsc) c.popEsc();
    const back = c.returnFocus && c.returnFocus.isConnected ? c.returnFocus : null;
    if (back && back.focus) back.focus();
    if (c.onClose) c.onClose(reason);
  }

  /* { title, body: SafeHtml | Node, actions: [{ label, kind?: 'primary'|'danger', onClick?(api), cancel? }], onClose?(reason) }
     onClick may return false to keep the sheet open (a validation error shown in the body). */
  function open({ title, body, actions = [{ label: 'Done', kind: 'primary' }], onClose = null }) {
    if (current) close('replaced');
    const api = { close, body: bodyEl, setBusy };
    current = { onClose, returnFocus: doc.activeElement, popEsc: keymap.pushEsc(() => close('cancel')) };
    titleEl.textContent = title || '';
    if (body instanceof SafeHtml) setHtml(bodyEl, body);
    else if (body) bodyEl.replaceChildren(body);
    else bodyEl.replaceChildren();
    btnsEl.replaceChildren(...actions.map((a) => {
      const b = doc.createElement('button');
      b.type = 'button';
      b.className = 'os-btn' + (a.kind ? ' os-btn--' + a.kind : '');
      b.textContent = a.label;
      b.addEventListener('click', async () => {
        if (a.cancel) return close('cancel');
        if (!a.onClick) return close('done');
        const keep = await a.onClick(api);
        if (keep !== false) close('done');
      });
      return b;
    }));
    layer.hidden = false;
    const preferred = card.querySelector('[autofocus], input, select, textarea') || btnsEl.lastElementChild;
    if (preferred) preferred.focus();
    return api;
  }

  function setBusy(on) {
    btnsEl.querySelectorAll('button').forEach((b) => { b.disabled = Boolean(on); });
  }

  return { open, close, isOpen: () => Boolean(current) };
}
