/* The six states every data region renders (DESIGN_SYSTEM.md): loading,
   populated, empty, stale, unavailable, error. This is what replaces a
   fabricated placeholder value. Errors show the server's own message.

   Each call sets data-state on the region so tests and the verification run
   can read which state is showing without guessing from text. */

import { html, setHtml, SafeHtml } from './html.js';
import { isAbort } from '../core/api.js';

/* S18 and F19: a person is never shown an environment variable name, a dotenv file or a "restart the
   server" instruction in the main line of a state. splitDeveloper() takes a message and returns
   { plain, dev }: the sentences that read like configuration go to dev (shown inside a "For developers"
   disclosure), the rest stay as the sentence a person reads. Pure, so node can test it. */
const DEV_SENTENCE = /\b[A-Z][A-Z0-9]*_[A-Z0-9_]{2,}\b|\.env\b|local_secrets\.py|restart the server|\/api\/[a-z]/;
export function splitDeveloper(text) {
  const parts = String(text || '').split(/(?<=[.!?])\s+(?=[A-Z])/).map((x) => x.trim()).filter(Boolean);
  const plain = [];
  const dev = [];
  parts.forEach((sentence) => (DEV_SENTENCE.test(sentence) ? dev : plain).push(sentence));
  return { plain: plain.join(' '), dev: dev.join(' ') };
}

function put(root, state, safe) {
  root.dataset.state = state;
  setHtml(root, safe);
  return root;
}

export const states = {
  loading(root, label = 'Loading') {
    return put(root, 'loading', html`<div class="st st-loading" role="status" aria-live="polite"><span class="st-spin" aria-hidden="true"></span><span>${label}</span></div>`);
  },

  /* Content is a SafeHtml, a Node, or an array of Nodes. */
  populated(root, content) {
    root.dataset.state = 'populated';
    if (content instanceof SafeHtml) {
      setHtml(root, content);
    } else if (Array.isArray(content)) {
      root.replaceChildren(...content);
    } else if (content) {
      root.replaceChildren(content);
    }
    return root;
  },

  /* action: { label, onClick }: the one button that fills this empty state, in the middle of it (F29) */
  empty(root, message, { hint = '', action = null } = {}) {
    put(root, 'empty', html`<div class="st st-empty"><div class="st-m">${message}</div>${hint ? html`<div class="st-d">${hint}</div>` : ''}${action ? html`<div class="st-btns"><button type="button" class="os-btn os-btn--primary" data-st-action>${action.label}</button></div>` : ''}</div>`);
    if (action && action.onClick) root.querySelector('[data-st-action]').addEventListener('click', action.onClick);
    return root;
  },

  /* The thing behind this region is not running or not configured.
     action: { label, href | onClick }: the one thing that fixes it, as a primary button.
     retry is offered only when it can help (pass none for a state that needs setting up first).
     Any configuration wording in message or detail moves into a "For developers" disclosure. */
  unavailable(root, message, { detail = '', retry = null, action = null, developer = '' } = {}) {
    const m = splitDeveloper(message);
    const d = splitDeveloper(detail);
    const dev = [m.dev, d.dev, developer].filter(Boolean).join(' ');
    const main = m.plain || (dev ? 'This is not set up yet.' : '');
    const act = action
      ? (action.href ? html`<a class="os-btn os-btn--primary" href="${action.href}" data-st-action>${action.label}</a>` : html`<button type="button" class="os-btn os-btn--primary" data-st-action>${action.label}</button>`)
      : '';
    put(root, 'unavailable', html`<div class="st st-unavailable" role="status"><div class="st-t">Not available</div><div class="st-m">${main}</div>${d.plain ? html`<div class="st-d">${d.plain}</div>` : ''}${dev ? html`<details class="st-dev"><summary>For developers</summary><div class="st-d">${dev}</div></details>` : ''}${act || retry ? html`<div class="st-btns">${act}${retry ? html`<button type="button" class="os-btn" data-st-retry>Retry</button>` : ''}</div>` : ''}</div>`);
    if (retry) root.querySelector('[data-st-retry]').addEventListener('click', retry);
    if (action && action.onClick) root.querySelector('[data-st-action]').addEventListener('click', action.onClick);
    return root;
  },

  /* err is an ApiError (server's own message, status, path) or anything
     thrown. An aborted request is a screen being left, never an error. */
  error(root, err, { retry = null, title = 'Could not load this' } = {}) {
    if (isAbort(err)) return false;
    if (err && err.offline) {
      return states.unavailable(root, err.message, { detail: err.path ? 'while reading ' + err.path : '', retry });
    }
    const full = err && err.message ? err.message : String(err);
    const split = splitDeveloper(full);
    const message = split.plain || full;
    const bits = [];
    if (err && err.status) bits.push('HTTP ' + err.status);
    if (err && err.path) bits.push(err.path);
    put(root, 'error', html`<div class="st st-error" role="alert"><div class="st-t">${title}</div><div class="st-m">${message}</div>${split.plain && split.dev ? html`<details class="st-dev"><summary>For developers</summary><div class="st-d">${split.dev}</div></details>` : ''}${bits.length ? html`<div class="st-d">${bits.join(' / ')}</div>` : ''}${retry ? html`<button type="button" class="os-btn" data-st-retry>Retry</button>` : ''}</div>`);
    if (retry) root.querySelector('[data-st-retry]').addEventListener('click', retry);
    return root;
  },

  /* Cached data shown while the network is down: a banner over the content
     that is still there. Cached data is never presented as live. */
  stale(root, message) {
    root.querySelectorAll(':scope > .st-stale').forEach((n) => n.remove());
    const b = document.createElement('div');
    b.className = 'st-stale';
    b.setAttribute('role', 'status');
    b.textContent = message;
    root.prepend(b);
    root.dataset.state = 'stale';
    return b;
  },

  clearStale(root) {
    root.querySelectorAll(':scope > .st-stale').forEach((n) => n.remove());
    if (root.dataset.state === 'stale') root.dataset.state = 'populated';
  },
};
