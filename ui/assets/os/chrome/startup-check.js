/* The startup sign-in check. The console had it as a separate script
   (assets/startup_check.js, a light-themed overlay that would flash on this
   dark shell). Kept: a real read of the three sign-in states (Claude CLI and
   Codex CLI from /api/connections, Google from /api/auth/status) and the EXACT
   command to paste. Dropped on purpose: the loading overlay, because the shell
   paints at once and the check never blocks it.

   S12 and F22: it used to be a floating dialog over whatever screen was open,
   covering the first thing a newcomer reads. It is now a quiet banner under the
   stage bar, on HOME only, with one "Not now" that is remembered: a dismissed item
   stays quiet across launches until it is signed in (and so drops out) or a new
   item goes missing. The launcher has "Show sign-in help" to bring it back.

   A failed read shows nothing (as the console's did): silence here means "no
   sign-in problem was reported", never "everything is signed in". */

import { html, setHtml } from '../kit/html.js';

const DISMISSED_KEY = 'dm.os.signinDismissed';

/* Pure, so node can test it. Each item is { id, label, command } or { id, label, href }. */
export function missingSignins(conns, auth) {
  const c = conns || {};
  const a = auth || {};
  const out = [];
  if (c.claude && c.claude.ok === false) {
    out.push({ id: 'claude', label: 'Claude Code CLI: run this, then complete /login', command: 'claude' });
  }
  if (c.codex && c.codex.ok === false) {
    out.push({ id: 'codex', label: 'Codex CLI: install it and sign in', command: 'npm i -g @openai/codex && codex login' });
  }
  if (a.configured && !a.me) {
    out.push({ id: 'google', label: 'Google account: not signed in', href: '/api/auth/google/start' });
  }
  return out;
}

/* Pure. The items the owner has not said "not now" to. */
export function unsilenced(items, dismissedIds) {
  const gone = new Set(Array.isArray(dismissedIds) ? dismissedIds : []);
  return (items || []).filter((it) => !gone.has(it.id));
}

export function createStartupCheck({ root, api, toasts, prefs = null }) {
  let items = [];
  let screen = 'HOME';

  function readDismissed() {
    try {
      const v = JSON.parse((prefs && prefs.read(DISMISSED_KEY)) || '[]');
      return Array.isArray(v) ? v.filter((x) => typeof x === 'string') : [];
    } catch (_err) {
      return [];
    }
  }

  function paint() {
    const list = unsilenced(items, readDismissed());
    const show = list.length > 0 && screen === 'HOME';
    root.hidden = !show;
    if (!show) {
      root.replaceChildren();
      return;
    }
    setHtml(root, html`
      <div class="sb-main">
        <b class="sb-title">Finish signing in</b>
        <span class="sb-sub">Optional. Dourmouse works without these.</span>
        ${list.map((it) => html`<span class="sb-item"><span class="sb-label">${it.label}</span>
          ${it.command ? html`<button type="button" class="su-cmd" data-copy="${it.command}" title="Click to copy" data-spec="Copies this command so you can paste it into a terminal.">${it.command}</button>` : ''}
          ${it.href ? html`<a class="os-btn" href="${it.href}" data-spec="Starts the Google sign-in for this app.">Sign in with Google</a>` : ''}</span>`)}
      </div>
      <button type="button" class="os-btn" id="suDismiss" data-spec="Hides this. It stays hidden on later launches until something new needs signing in. The launcher can bring it back.">Not now</button>`);
    root.querySelectorAll('[data-copy]').forEach((b) => {
      b.addEventListener('click', () => {
        const text = b.dataset.copy;
        const write = navigator.clipboard && navigator.clipboard.writeText ? navigator.clipboard.writeText(text) : Promise.reject(new Error('clipboard not available here'));
        write.then(() => toasts.show({ level: 'ok', title: 'Copied', detail: text, ttl: 2500 }), () => toasts.show({ level: 'warn', title: 'Could not copy', detail: 'Select the command and copy it by hand.' }));
      });
    });
    root.querySelector('#suDismiss').addEventListener('click', () => api_.dismiss());
  }

  const api_ = {
    /* Reads the two sign-in sources and shows the banner only if something is missing and not dismissed. */
    async run() {
      const [conns, auth] = await Promise.all([api.get('/api/connections').catch(() => ({})), api.get('/api/auth/status').catch(() => ({}))]);
      items = missingSignins(conns, auth);
      paint();
      return items;
    },
    /* the banner belongs to HOME: it is hidden on every other screen */
    setScreen(id) {
      screen = id;
      paint();
    },
    dismiss() {
      if (prefs) prefs.write(DISMISSED_KEY, JSON.stringify(Array.from(new Set([...readDismissed(), ...items.map((i) => i.id)]))));
      paint();
    },
    /* the launcher's "Show sign-in help" */
    reopen() {
      if (prefs) prefs.write(DISMISSED_KEY, '[]');
      paint();
    },
    count: () => unsilenced(items, readDismissed()).length,
  };
  return api_;
}
