/* The sidebar (T6 milestone 1, finding #194): the eight places, then pinned projects.

   Built ONCE: switching only moves the highlight (rebuilding the nav on every switch destroyed the
   element being pressed and threw away focus). The top strip is empty window chrome: macOS draws
   the real traffic lights there (titleBarStyle hiddenInset), and it is a drag region.

   Badges are real current values only (busy agents on Office, open high and medium findings on
   Security, alerts); a zero hides the badge. A pinned project opens through the same
   /api/projects/open call the Projects view uses and becomes the scope everything is sent in. */

import { html, setHtml } from '../kit/html.js';
import { icon } from '../kit/icons.js';

/* a stable cover colour per project, from the cover tokens (never a random one) */
export function coverIndex(name, n = 6) {
  let h = 0;
  for (const ch of String(name || '')) h = (h * 31 + ch.codePointAt(0)) >>> 0;
  return (h % n) + 1;
}

export function createSidebar({ root, registry, go, onToggle, onEditPins, onOpenPin }) {
  const places = registry.PLACES;
  setHtml(root, html`
    <div class="sb-top" data-drag>
      <button type="button" class="sb-icon" id="sbToggle" aria-label="Hide sidebar" title="Hide sidebar (Control Command S)">${icon('M4 5h16v14H4zM9 5v14')}</button>
    </div>
    <div class="sb-places" role="list" aria-label="Places">${places.map((p, i) => html`
      <button type="button" class="navitem" role="listitem" data-place="${p.id}" data-go="${p.views[0].toLowerCase()}" data-live="0" aria-current="false" title="${p.label} (Command ${i + 1})">
        ${icon(p.icon, 'ni')}<span class="nl">${p.label}</span><span class="nb" hidden></span><i class="nd" aria-hidden="true"></i>
      </button>`)}
    </div>
    <div class="sb-head"><h2 id="sbPinsHead">Pinned projects</h2><button type="button" class="sb-link" id="sbPinEdit" aria-describedby="sbPinsHead">Edit</button></div>
    <div class="sb-pins" id="sbPins" aria-labelledby="sbPinsHead"></div>`);

  const items = new Map(Array.from(root.querySelectorAll('.navitem')).map((el) => [el.dataset.place, el]));
  const pinsEl = root.querySelector('#sbPins');
  const live = new Map(); /* place id -> Set of "screen id:source" */

  root.addEventListener('click', (e) => {
    const b = e.target.closest('[data-go]');
    if (b && b.classList.contains('navitem')) go(b.dataset.go);
    const pin = e.target.closest('[data-pin]');
    if (pin && onOpenPin) onOpenPin(pin.dataset.pin);
    if (e.target.closest('[data-pin-add]') && onEditPins) onEditPins();
    if (e.target.closest('[data-pin-retry]') && onEditPins) onEditPins();
  });
  root.querySelector('#sbToggle').addEventListener('click', () => onToggle && onToggle());
  root.querySelector('#sbPinEdit').addEventListener('click', () => onEditPins && onEditPins());

  function placeFor(screenId) {
    return registry.placeOf(screenId);
  }

  return {
    setCurrent(screenId) {
      const place = placeFor(screenId);
      items.forEach((el, id) => el.setAttribute('aria-current', place && place.id === id ? 'page' : 'false'));
    },
    /* live dots come from ctx.chrome.setLive and from chat threads that are running, never a static flag */
    setLive(screenId, on, source = 'screen') {
      const place = placeFor(screenId);
      if (!place) return;
      const set = live.get(place.id) || new Set();
      if (on) set.add(screenId + ':' + source);
      else set.delete(screenId + ':' + source);
      live.set(place.id, set);
      const el = items.get(place.id);
      if (!el) return;
      el.dataset.live = set.size ? '1' : '0';
      el.setAttribute('aria-label', place.label + (set.size ? ', running' : '') + (el.dataset.badgeWords ? ', ' + el.dataset.badgeWords : ''));
    },
    /* n: a real current count or 0; words: what it counts, for a screen reader */
    setBadge(placeId, n, words = '') {
      const el = items.get(placeId);
      if (!el) return;
      const b = el.querySelector('.nb');
      const count = Number(n) || 0;
      b.hidden = !count;
      b.textContent = count > 99 ? '99+' : String(count || '');
      el.dataset.badgeWords = count ? count + ' ' + words : '';
      const running = el.dataset.live === '1';
      el.setAttribute('aria-label', (el.querySelector('.nl').textContent) + (running ? ', running' : '') + (count ? ', ' + count + ' ' + words : ''));
    },
    /* pins: [{ path, name, tab_id }] | { error } | null (loading); active: the scoped project's tab id */
    setPins(pins, active = '') {
      if (pins === null) {
        setHtml(pinsEl, html`<div class="sb-note" role="status">Reading projects</div>`);
        return;
      }
      if (pins && pins.error) {
        setHtml(pinsEl, html`<div class="sb-note">Could not read projects: ${pins.error}</div><button type="button" class="sb-link" data-pin-retry>Try again</button>`);
        return;
      }
      if (!pins.length) {
        setHtml(pinsEl, html`<button type="button" class="sb-link sb-add" data-pin-add>Pin a project</button>`);
        return;
      }
      setHtml(pinsEl, html`${pins.map((p) => html`
        <button type="button" class="navitem pin" data-pin="${p.path}" aria-current="${active && p.tab_id && String(p.tab_id) === String(active) ? 'true' : 'false'}" title="${p.path}">
          <span class="cover" data-cover="${coverIndex(p.name)}" aria-hidden="true"></span><span class="nl">${p.name}</span>
        </button>`)}`);
    },
    toggleLabel(hidden) {
      const t = root.querySelector('#sbToggle');
      t.setAttribute('aria-label', hidden ? 'Show sidebar' : 'Hide sidebar');
    },
    liveIds: () => Array.from(live.entries()).filter(([, s]) => s.size).map(([id]) => id),
  };
}
