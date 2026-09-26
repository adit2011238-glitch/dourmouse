/* The 18 screens as a nav. Built ONCE: switching screens only moves the
   highlight. Rebuilding the nav on every switch destroyed the element being
   pressed and threw away focus (os_mockup.html:841-848). */

import { html, setHtml } from '../kit/html.js';
import { icon } from '../kit/icons.js';

export function createSidebar({ root, registry, go }) {
  setHtml(root, html`${registry.SCREENS.map((s) => html`
    <button type="button" class="navitem" data-go="${s.slug}" data-live="0" aria-current="false" data-spec="Switches the stage to ${s.id}. ${s.thread ? 'Has its own independent thread.' : "Directives typed here fall back to HOME's thread."}">
      ${icon(s.icon, 'ni')}<span class="nl">${s.label}</span><i class="nd" aria-hidden="true"></i>
    </button>`)}`);
  root.addEventListener('click', (e) => {
    const b = e.target.closest('[data-go]');
    if (b) go(b.dataset.go);
  });
  const items = new Map(Array.from(root.querySelectorAll('.navitem')).map((el) => [el.dataset.go, el]));
  const live = new Map();

  return {
    setCurrent(id) {
      const slug = id.toLowerCase();
      items.forEach((el, key) => {
        if (key === slug) el.setAttribute('aria-current', 'page');
        else el.setAttribute('aria-current', 'false');
      });
    },
    /* live dots come from ctx.chrome.setLive and from chat threads that are
       running, never from a static flag */
    setLive(id, on, source = 'screen') {
      const set = live.get(id) || new Set();
      if (on) set.add(source);
      else set.delete(source);
      live.set(id, set);
      const el = items.get(id.toLowerCase());
      if (el) el.dataset.live = set.size ? '1' : '0';
    },
    liveIds: () => Array.from(live.entries()).filter(([, s]) => s.size).map(([id]) => id),
  };
}
