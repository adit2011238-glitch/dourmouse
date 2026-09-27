/* The stage: title bar with traffic lights, subtitle, per-screen action
   buttons, the body a screen mounts into, and the composer. The lights are
   real buttons that act on the STAGE (the Electron window keeps its native
   frame and preload.js exposes no window-control IPC, so they cannot close or
   minimise the OS window):
     red    return to HOME
     yellow collapse the stage to its title bar / restore it
     green  hide or show the sidebar */

import { html, setHtml } from '../kit/html.js';
import { actionKey, assertUniqueActions, dedupeActions } from '../kit/actions.js';

export function createStage({ shell, stage, bar, titleEl, subEl, actionsEl, body, lights, go, host = null }) {
  setHtml(lights, html`
    <button type="button" class="os-tl r" id="tlClose" aria-label="Return to HOME" title="Return to HOME (these three only act on this stage, not on the window)" data-spec="Red: return to HOME. In a browser these lights act on the stage, not on the window."></button>
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
  function toggleSidebar() {
    const hidden = shell.dataset.sidebar !== 'hidden';
    shell.dataset.sidebar = hidden ? 'hidden' : 'shown';
    greenEl.setAttribute('aria-pressed', String(hidden));
    greenEl.setAttribute('aria-label', hidden ? 'Show the sidebar' : 'Hide the sidebar');
  }
  greenEl.addEventListener('click', toggleSidebar);
  /* S6: inside the Electron app the native window frame already has the real
     traffic lights, so these stage-only look-alikes would be a second set that
     does not close, minimise or zoom anything. They stay in a plain browser
     (and pywebview), where there is no such frame of ours to confuse them
     with. The sidebar toggle they carried is also on Command backslash. */
  if (host && host.kind === 'electron') lights.hidden = true;

  const buttons = new Map(); /* key -> button element */

  return {
    toggleSidebar,
    lightsHidden: () => lights.hidden,
    setTitle(text) {
      titleEl.textContent = text;
    },
    setSub(text) {
      subEl.textContent = text || '';
    },
    /* actions: [{ id?, label, spec?, onClick, kind?: 'primary'|'danger', disabled?, pressed?, title? }]
       Buttons are reused by key so a state change never drops keyboard focus. */
    setActions(actions) {
      /* a repeated id is a bug in the screen: name it in the console, then draw every
         button anyway so one slip never takes the whole screen down */
      try {
        assertUniqueActions(actions);
      } catch (err) {
        console.error('[stage] ' + err.message);
        actions = dedupeActions(actions);
      }
      const seen = new Set();
      const order = [];
      (actions || []).forEach((a) => {
        const key = actionKey(a);
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
