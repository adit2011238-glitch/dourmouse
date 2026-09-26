/* Pending-approval bookkeeping per screen (a port of console.html:1606-1625
   and addApproval). DOM-free: the card that shows one lives in
   kit/approval-card.js. The approval gate is the only mutation path for chat
   tools, so this file is deliberately small and honest about failure.

   An entry is one confirmation_requested event. Deciding it POSTs
   /api/confirm {id, approved, tab_id}; the entry only becomes approved or
   declined once the server accepted that. */

import { ApiError } from './api.js';

const EMAIL_PREFIXES = ['Send Gmail to ', 'Send mail FROM the Dourmouse identity'];

export function isEmailSendPrompt(prompt) {
  const p = prompt || '';
  return EMAIL_PREFIXES.some((x) => p.startsWith(x));
}

export function createApprovals({ api, scope }) {
  const byId = new Map();
  const pendingByKey = new Map();
  const listeners = new Set();
  const emit = (entry) => listeners.forEach((fn) => {
    try {
      fn(entry);
    } catch (err) {
      console.error(err);
    }
  });

  async function confirm(id, approved) {
    try {
      return await api.post('/api/confirm', { id, approved: Boolean(approved), tab_id: scope.tabId() });
    } catch (err) {
      if (err instanceof ApiError && err.body && err.body.ok === false && !err.body.error) {
        throw new ApiError('That approval is no longer pending on the server. It may have timed out or been answered already.', {
          status: err.status,
          body: err.body,
          path: err.path,
        });
      }
      throw err;
    }
  }

  const approvals = {
    /* Records a confirmation_requested event for a screen's thread. */
    add(key, evt) {
      if (!evt || !evt.id) return null;
      const existing = byId.get(evt.id);
      if (existing) return existing;
      const entry = {
        id: evt.id,
        key,
        prompt: evt.prompt || '',
        tool: evt.tool || '',
        autonomous: Boolean(evt.autonomous),
        email: isEmailSendPrompt(evt.prompt),
        state: 'pending',
        error: '',
        busy: false,
      };
      byId.set(entry.id, entry);
      pendingByKey.set(key, entry);
      emit(entry);
      return entry;
    },
    get(id) {
      return byId.get(id) || null;
    },
    /* The screen's one open approval, if any (so typing "send it" while the
       turn that opened it is still streaming can resolve it directly). */
    pending(key) {
      const e = pendingByKey.get(key);
      return e && e.state === 'pending' ? e : null;
    },
    /* Resolves an entry. Returns true when the server recorded the decision. */
    async decide(id, approved) {
      const entry = byId.get(id);
      if (!entry || entry.state !== 'pending' || entry.busy) return false;
      entry.busy = true;
      entry.error = '';
      emit(entry);
      try {
        await confirm(id, approved);
        entry.state = approved ? 'approved' : 'declined';
        if (pendingByKey.get(entry.key) === entry) pendingByKey.delete(entry.key);
        return true;
      } catch (err) {
        entry.error = err && err.message ? err.message : String(err);
        return false;
      } finally {
        entry.busy = false;
        emit(entry);
      }
    },
    /* STOP: decline every open approval on the thread so the blocked server
       thread wakes instead of waiting out its 300 s timeout. */
    async declineAll(key) {
      const open = Array.from(byId.values()).filter((e) => e.key === key && e.state === 'pending');
      await Promise.all(open.map((e) => approvals.decide(e.id, false)));
      return open.length;
    },
    onChange(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    count: () => listeners.size,
    openCount() {
      let n = 0;
      byId.forEach((e) => {
        if (e.state === 'pending') n += 1;
      });
      return n;
    },
  };
  return approvals;
}
