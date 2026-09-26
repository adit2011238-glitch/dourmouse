/* The startup sign-in check. The console had it as a separate script
   (assets/startup_check.js, a light-themed overlay that would flash on this
   dark shell). Kept: a real read of the three sign-in states (Claude CLI and
   Codex CLI from /api/connections, Google from /api/auth/status) and a dialog
   that shows the EXACT command to paste. Dropped on purpose: the loading
   overlay, because the shell paints at once and the check never blocks it.

   A failed read shows nothing (as the console's did): silence here means "no
   sign-in problem was reported", never "everything is signed in". */

import { html, setHtml } from '../kit/html.js';

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

export function createStartupCheck({ root, api, toasts }) {
  let def = null;

  function paint(items) {
    setHtml(root, html`
      <div class="cc-head"><span>Finish signing in</span></div>
      ${items.map((it) => html`<div class="su-item">
        <div class="su-label">${it.label}</div>
        ${it.command ? html`<button type="button" class="su-cmd" data-copy="${it.command}" title="Click to copy" data-spec="Copies this command so you can paste it into a terminal.">${it.command}</button>` : ''}
        ${it.href ? html`<a class="os-btn" href="${it.href}" data-spec="Starts the Google sign-in for this app.">Sign in with Google</a>` : ''}
      </div>`)}
      <div class="su-foot"><button type="button" class="os-btn" id="suDismiss" data-spec="Closes this. The app works without these, and asks again next time it opens.">Continue anyway</button></div>`);
    root.querySelectorAll('[data-copy]').forEach((b) => {
      b.addEventListener('click', () => {
        const text = b.dataset.copy;
        const write = navigator.clipboard && navigator.clipboard.writeText ? navigator.clipboard.writeText(text) : Promise.reject(new Error('clipboard not available here'));
        write.then(() => toasts.show({ level: 'ok', title: 'Copied', detail: text, ttl: 2500 }), () => toasts.show({ level: 'warn', title: 'Could not copy', detail: 'Select the command and copy it by hand.' }));
      });
    });
    root.querySelector('#suDismiss').addEventListener('click', () => def && def.close());
  }

  return {
    bind(panelDef) {
      def = panelDef;
    },
    /* Reads the two sign-in sources and opens the dialog only if something is missing. */
    async run() {
      const [conns, auth] = await Promise.all([api.get('/api/connections').catch(() => ({})), api.get('/api/auth/status').catch(() => ({}))]);
      const items = missingSignins(conns, auth);
      if (items.length && def) {
        paint(items);
        def.open();
      }
      return items;
    },
  };
}
