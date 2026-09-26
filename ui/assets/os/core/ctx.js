/* The per-screen context (architecture section 2.2) and the shared timer
   service. Everything a screen subscribes to is registered here so leaving the
   screen can undo all of it: abort the signal, drop event subscriptions, clear
   timers, unbind keys. A screen that follows the contract cannot leak.

   Timers exist only through every(): it pauses while the tab is hidden, so a
   screen the user cannot see does not poll. */

export function createTimers({ setInterval: setI = globalThis.setInterval, clearInterval: clearI = globalThis.clearInterval, doc = globalThis.document } = {}) {
  const live = new Set();
  return {
    every(ms, fn, owner) {
      const id = setI(() => {
        if (doc && doc.hidden) return;
        try {
          const r = fn();
          if (r && typeof r.catch === 'function') r.catch((err) => console.error(err));
        } catch (err) {
          console.error(err);
        }
      }, ms);
      const handle = { id, owner };
      live.add(handle);
      return () => {
        if (live.delete(handle)) clearI(id);
      };
    },
    count: () => live.size,
    countFor: (owner) => Array.from(live).filter((h) => h.owner === owner).length,
    clearOwner(owner) {
      Array.from(live).forEach((h) => {
        if (h.owner === owner) {
          live.delete(h);
          clearI(h.id);
        }
      });
    },
  };
}

/* deps: { api, events, chat, approvals, scope, host, prefs, keymap, timers,
           toasts, chrome, kit, renderApproval }
   chrome is the shell's stage/nav control surface, already bound to `screen`. */
export function createScreenCtx({ id, root, deps }) {
  const owner = { id };
  const ac = new AbortController();
  const events = deps.events.scope();
  const offs = [];
  let resyncRegistered = false;
  let disposed = false;

  const thread = deps.chat.thread(id);
  const ctx = {
    id,
    root,
    signal: ac.signal,
    api: deps.api.withSignal(ac.signal),
    chat: thread,
    events: {
      on: (spec, fn) => events.on(spec, fn),
      onResync(fn) {
        resyncRegistered = true;
        return events.onResync(fn);
      },
      onStatus: (fn) => events.onStatus(fn),
    },
    every: (ms, fn) => deps.timers.every(ms, fn, owner),
    notify: (n) => deps.toasts.show(n),
    approvals: {
      /* Renders (or re-renders) the approval card for a confirmation_requested
         event into `container` and POSTs /api/confirm on a click. */
      render: (container, evt) => deps.renderApproval(container, evt, id),
      pending: () => deps.approvals.pending(thread.key),
      declineAll: () => deps.approvals.declineAll(thread.key),
    },
    scope: {
      tabId: () => deps.scope.tabId(),
      project: () => deps.scope.project(),
      leaveProject: () => deps.scope.leaveProject(),
      onChange(fn) {
        const off = deps.scope.onChange(fn);
        offs.push(off);
        return off;
      },
    },
    chrome: deps.chrome,
    keys: {
      bind(combo, fn) {
        const off = deps.keymap.bind(combo, fn);
        offs.push(off);
        return off;
      },
    },
    host: deps.host,
    prefs: {
      get: (key) => deps.prefs.read('dm.os.' + id.toLowerCase() + '.' + key),
      set: (key, value) => deps.prefs.write('dm.os.' + id.toLowerCase() + '.' + key, String(value)),
    },
    kit: deps.kit,
  };

  return {
    ctx,
    owner,
    wantsResync: () => !resyncRegistered,
    dispose() {
      if (disposed) return;
      disposed = true;
      ac.abort();
      events.dispose();
      deps.timers.clearOwner(owner);
      offs.splice(0).forEach((off) => off());
    },
    counts() {
      return { timers: deps.timers.countFor(owner) };
    },
  };
}
