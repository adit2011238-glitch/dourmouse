/* The one POST /api/chat stream parser and the per-screen threads. Today the
   same parser is copied into seven files (docs/UI_SOURCE_MAP.md); this is the
   single port of console.html run(), DOM-free so node can test it.

   A thread is a screen's conversation, keyed like the console's threadKey():
   the six screens with their own thread keep it, every other screen falls
   back to HOME's. Threads live here, not in a screen, so a reply that lands
   while you are on another screen is still there when you come back. */

import { ring } from './ring.js';
import { isAbort } from './api.js';

export const AFFIRM = new Set([
  'yes', 'y', 'yeah', 'yep', 'confirm', 'confirmed', 'approve', 'approved', 'send',
  'send it', 'go', 'go ahead', 'do it', 'do it now', 'send now', 'yes send',
  'yes send it', 'yes go ahead', 'yes do it', 'ok send it', 'ok go ahead',
  'okay send it', 'confirm it',
]);

/* A curated whole-message set, never a substring test (mirrors webui.py). */
export function isImperativeAffirm(text) {
  const t = String(text || '').trim().toLowerCase().replace(/[!.\s]+$/, '').trim().replace(/\s+/g, ' ');
  return AFFIRM.has(t);
}

const TURN_CAP = 200;
const STEP_CAP = 200;
const QUEUE_CAP = 20;
const THINK_CAP = 20000;
const RESULT_CAP = 8000;

function capPush(arr, item, cap) {
  arr.push(item);
  if (arr.length > cap) arr.splice(0, arr.length - cap);
}

function prettyArgs(raw) {
  if (!raw) return '';
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch (_err) {
    return String(raw);
  }
}

function modelLabel(e) {
  const raw = e.model || '';
  return String(raw).replace(/^server:/, '').split('/').pop() || '';
}

let turnSeq = 0;

export function newTurn(text, now = Date.now()) {
  turnSeq += 1;
  return {
    id: 't' + turnSeq,
    text: String(text),
    at: now,
    startedAt: now,
    elapsedMs: null,
    status: 'thinking', /* thinking generating working waiting done stopped error */
    model: '',
    alive: 0,
    think: '',
    steps: [],
    reply: '',
    error: '',
    notes: [],
    approvals: [],
    restored: false,
  };
}

/* Folds one SSE event into a turn. Pure, so the whole switch is testable. */
export function applyEvent(turn, e, hooks = {}) {
  switch (e.type) {
    case 'brain':
      turn.model = modelLabel(e) || turn.model;
      turn.status = 'generating';
      break;
    case 'brain_thinking':
      turn.status = 'thinking';
      turn.alive = e.elapsed_s || 0;
      break;
    case 'thinking_delta':
      turn.think = (turn.think + (e.text || '')).slice(-THINK_CAP);
      break;
    case 'plan':
      capPush(turn.steps, { kind: 'plan', label: 'PLAN' }, STEP_CAP);
      break;
    case 'tool_use':
    case 'function': {
      const name = e.name || e.tool || 'tool';
      capPush(turn.steps, { kind: 'tool', name, args: prettyArgs(e.raw_arguments), result: '', running: true, ok: true }, STEP_CAP);
      turn.status = 'working';
      break;
    }
    case 'tool_result':
    case 'result': {
      const name = e.name || e.tool || '';
      let step = null;
      for (let i = turn.steps.length - 1; i >= 0; i -= 1) {
        const s = turn.steps[i];
        if (s.kind === 'tool' && s.running && (!name || s.name === name)) {
          step = s;
          break;
        }
      }
      if (!step) {
        step = { kind: 'tool', name: name || 'tool', args: '', result: '', running: false, ok: true };
        capPush(turn.steps, step, STEP_CAP);
      }
      step.running = false;
      step.result = String(e.text || '').slice(0, RESULT_CAP);
      break;
    }
    case 'delegate_parallel_branch': {
      const label = 'branch ' + ((e.index ?? 0) + 1) + '/' + (e.total || '?') + ' ' + (e.agent || 'any') + ' ' + (modelLabel(e) || '-');
      if (e.phase === 'start') {
        capPush(turn.steps, { kind: 'tool', name: label, args: e.task || '', result: '', running: true, ok: true }, STEP_CAP);
      } else if (e.phase === 'result') {
        let step = null;
        for (let i = turn.steps.length - 1; i >= 0; i -= 1) {
          if (turn.steps[i].kind === 'tool' && turn.steps[i].running && turn.steps[i].name === label) {
            step = turn.steps[i];
            break;
          }
        }
        if (!step) {
          step = { kind: 'tool', name: label, args: '', result: '', running: false, ok: true };
          capPush(turn.steps, step, STEP_CAP);
        }
        step.running = false;
        step.ok = Boolean(e.ok);
        step.result = e.ok ? String(e.text || '').slice(0, RESULT_CAP) : 'ERROR: ' + (e.error || '');
      }
      break;
    }
    case 'delegate_parallel_burst':
      capPush(turn.steps, { kind: 'plan', label: 'FAN-OUT ' + e.total + ' branches, ' + e.concurrent + ' at once' }, STEP_CAP);
      break;
    case 'confirmation_requested':
      turn.status = 'waiting';
      if (hooks.approval) {
        const entry = hooks.approval(e);
        if (entry) capPush(turn.approvals, entry.id, 20);
      }
      break;
    case 'confirmation_resolved':
      capPush(turn.steps, { kind: 'plan', label: (e.approved ? 'APPROVED ' : 'DECLINED ') + (e.tool || '') }, STEP_CAP);
      break;
    case 'allhands_started':
      if (e.run_id) capPush(turn.notes, { kind: 'allhands', run_id: String(e.run_id) }, 20);
      break;
    case 'assistant_delta':
      turn.reply += e.text || '';
      break;
    case 'assistant_text':
      /* the whole text so far, not a delta */
      turn.reply = String(e.text || '');
      break;
    case 'error':
      turn.error = e.message || 'The server reported an error.';
      capPush(turn.steps, { kind: 'error', label: 'ERROR', result: turn.error }, STEP_CAP);
      break;
    case 'done':
      if (e.final_text) turn.reply = String(e.final_text);
      break;
    default:
      break;
  }
  return turn;
}

/* One record of the session ledger (GET /api/session/current) as a finished
   turn. The approval box is not restored as a live card: its confirmation is
   long resolved, and showing it again would invite clicking a dead action. */
export function turnFromLedger(rec) {
  const turn = newTurn(rec.display_text || rec.user || '', 0);
  turn.restored = true;
  turn.status = 'done';
  turn.reply = rec.final_text || '';
  turn.elapsedMs = typeof rec.elapsed_ms === 'number' ? rec.elapsed_ms : null;
  const ts = rec.timestamp ? Date.parse(rec.timestamp) : NaN;
  turn.at = Number.isFinite(ts) ? ts : 0;
  for (const e of rec.transcript || []) {
    if (!e || typeof e !== 'object') continue;
    if (e.type === 'confirmation_requested') {
      capPush(turn.steps, { kind: 'plan', label: 'APPROVAL REQUESTED ' + (e.tool || '') + (e.prompt ? ': ' + e.prompt : '') }, STEP_CAP);
    } else if (e.type === 'assistant_delta' || e.type === 'assistant_text' || e.type === 'done') {
      continue;
    } else {
      applyEvent(turn, e);
    }
  }
  for (const s of turn.steps) if (s.running) s.running = false;
  turn.status = 'done';
  if (turn.error && !turn.reply) turn.status = 'error';
  return turn;
}

export function createChat({ api, scope, approvals, threadScreens = [], storage } = {}) {
  const threads = new Map();
  const busyListeners = new Set();
  const store = storage === undefined ? (() => {
    try {
      return globalThis.sessionStorage;
    } catch (_err) {
      return null;
    }
  })() : storage;
  const AUTO_KEY = 'dm_autonomous_v1';
  let autonomous = false;
  try {
    autonomous = Boolean(store) && store.getItem(AUTO_KEY) === '1';
  } catch (_err) {
    autonomous = false;
  }

  const keyFor = (screen) => (threadScreens.includes(screen) ? screen : 'HOME');

  function makeThread(key) {
    const listeners = new Set();
    const turns = [];
    const queue = ring(QUEUE_CAP);
    let busy = false;
    let current = null;
    let stopped = false;

    const emit = (kind, turn) => listeners.forEach((fn) => {
      try {
        fn({ kind, turn, thread });
      } catch (err) {
        console.error(err);
      }
    });
    const setBusy = (on) => {
      busy = on;
      busyListeners.forEach((fn) => {
        try {
          fn(key, on);
        } catch (err) {
          console.error(err);
        }
      });
      emit('busy', null);
    };

    async function run(text, opts) {
      const turn = newTurn(text);
      capPush(turns, turn, TURN_CAP);
      stopped = false;
      const ctrl = new AbortController();
      current = { ctrl, turn };
      setBusy(true);
      emit('turn', turn);
      const body = { prompt: turn.text, screen: key, tab_id: scope.tabId(), autonomous: Boolean(autonomous) };
      if (opts && opts.focusAgent) body.focus_agent = opts.focusAgent;
      if (opts && opts.forceBackend) body.force_backend = opts.forceBackend;
      try {
        await api.stream('/api/chat', body, (e) => {
          applyEvent(turn, e, {
            approval: (evt) => approvals.add(key, evt),
          });
          emit('turn', turn);
        }, { signal: ctrl.signal });
        if (!stopped && turn.status !== 'error') turn.status = 'done';
      } catch (err) {
        if (stopped || isAbort(err)) {
          turn.status = 'stopped';
        } else {
          turn.status = 'error';
          turn.error = (err && err.message) || String(err);
          capPush(turn.steps, { kind: 'error', label: 'REQUEST FAILED', result: turn.error }, STEP_CAP);
        }
      } finally {
        turn.elapsedMs = Date.now() - turn.startedAt;
        for (const s of turn.steps) if (s.running) s.running = false;
        if (turn.status === 'done' && !turn.reply && turn.error) turn.status = 'error';
        current = null;
        setBusy(false);
        emit('turn', turn);
      }
      return turn;
    }

    async function drain() {
      for (;;) {
        const items = queue.items();
        if (!items.length || busy) return;
        const next = items[0];
        const rest = items.slice(1);
        queue.clear();
        rest.forEach((x) => queue.push(x));
        emit('queue', null);
        await run(next, {});
      }
    }

    const thread = {
      key,
      turns: () => turns.slice(),
      busy: () => busy,
      queue: () => queue.items(),
      current: () => (current ? current.turn : null),
      subscribe(fn) {
        listeners.add(fn);
        return () => listeners.delete(fn);
      },
      listenerCount: () => listeners.size,
      /* Sends a directive, or queues it while a turn runs. Typing "send it"
         while an approval is open resolves that approval directly. */
      async send(text, opts = {}) {
        const t = String(text || '').trim();
        if (!t) return null;
        if (busy) {
          const pend = approvals.pending(key);
          if (pend && isImperativeAffirm(t)) {
            await approvals.decide(pend.id, true);
            return null;
          }
          queue.push(t);
          emit('queue', null);
          return null;
        }
        const turn = await run(t, opts);
        await drain();
        return turn;
      },
      /* Aborts the stream and declines any open approval so the server thread
         blocked on it wakes at once. */
      async stop() {
        if (!busy) return false;
        stopped = true;
        try {
          if (current) current.ctrl.abort();
        } catch (_err) {
          /* already aborted */
        }
        queue.clear();
        await approvals.declineAll(key);
        return true;
      },
      /* Replaces the thread with turns read back from the ledger. */
      restore(records) {
        turns.length = 0;
        for (const rec of records) capPush(turns, turnFromLedger(rec), TURN_CAP);
        emit('restore', null);
      },
      clear() {
        turns.length = 0;
        emit('restore', null);
      },
    };
    return thread;
  }

  return {
    keyFor,
    thread(screen) {
      const key = keyFor(screen);
      let t = threads.get(key);
      if (!t) {
        t = makeThread(key);
        threads.set(key, t);
      }
      return t;
    },
    /* Fires (key, busy) whenever any thread starts or ends a run. */
    onBusy(fn) {
      busyListeners.add(fn);
      return () => busyListeners.delete(fn);
    },
    busyKeys() {
      return Array.from(threads.values()).filter((t) => t.busy()).map((t) => t.key);
    },
    /* The per-tab, per-request flag console.html sends as `autonomous`. It
       raises the step ceiling for the next turns. It is NOT auto-approve:
       every gated action still asks. */
    autonomous: () => autonomous,
    setAutonomous(on) {
      autonomous = Boolean(on);
      try {
        if (store) {
          if (autonomous) store.setItem(AUTO_KEY, '1');
          else store.removeItem(AUTO_KEY);
        }
      } catch (_err) {
        /* per-load then */
      }
    },
    listenerCount() {
      let n = busyListeners.size;
      threads.forEach((t) => {
        n += t.listenerCount();
      });
      return n;
    },
  };
}
