/* The menu bar: brand, wallpaper button, the status cluster (agents, security,
   network: the three reads, coloured by real state) and the clock. The
   cluster is a real button that opens the Control Centre. */

import { html, setHtml } from '../kit/html.js';
import { icon } from '../kit/icons.js';

const TONE = { ok: 'var(--dm-ok)', warn: 'var(--os-warn)', bad: 'var(--dm-error)', dim: 'var(--dm-fg-dim)' };

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

export function createMenubar({ root, status, timers, panels, host = null, scope = null }) {
  setHtml(root, html`
    <b class="mb-brand">DOURMOUSE</b>
    <button type="button" class="os-btn" id="wallBtn" aria-expanded="false" aria-controls="wallpicker" data-spec="Opens the wallpaper picker: four built-in gradients plus your own photo.">Wallpaper</button>
    <span class="os-menubar-spacer"></span>
    <button type="button" class="os-btn" id="palBtn" aria-haspopup="dialog" aria-controls="palette" aria-label="Search or ask (Command K)" data-spec="Opens the launcher: jump to any screen, run a quick action, or ask Dourmouse. Keyboard: Command K (Ctrl K)."><span class="pb-text">Search or ask</span><kbd aria-hidden="true">&#8984;K</kbd></button>
    <span class="os-menubar-spacer"></span>
    <button type="button" class="os-btn" id="mbProject" hidden data-spec="The project this tab is scoped to. Click to leave it and go back to your general conversation. The project keeps its own thread on the server."></button>
    <button type="button" class="os-mb-cluster" id="ccBtn" aria-expanded="false" aria-controls="controlcenter" aria-label="Control Centre" data-panel-trigger="cc" data-spec="Control Centre. Live agents, security, network and model, plus brightness and accent.">
      <span class="os-mb-ico" id="mbAgents" title="Agents">${icon('AGENTS')}<span id="mbAgentsN">-</span></span>
      <span class="os-mb-ico" id="mbSec" title="Security">${icon('SHIELD', '', { stroke: 'var(--dm-fg-dim)' })}<span id="mbSecN"></span></span>
      <span class="os-mb-ico" id="mbNet" title="Network">${icon('WIFI')}</span>
    </button>
    <span id="clock"></span>`);

  const nEl = root.querySelector('#mbAgentsN');
  const agentsEl = root.querySelector('#mbAgents');
  const secEl = root.querySelector('#mbSec svg');
  const netEl = root.querySelector('#mbNet svg');
  const clock = root.querySelector('#clock');
  const secN = root.querySelector('#mbSecN');
  const ccBtn = root.querySelector('#ccBtn');
  /* F26: scope is visible on every screen, not only HOME. One chip, one click to leave. */
  const projEl = root.querySelector('#mbProject');
  function paintProject() {
    const p = scope ? scope.project() : null;
    projEl.hidden = !p;
    if (!p) return;
    const name = String(p.name || p.tab_id || '').slice(0, 40);
    projEl.textContent = 'Project: ' + name + ' \u00d7';
    projEl.setAttribute('aria-label', 'Project ' + name + '. Activate to leave the project.');
    projEl.title = 'Everything you send is scoped to ' + name + '. Click to leave.';
  }
  if (scope) {
    projEl.addEventListener('click', () => scope.leaveProject());
    scope.onChange(paintProject);
    paintProject();
  }
  /* inside the Electron app the macOS menu bar right above already shows the time */
  if (host && host.kind === 'electron') clock.hidden = true;

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
    /* S34: the state is also words and a number, not only a colour and a tooltip */
    const hits = s.security.known && s.security.scanned ? s.security.high + s.security.med : 0;
    secN.textContent = hits ? String(hits) : '';
    ccBtn.setAttribute('aria-label', 'Control Centre. ' + [
      s.agents.known ? 'Agents: ' + s.agents.total + ', ' + s.agents.busy + ' working.' : 'Agents: not read yet.',
      !s.security.known ? 'Security: not read yet.' : !s.security.scanned ? 'Security: no scan has finished yet.' : 'Security: ' + s.security.high + ' high, ' + s.security.med + ' medium, ' + s.security.low + ' low.',
      !s.network.known ? 'Network: not read yet.' : !s.network.watching ? 'Network watch is off.' : 'Network: ' + (s.network.iface || 'connected') + '.',
    ].join(' '));
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
