/* The Control Centre. Four tiles are live reads (Agents, Security, Network,
   Brain), Do Not Disturb silences in-app toasts and nothing else, and the
   Autonomous tile only REPORTS this tab's per-request flag: turning it on is
   done on HOME, on purpose. Autonomous mode and auto-approve are never a
   one-click tile (architecture section 3): auto-approve is rated HIGH risk when
   on, and the flag raises how far a run may go before it stops.

   Brightness is the wallpaper dim layer inverted; accent swatches recolour the
   whole OS through --dm-active. Both save locally at once and to the server as
   the durable copy, and a failed server save is said out loud. */

import { html, setHtml } from '../kit/html.js';
import { ACCENTS } from '../core/prefs.js';
import { agoLabel } from '../kit/format.js';

export function agentsTile(s) {
  if (!s.known) return { value: 'Unavailable', sub: s.error || 'Reading', tone: 'dim', act: false };
  return { value: s.total + (s.total === 1 ? ' agent' : ' agents'), sub: s.busy + ' working now', tone: s.busy ? 'ok' : 'dim', act: s.busy > 0 };
}

export function securityTile(s) {
  if (!s.known) return { value: 'Unavailable', sub: s.error || 'Reading', tone: 'dim', act: false };
  if (!s.scanned) return { value: 'No scan yet', sub: 'The sentry has not finished a scan', tone: 'dim', act: false };
  const total = s.high + s.med + s.low;
  const when = s.lastScanAt ? 'scanned ' + agoLabel(s.lastScanAt) : 'scanned';
  if (!total) return { value: '0 findings', sub: when + ' (last scan only)', tone: 'ok', act: true };
  return { value: total + (total === 1 ? ' finding' : ' findings'), sub: s.high + ' high, ' + s.med + ' medium, ' + s.low + ' low; ' + when, tone: s.high ? 'bad' : 'warn', act: true };
}

export function networkTile(n) {
  if (!n.known) return { value: 'Unavailable', sub: n.error || 'Reading', tone: 'dim', act: false };
  if (!n.watching) return { value: 'Watch off', sub: 'The network watcher is not running', tone: 'dim', act: false };
  return { value: n.ssid || n.iface || 'Connected', sub: (n.gateway ? 'via ' + n.gateway : 'no gateway') + (n.changes ? '; ' + n.changes + ' changes seen' : ''), tone: 'ok', act: true };
}

export function brainTile(b) {
  if (!b.known) return { value: 'Unavailable', sub: b.error || 'Reading', tone: 'dim', act: false };
  return { value: b.model || b.backend || 'Unknown', sub: b.model && b.backend ? b.backend : '', tone: 'ok', act: false };
}

export function createControlCentre({ root, status, prefs, chat, go, toasts, onChange }) {
  setHtml(root, html`
    <div class="cc-grid">
      <button type="button" class="cc-tile" id="ccAgents" data-spec="Agents. Live count from GET /api/activity, kept current by agent_activity events. Opens OFFICE."><div class="t">Agents</div><div class="v"><span class="dot"></span><span class="vt"></span></div><div class="s"></div></button>
      <button type="button" class="cc-tile" id="ccSec" data-spec="Security. The last completed scan from GET /api/security_dashboard, kept current by security_scan events. Opens SECURITY."><div class="t">Security</div><div class="v"><span class="dot"></span><span class="vt"></span></div><div class="s"></div></button>
      <div class="cc-tile" id="ccNet" data-spec="Network. What the network watcher sees right now (GET /api/security/network)."><div class="t">Network</div><div class="v"><span class="dot"></span><span class="vt"></span></div><div class="s"></div></div>
      <button type="button" class="cc-tile" id="ccModel" data-spec="The active brain (GET /api/backend). Switching the model is in SETTINGS. Large cloud models only."><div class="t">Brain</div><div class="v"><span class="dot"></span><span class="vt"></span></div><div class="s"></div></button>
      <button type="button" class="cc-tile" id="ccDnd" aria-pressed="false" data-spec="Do not disturb. Silences in-app toasts only. Desktop notifications from the app shell are not affected."><div class="t">Do not disturb</div><div class="v"><span class="vt"></span></div><div class="s">In-app toasts only</div></button>
      <button type="button" class="cc-tile" id="ccAuto" data-spec="Autonomous mode for this tab. Shown here, never switched here: raising how far a run may go is set on HOME. It is not auto-approve, and every gated action still asks."><div class="t">Autonomous</div><div class="v"><span class="vt"></span></div><div class="s">Set on HOME. Not auto-approve.</div></button>
    </div>
    <div class="cc-head">Brightness</div>
    <div class="cc-slider"><span aria-hidden="true">&#9728;</span><input type="range" id="ccBright" min="20" max="100" aria-label="Wallpaper brightness" data-spec="Wallpaper brightness. A readability control: body text needs 4.5:1 over whatever is behind it."></div>
    <div class="cc-head">Accent</div>
    <div class="accents" id="accents">${ACCENTS.map((a) => html`<button type="button" class="sw" data-accent="${a.hex}" aria-pressed="false" aria-label="${a.name}" style="background:${a.hex}" data-spec="${a.name}."></button>`)}</div>
    <div class="cc-note" id="ccMsg" role="status"></div>`);

  const $ = (sel) => root.querySelector(sel);
  const msg = $('#ccMsg');

  function paintTile(id, t) {
    const el = $('#' + id);
    el.classList.toggle('act', Boolean(t.act));
    const dot = el.querySelector('.dot');
    dot.className = 'dot' + (t.tone && t.tone !== 'dim' ? ' ' + t.tone : '');
    el.querySelector('.vt').textContent = t.value;
    el.querySelector('.s').textContent = t.sub || '';
  }

  function paint() {
    const s = status.state;
    paintTile('ccAgents', agentsTile(s.agents));
    paintTile('ccSec', securityTile(s.security));
    paintTile('ccNet', networkTile(s.network));
    paintTile('ccModel', brainTile(s.brain));
    const dnd = prefs.dnd();
    const dEl = $('#ccDnd');
    dEl.classList.toggle('act', dnd);
    dEl.setAttribute('aria-pressed', String(dnd));
    dEl.querySelector('.vt').textContent = dnd ? 'On' : 'Off';
    const auto = chat.autonomous();
    const aEl = $('#ccAuto');
    aEl.classList.toggle('act', auto);
    aEl.querySelector('.vt').textContent = auto ? 'On for this tab' : 'Off';
    $('#ccBright').value = String(100 - prefs.dim());
    root.querySelectorAll('.sw').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.accent.toLowerCase() === prefs.accent().toLowerCase())));
  }

  async function saved(name, value) {
    const r = await prefs.save(name, value);
    if (!r.ok) {
      msg.textContent = 'Saved in this browser only. The server said: ' + r.error;
      toasts.show({ level: 'warn', title: 'Preference not saved on the server', detail: r.error });
    } else {
      msg.textContent = '';
    }
  }

  const close = () => onChange && onChange('close');
  $('#ccAgents').addEventListener('click', () => { go('office'); close(); });
  $('#ccSec').addEventListener('click', () => { go('security'); close(); });
  $('#ccModel').addEventListener('click', () => { go('settings'); close(); });
  $('#ccAuto').addEventListener('click', () => { go('home'); close(); });
  $('#ccDnd').addEventListener('click', () => {
    const on = !prefs.dnd();
    prefs.setDnd(on);
    if (on) toasts.hideAll();
    paint();
  });
  const bright = $('#ccBright');
  bright.addEventListener('input', () => prefs.applyDim(100 - Number(bright.value)));
  bright.addEventListener('change', () => saved('dim', 100 - Number(bright.value)));
  $('#accents').addEventListener('click', (e) => {
    const b = e.target.closest('.sw');
    if (!b) return;
    prefs.applyAccent(b.dataset.accent);
    saved('accent', b.dataset.accent);
    paint();
  });

  status.onChange(paint);
  prefs.onChange(paint);
  paint();
  return { paint };
}
