/* The content column and its toolbar (T6 milestone 1, finding #194).

   Toolbar, leading edge: the sidebar button (only while the sidebar is hidden), the place's title,
   and the view switcher when the place holds more than one view. Trailing edge: the view's own
   actions (at most one is drawn as primary), the project scope chip, Search and the inspector
   button. The toolbar is a window drag region; every control in it is not.

   Below it: the body a view mounts into, and the composer. The painted traffic lights are gone:
   macOS draws the real ones (finding #194 retires the stage-only look-alikes of S6). */

import { html, setHtml } from '../kit/html.js';
import { icon } from '../kit/icons.js';
import { actionKey, assertUniqueActions, dedupeActions } from '../kit/actions.js';

export function createStage({ shell, stage, bar, titleEl, subEl, actionsEl, viewsEl, body, go, onSidebar, onInspector, onSearch, scope = null, registry = null }) {
  const lead = bar.querySelector('#tbLead');
  setHtml(lead, html`<button type="button" class="tb-icon" id="tbSidebar" aria-label="Show sidebar" title="Show sidebar (Control Command S)" hidden>${icon('M4 5h16v14H4zM9 5v14')}</button>`);
  const trail = bar.querySelector('#tbTrail');
  setHtml(trail, html`
    <button type="button" class="tb-chip" id="scopeChip" hidden></button>
    <button type="button" class="tb-search" id="palBtn" aria-haspopup="dialog" aria-controls="palette" aria-label="Search (Command K)">${icon('SEARCH', 'tb-ico')}<span>Search</span><kbd aria-hidden="true">&#8984;K</kbd></button>
    <button type="button" class="tb-icon" id="inspBtn" aria-controls="inspector" aria-pressed="false" aria-label="Show inspector" title="Show inspector (Option Command I)">${icon('M4 5h16v14H4zM15 5v14')}</button>`);
  const sidebarBtn = lead.querySelector('#tbSidebar');
  const inspBtn = trail.querySelector('#inspBtn');
  sidebarBtn.addEventListener('click', () => onSidebar && onSidebar());
  inspBtn.addEventListener('click', () => onInspector && onInspector());
  trail.querySelector('#palBtn').addEventListener('click', () => onSearch && onSearch());

  /* F26: the project scope is visible on every view. One chip, one click to leave. */
  const chip = trail.querySelector('#scopeChip');
  function paintScope() {
    const p = scope ? scope.project() : null;
    chip.hidden = !p;
    if (!p) return;
    const name = String(p.name || p.tab_id || '').slice(0, 40);
    chip.textContent = name;
    chip.setAttribute('aria-label', 'Project ' + name + '. Activate to leave the project.');
    chip.title = 'Everything you send is scoped to ' + name + '. Click to leave the project.';
  }
  if (scope) {
    chip.addEventListener('click', () => scope.leaveProject());
    scope.onChange(paintScope);
    paintScope();
  }

  viewsEl.addEventListener('click', (e) => {
    const b = e.target.closest('[data-view]');
    if (b) go(b.dataset.view.toLowerCase());
  });
  /* arrow keys move between views, like a macOS segmented control */
  viewsEl.addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
    const all = Array.from(viewsEl.querySelectorAll('[data-view]'));
    const i = all.indexOf(document.activeElement);
    if (i < 0) return;
    const next = all[(i + (e.key === 'ArrowRight' ? 1 : all.length - 1)) % all.length];
    next.focus();
    e.preventDefault();
  });

  const buttons = new Map(); /* key -> button element */

  return {
    toggleSidebar: () => onSidebar && onSidebar(),
    sidebarHidden(hidden) {
      sidebarBtn.hidden = !hidden;
    },
    inspectorShown(on) {
      inspBtn.setAttribute('aria-pressed', String(on));
      inspBtn.setAttribute('aria-label', on ? 'Hide inspector' : 'Show inspector');
      inspBtn.title = (on ? 'Hide' : 'Show') + ' inspector (Option Command I)';
    },
    setTitle(text) {
      titleEl.textContent = text;
    },
    setSub(text) {
      subEl.textContent = text || '';
    },
    /* the switcher for the place that holds screenId; nothing when the place has one view */
    setViews(screenId) {
      const place = registry ? registry.placeOf(screenId) : null;
      if (!place || place.views.length < 2) {
        viewsEl.hidden = true;
        viewsEl.replaceChildren();
        return place;
      }
      viewsEl.hidden = false;
      setHtml(viewsEl, html`${place.views.map((v) => html`<button type="button" class="seg" data-view="${v}" aria-current="${v === screenId ? 'page' : 'false'}" tabindex="${v === screenId ? '0' : '-1'}">${registry.VIEW_LABELS[v] || v}</button>`)}`);
      viewsEl.setAttribute('aria-label', place.label + ' views');
      return place;
    },
    /* actions: [{ id?, label, spec?, onClick, kind?: 'primary'|'danger', disabled?, pressed?, title? }]
       Buttons are reused by key so a state change never drops keyboard focus. */
    setActions(actions) {
      /* a repeated id is a bug in the view: name it in the console, then draw every button anyway */
      try {
        assertUniqueActions(actions);
      } catch (err) {
        console.error('[stage] ' + err.message);
        actions = dedupeActions(actions);
      }
      const seen = new Set();
      const order = [];
      let primaries = 0;
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
        /* the toolbar carries at most one prominent action (Apple HIG); later primaries draw plain */
        let kind = a.kind || '';
        if (kind === 'primary' && primaries++ > 0) kind = '';
        b.className = 'os-btn' + (kind ? ' os-btn--' + kind : '');
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
    /* a fresh, empty element for the view to mount into */
    newRoot(id) {
      const el = document.createElement('div');
      el.dataset.screen = id;
      el.dataset.region = '';
      body.replaceChildren(el);
      body.scrollTop = 0;
      return el;
    },
    /* between views: nothing of the old one may linger */
    reset() {
      this.setActions([]);
      this.setSub('');
    },
    focusTitle() {
      titleEl.focus({ preventScroll: true });
    },
    bodyEl: body,
    shell,
    stage,
  };
}
