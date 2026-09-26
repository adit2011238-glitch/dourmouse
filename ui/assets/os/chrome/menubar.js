/* The menu bar: brand, wallpaper button, the status cluster (agents, security,
   network: the three reads, coloured by real state) and the clock. The
   cluster is a real button that opens the Control Centre. */

import { html, setHtml } from '../kit/html.js';
import { icon } from '../kit/icons.js';

const TONE = { ok: 'var(--dm-ok)', warn: 'var(--dm-active)', bad: 'var(--dm-error)', dim: 'var(--dm-fg-dim)' };

export function securityTone(s) {
  if (!s.known) return 'dim';
  if (!s.scanned) return 'dim';
  if (s.high > 0) return 'bad';
  if (s.med > 0) return 'warn';
  return 'ok';
}

export function networkTone(n) {
  if (!n.known) return 'dim';
  if (!n.watching) return 'dim';
  return n.ssid === 'offline' && !n.gateway ? 'bad' : 'ok';
}

export function createMenubar({ root, status, timers, panels }) {
  setHtml(root, html`
    <b class="mb-brand">DOURMOUSE</b>
    <button type="button" class="os-btn" id="wallBtn" aria-expanded="false" aria-controls="wallpicker" data-spec="Opens the wallpaper picker: four built-in gradients plus your own photo.">WALLPAPER</button>
    <span class="os-menubar-spacer"></span>
    <button type="button" class="os-mb-cluster" id="ccBtn" aria-expanded="false" aria-controls="controlcenter" aria-label="Control Centre" data-panel-trigger="cc" data-spec="Control Centre. Live agents, security, network and brain, plus brightness and accent.">
      <span class="os-mb-ico" id="mbAgents" title="Agents">${icon('AGENTS')}<span id="mbAgentsN">-</span></span>
      <span class="os-mb-ico" id="mbSec" title="Security">${icon('SHIELD', '', { stroke: 'var(--dm-fg-dim)' })}</span>
      <span class="os-mb-ico" id="mbNet" title="Network">${icon('WIFI')}</span>
    </button>
    <span id="clock"></span>`);

  const nEl = root.querySelector('#mbAgentsN');
  const agentsEl = root.querySelector('#mbAgents');
  const secEl = root.querySelector('#mbSec svg');
  const netEl = root.querySelector('#mbNet svg');
  const clock = root.querySelector('#clock');

  function paint(s) {
    if (!s.agents.known) {
      nEl.textContent = '-';
      agentsEl.title = s.agents.error ? 'Agents: ' + s.agents.error : 'Agents: reading';
    } else {
      nEl.textContent = String(s.agents.total);
      agentsEl.title = s.agents.total + ' agents, ' + s.agents.busy + ' working';
    }
    secEl.setAttribute('stroke', TONE[securityTone(s.security)]);
    root.querySelector('#mbSec').title = !s.security.known
      ? 'Security: ' + (s.security.error || 'reading')
      : !s.security.scanned
        ? 'Security: no scan has finished yet'
        : 'Security: ' + s.security.high + ' high, ' + s.security.med + ' medium, ' + s.security.low + ' low';
    netEl.setAttribute('stroke', TONE[networkTone(s.network)] === TONE.dim ? 'currentColor' : TONE[networkTone(s.network)]);
    root.querySelector('#mbNet').title = !s.network.known
      ? 'Network: ' + (s.network.error || 'reading')
      : !s.network.watching
        ? 'Network watch is off'
        : 'Network: ' + (s.network.iface || '?') + ' via ' + (s.network.gateway || '?');
  }

  function tick() {
    const d = new Date();
    clock.textContent = String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
  }
  tick();
  timers.every(10000, tick, 'chrome');
  status.onChange(paint);
  paint(status.state);
  return { paint, el: root };
}
