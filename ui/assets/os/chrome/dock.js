/* The dock: icons for the surfaces worth one click from anywhere, plus the
   Alerts item (opens the Notification Centre, badge = live alert count).
   Running dots come from ctx.chrome.setLive, never from a static flag. */

import { html, setHtml } from '../kit/html.js';
import { icon } from '../kit/icons.js';

export function createDock({ root, registry, go, notif }) {
  setHtml(root, html`${registry.DOCK.map(([id, label]) => html`
    <button type="button" class="os-dock-item" data-go="${id.toLowerCase()}" data-dock="${id}" data-running="false" aria-label="${label}" aria-current="false" data-spec="Opens ${id}. The dot means that surface has something live running.">
      ${icon(id)}<span class="os-dock-label">${label}</span>
    </button>`)}<div class="os-dock-sep"></div>
    <button type="button" class="os-dock-item" id="dockAlerts" data-panel-trigger="alerts" aria-label="Alerts" aria-expanded="false" aria-controls="notifcenter" data-spec="Notification centre. Every alert the server raised, plus this session's notices. Dismissing here dismisses it on the server.">
      ${icon('BELL')}<span class="os-badge" id="dockBadge" hidden></span><span class="os-dock-label">Alerts</span>
    </button><div class="os-dock-sep"></div>${(registry.CONSOLE_LINKS || []).map(([id, label]) => html`
    <a class="os-dock-item" href="/console" data-console="${id}" aria-label="${label}, in the classic console" data-spec="${label} stays in the classic console. This link opens the console at its home screen; choose ${label} from its sidebar.">
      ${icon(id)}<span class="os-dock-label">${label} (classic)</span>
    </a>`)}`);
  root.addEventListener('click', (e) => {
    const b = e.target.closest('[data-go]');
    if (b) go(b.dataset.go);
  });
  const items = new Map(Array.from(root.querySelectorAll('[data-dock]')).map((el) => [el.dataset.dock, el]));
  const live = new Map();
  const alertsBtn = root.querySelector('#dockAlerts');
  const badge = root.querySelector('#dockBadge');
  alertsBtn.addEventListener('click', () => notif.toggle());

  return {
    alertsButton: alertsBtn,
    setCurrent(id) {
      items.forEach((el, key) => el.setAttribute('aria-current', key === id ? 'page' : 'false'));
    },
    setLive(id, on, source = 'screen') {
      const set = live.get(id) || new Set();
      if (on) set.add(source);
      else set.delete(source);
      live.set(id, set);
      const el = items.get(id);
      if (el) el.dataset.running = set.size ? 'true' : 'false';
    },
    setBadge(n) {
      badge.hidden = !n;
      badge.textContent = n > 99 ? '99+' : String(n || '');
      alertsBtn.setAttribute('aria-label', n ? 'Alerts, ' + n + ' active' : 'Alerts');
    },
  };
}
