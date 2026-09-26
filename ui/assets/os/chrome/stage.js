/* The stage: title bar with traffic lights, subtitle, per-screen action
   buttons, the body a screen mounts into, and the composer. The lights are
   real buttons that act on the STAGE (the Electron window keeps its native
   frame and preload.js exposes no window-control IPC, so they cannot close or
   minimise the OS window):
     red    return to HOME
     yellow collapse the stage to its title bar / restore it
     green  hide or show the sidebar */

import { html, setHtml } from '../kit/html.js';

export function createStage({ shell, stage, bar, titleEl, subEl, actionsEl, body, lights, go }) {
  setHtml(lights, html`
    <button type="button" class="os-tl r" id="tlClose" aria-label="Return to HOME" data-spec="Red: return to HOME. This shell keeps its native window frame, so the lights act on the stage."></button>
    <button type="button" class="os-tl y" id="tlMin" aria-label="Collapse the stage" aria-pressed="false" data-spec="Yellow: collapse the stage to its title bar, or restore it."></button>
    <button type="button" class="os-tl g" id="tlZoom" aria-label="Hide the sidebar" aria-pressed="false" data-spec="Green: hide or show the sidebar to give the stage the whole width."></button>`);
  const redEl = lights.querySelector('#tlClose');
  const yellowEl = lights.querySelector('#tlMin');
  const greenEl = lights.querySelector('#tlZoom');
  redEl.addEventListener('click', () => go('home'));
  yellowEl.addEventListener('click', () => {
    const on = stage.dataset.min !== '1';
    stage.dataset.min = on ? '1' : '0';
    yellowEl.setAttribute('aria-pressed', String(on));
    yellowEl.setAttribute('aria-label', on ? 'Restore the stage' : 'Collapse the stage');
  });
  greenEl.addEventListener('click', () => {
    const hidden = shell.dataset.sidebar !== 'hidden';
    shell.dataset.sidebar = hidden ? 'hidden' : 'shown';
    greenEl.setAttribute('aria-pressed', String(hidden));
    greenEl.setAttribute('aria-label', hidden ? 'Show the sidebar' : 'Hide the sidebar');
  });

  const buttons = new Map(); /* key -> button element */

  return {
    setTitle(text) {
      titleEl.textContent = text;
    },
    setSub(text) {
      subEl.textContent = text || '';
    },
    /* actions: [{ id?, label, spec?, onClick, kind?: 'primary'|'danger', disabled?, pressed?, title? }]
       Buttons are reused by key so a state change never drops keyboard focus. */
    setActions(actions) {
      const seen = new Set();
      const order = [];
      (actions || []).forEach((a) => {
        const key = a.id || a.label;
        seen.add(key);
        let b = buttons.get(key);
        if (!b) {
          b = document.createElement('button');
          b.type = 'button';
          b.addEventListener('click', () => b._onClick && b._onClick());
          buttons.set(key, b);
        }
        b._onClick = a.onClick || null;
        b.className = 'os-btn' + (a.kind ? ' os-btn--' + a.kind : '');
        b.textContent = a.label;
        b.disabled = Boolean(a.disabled);
        if (a.spec) b.dataset.spec = a.spec;
        else delete b.dataset.spec;
        if (a.title) b.title = a.title;
        else b.removeAttribute('title');
        if (a.pressed === undefined) b.removeAttribute('aria-pressed');
        else b.setAttribute('aria-pressed', String(Boolean(a.pressed)));
        order.push(b);
      });
      Array.from(buttons.keys()).forEach((k) => {
        if (!seen.has(k)) buttons.delete(k);
      });
      actionsEl.replaceChildren(...order);
    },
    /* a fresh, empty element for the screen to mount into */
    newRoot(id) {
      const el = document.createElement('div');
      el.dataset.screen = id;
      el.dataset.region = '';
      body.replaceChildren(el);
      body.scrollTop = 0;
      return el;
    },
    /* between screens: nothing of the old one may linger */
    reset() {
      this.setActions([]);
      this.setSub('');
    },
    focusTitle() {
      titleEl.focus({ preventScroll: true });
    },
    bodyEl: body,
    minimised: () => stage.dataset.min === '1',
  };
}
