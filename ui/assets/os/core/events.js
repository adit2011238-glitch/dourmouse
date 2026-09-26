/* The ONE EventSource. Screens never open their own: every long-lived stream
   holds a server thread (webui.py _handle_events blocks in rfile.read), and
   the console shares one for the same reason.

   The hub keeps no event ids and replays nothing, so anything broadcast while
   the stream was down is lost. That is why there is onResync: after a
   reconnect every live screen must re-read what it shows. */

const CLOSED = 2;

function matcher(spec) {
  if (typeof spec === 'function') return spec;
  if (typeof spec === 'string' && spec.endsWith('*')) {
    const prefix = spec.slice(0, -1);
    return (evt) => typeof evt.type === 'string' && evt.type.startsWith(prefix);
  }
  return (evt) => evt.type === spec;
}

export function createEvents({
  url = '/api/events',
  EventSourceImpl = globalThis.EventSource,
  setTimer = globalThis.setTimeout,
  clearTimer = globalThis.clearTimeout,
  onError = (err) => console.error(err),
} = {}) {
  const handlers = new Set();
  const resyncs = new Set();
  const statusFns = new Set();
  let es = null;
  let everOpened = false;
  let dropped = false;
  let connected = false;
  let delay = 1000;
  let timer = null;
  let stopped = false;

  const safe = (fn, arg) => {
    try {
      fn(arg);
    } catch (err) {
      onError(err);
    }
  };
  const status = (s) => statusFns.forEach((fn) => safe(fn, s));

  function dispatch(evt) {
    for (const h of Array.from(handlers)) {
      let hit = false;
      try {
        hit = h.match(evt);
      } catch (err) {
        onError(err);
      }
      if (hit) safe(h.fn, evt);
    }
  }

  function open() {
    if (stopped || !EventSourceImpl) return;
    es = new EventSourceImpl(url);
    es.onopen = () => {
      connected = true;
      delay = 1000;
      status('open');
      if (everOpened && dropped) resyncs.forEach((fn) => safe(fn, undefined));
      everOpened = true;
      dropped = false;
    };
    es.onmessage = (m) => {
      let evt;
      try {
        evt = JSON.parse(m.data);
      } catch (_err) {
        return;
      }
      if (evt && typeof evt === 'object') dispatch(evt);
    };
    es.onerror = () => {
      connected = false;
      dropped = true;
      status('error');
      /* A closed source (the server answered non-200, or went away hard) will
         not retry by itself; a connecting one will. */
      if (es && es.readyState === CLOSED && !timer && !stopped) {
        timer = setTimer(() => {
          timer = null;
          open();
        }, delay);
        delay = Math.min(delay * 2, 15000);
      }
    };
  }

  function makeScope() {
    const mine = [];
    const track = (set, item) => {
      set.add(item);
      const off = () => set.delete(item);
      mine.push(off);
      return off;
    };
    return {
      /* type string ("security_scan"), prefix ("security_*") or predicate */
      on(spec, fn) {
        return track(handlers, { match: matcher(spec), fn });
      },
      onResync(fn) {
        return track(resyncs, fn);
      },
      onStatus(fn) {
        return track(statusFns, fn);
      },
      /* Removes everything this scope subscribed. Called on unmount. */
      dispose() {
        mine.splice(0).forEach((off) => off());
      },
    };
  }

  const root = makeScope();
  return {
    start() {
      stopped = false;
      if (!es) open();
    },
    stop() {
      stopped = true;
      if (timer) clearTimer(timer);
      timer = null;
      if (es) es.close();
      es = null;
      connected = false;
    },
    scope: makeScope,
    on: root.on,
    onResync: root.onResync,
    onStatus: root.onStatus,
    connected: () => connected,
    /* Live subscription count, for window.__dmShell.stats(). */
    count: () => handlers.size + resyncs.size + statusFns.size,
    sources: () => (es ? 1 : 0),
    /* test hook: deliver an event as if it came off the wire */
    _dispatch: dispatch,
  };
}
