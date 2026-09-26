/* The status the menubar cluster and the Control Centre tiles share. One
   owner, four real reads, no invented numbers:
     agents    GET /api/activity            live via agent_activity
     security  GET /api/security_dashboard  live via security_scan
     network   GET /api/security/network    live via security_network_change
     brain     GET /api/backend
   Each read fails on its own and keeps the server's own message, so a tile can
   say exactly what is wrong instead of showing a stale or made-up value. */

const POLL_MS = 30000;

export function createStatus({ api, events, timers }) {
  const state = {
    agents: { known: false, total: 0, busy: 0, error: '' },
    security: { known: false, scanned: false, risk: 0, high: 0, med: 0, low: 0, lastScanAt: null, error: '' },
    network: { known: false, watching: false, gateway: '', iface: '', ssid: '', changes: 0, error: '' },
    brain: { known: false, backend: '', model: '', error: '' },
    events: 'connecting',
  };
  const activity = new Map();
  const listeners = new Set();
  const emit = () => listeners.forEach((fn) => {
    try {
      fn(state);
    } catch (err) {
      console.error(err);
    }
  });

  function recountAgents() {
    let busy = 0;
    activity.forEach((s) => {
      if (s !== 'idle') busy += 1;
    });
    state.agents.total = activity.size;
    state.agents.busy = busy;
    state.agents.known = true;
    state.agents.error = '';
  }

  const msg = (err) => (err && err.message ? err.message : String(err));

  async function readAgents() {
    try {
      const d = await api.get('/api/activity');
      activity.clear();
      Object.entries(d.agents || {}).forEach(([name, a]) => activity.set(name, (a && a.status) || 'idle'));
      recountAgents();
    } catch (err) {
      state.agents.error = msg(err);
    }
  }

  async function readSecurity() {
    try {
      const d = await api.get('/api/security_dashboard');
      const sev = d.findings_by_severity || {};
      state.security = {
        known: true,
        scanned: Boolean(d.scanned),
        risk: Number(d.risk_score) || 0,
        high: sev.high || 0,
        med: sev.med || 0,
        low: sev.low || 0,
        lastScanAt: d.last_scan_at || null,
        error: '',
      };
    } catch (err) {
      state.security.error = msg(err);
    }
  }

  async function readNetwork() {
    try {
      const d = await api.get('/api/security/network');
      const id = Array.isArray(d.identity) ? d.identity : [];
      state.network = {
        known: true,
        watching: Boolean(d.watching),
        gateway: id[0] || '',
        iface: id[1] || '',
        ssid: id[2] || '',
        changes: d.changes || 0,
        error: '',
      };
    } catch (err) {
      state.network.error = msg(err);
    }
  }

  async function readBrain() {
    try {
      const d = await api.get('/api/backend');
      state.brain = { known: true, backend: d.backend || '', model: d.model || '', error: '' };
    } catch (err) {
      state.brain.error = msg(err);
    }
  }

  const status = {
    state,
    async refresh() {
      await Promise.allSettled([readAgents(), readSecurity(), readNetwork(), readBrain()]);
      emit();
    },
    onChange(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    count: () => listeners.size,
    start() {
      events.on('agent_activity', (e) => {
        Object.entries(e.agents || {}).forEach(([name, a]) => activity.set(name, (a && a.status) || 'idle'));
        recountAgents();
        emit();
      });
      events.on('security_scan', (e) => {
        const c = e.counts || {};
        state.security = {
          known: true,
          scanned: true,
          risk: state.security.risk,
          high: c.high || 0,
          med: c.med || 0,
          low: c.low || 0,
          lastScanAt: e.at || Date.now() / 1000,
          error: '',
        };
        emit();
        /* the event carries counts only, so re-read the risk score */
        readSecurity().then(emit);
      });
      events.on('security_network_change', () => readNetwork().then(emit));
      events.onStatus((s) => {
        state.events = s === 'open' ? 'live' : 'reconnecting';
        emit();
      });
      events.onResync(() => status.refresh());
      timers.every(POLL_MS, () => status.refresh(), 'chrome');
      return status.refresh();
    },
  };
  return status;
}
