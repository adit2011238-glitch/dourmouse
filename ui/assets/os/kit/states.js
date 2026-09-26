/* The six states every data region renders (DESIGN_SYSTEM.md): loading,
   populated, empty, stale, unavailable, error. This is what replaces a
   fabricated placeholder value. Errors show the server's own message.

   Each call sets data-state on the region so tests and the verification run
   can read which state is showing without guessing from text. */

import { html, setHtml } from './html.js';
import { isAbort } from '../core/api.js';

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
    if (content && typeof content === 'object' && 'text' in content && typeof content.text === 'string') {
      setHtml(root, content);
    } else if (Array.isArray(content)) {
      root.replaceChildren(...content);
    } else if (content) {
      root.replaceChildren(content);
    }
    return root;
  },

  empty(root, message, { hint = '' } = {}) {
    return put(root, 'empty', html`<div class="st st-empty"><div class="st-m">${message}</div>${hint ? html`<div class="st-d">${hint}</div>` : ''}</div>`);
  },

  /* The thing behind this region is not running or not configured. */
  unavailable(root, message, { detail = '', retry = null } = {}) {
    put(root, 'unavailable', html`<div class="st st-unavailable" role="status"><div class="st-t">Not available</div><div class="st-m">${message}</div>${detail ? html`<div class="st-d">${detail}</div>` : ''}${retry ? html`<button type="button" class="os-btn" data-st-retry>RETRY</button>` : ''}</div>`);
    if (retry) root.querySelector('[data-st-retry]').addEventListener('click', retry);
    return root;
  },

  /* err is an ApiError (server's own message, status, path) or anything
     thrown. An aborted request is a screen being left, never an error. */
  error(root, err, { retry = null, title = 'Could not load this' } = {}) {
    if (isAbort(err)) return false;
    if (err && err.offline) {
      return states.unavailable(root, err.message, { detail: err.path ? 'while reading ' + err.path : '', retry });
    }
    const message = err && err.message ? err.message : String(err);
    const bits = [];
    if (err && err.status) bits.push('HTTP ' + err.status);
    if (err && err.path) bits.push(err.path);
    put(root, 'error', html`<div class="st st-error" role="alert"><div class="st-t">${title}</div><div class="st-m">${message}</div>${bits.length ? html`<div class="st-d">${bits.join(' / ')}</div>` : ''}${retry ? html`<button type="button" class="os-btn" data-st-retry>RETRY</button>` : ''}</div>`);
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
