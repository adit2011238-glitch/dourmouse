/**
 * Dourmouse remote-control bridge (v13).
 *
 * The globe's own real action interface — createGevActionRunner() in
 * src/voice/gevActions.js — is already wired into voice control via
 * window.__godsEyeView.voiceCommands.runner. This module is the OTHER
 * side of the server-side queue in vite.config.js's
 * dourmouseActionBridgeProxy(): it long-polls for actions an external
 * caller (Dourmouse) queued, runs each through the SAME real runner
 * voice control uses (never a second, parallel action surface that could
 * drift from it), and reports the real result back.
 *
 * Started once from main.js, right after initGevVoiceCommands() sets
 * window.__godsEyeView.voiceCommands — see startDourmouseBridge()'s own
 * guard for what happens if that never happened (initialization failed
 * upstream): this degrades to a quiet no-op poll loop that reports the
 * honest reason on every action it's handed, rather than throwing and
 * taking down anything else in the page.
 */

const PENDING_URL = '/api/dourmouse/pending';
const RESULT_URL = '/api/dourmouse/result';

let _running = false;

async function runOneAction({ id, name, args }) {
  const runner = window.__godsEyeView?.voiceCommands?.runner;
  if (typeof runner !== 'function') {
    return {
      id,
      result: {
        ok: false,
        error: "God's Eye View voice/action runner is not ready yet — the globe may still be loading, or its own initialization failed (see the browser console).",
      },
    };
  }
  try {
    const result = await runner(name, args || {});
    return { id, result: result ?? { ok: true, action: name } };
  } catch (error) {
    return { id, result: { ok: false, error: String(error?.message || error) } };
  }
}

async function pollOnce() {
  const res = await fetch(PENDING_URL, { cache: 'no-store' });
  if (!res.ok) throw new Error(`pending poll failed: HTTP ${res.status}`);
  const { actions } = await res.json();
  if (!actions || actions.length === 0) return;
  // Real branch-independent execution, same reasoning as this app's own
  // AIS/opensky proxies processing rows independently: one action's
  // failure must never block another's real result from being reported.
  await Promise.all(
    actions.map(async (action) => {
      const { id, result } = await runOneAction(action);
      try {
        await fetch(RESULT_URL, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id, result }),
        });
      } catch {
        // The server-side wait for this id will simply time out honestly;
        // nothing else to do from here (the round trip already failed).
      }
    })
  );
}

async function loop() {
  while (_running) {
    try {
      await pollOnce();
    } catch {
      // A transient fetch/network failure must never kill the loop — the
      // server side's own long-poll already bounds how often this fires,
      // so a brief pause here is just extra politeness on a real error.
      await new Promise((r) => setTimeout(r, 1000));
    }
  }
}

export function startDourmouseBridge() {
  if (_running) return; // idempotent — main.js only calls this once, but never assume
  _running = true;
  loop();
}

export function stopDourmouseBridge() {
  _running = false;
}
