/* Google sign-in from inside the app window.

   Google refuses to sign in inside an embedded window, and the shell sends every off-site address
   to the system browser. A plain link to /api/auth/google/start therefore started the sign-in HERE
   (the server set its cookie on this window) while Google's consent page and its callback ran in
   the system browser, which has no such cookie. The callback then answered "this sign-in was not
   started in this browser".

   The way that works is the claim bridge the login page already uses: ask for the consent address
   as JSON with claim=1 (the server then binds no cookie to one browser), open it in the system
   browser, and poll /api/auth/claim from THIS window until the browser has finished and the
   session can be adopted. In a real browser tab there is no bridge and no problem: the plain
   redirect starts and finishes in the same browser. */

const START_PLAIN = '/api/auth/google/start';
const START_BRIDGE = '/api/auth/google/start?claim=1&format=json';
const CLAIM = '/api/auth/claim';

/* True when the app shell's bridge is present. A real browser never has it, so this is the honest
   test for "am I inside the app window" (the same one login.html uses). Pure, so node can test it. */
export function bridgeAvailable(win) {
  return !!(win && win.pywebview && win.pywebview.api && typeof win.pywebview.api.open_external === 'function');
}

const defaultSleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/* Starts the sign-in and reports each step through onStatus({ state, ... }). Resolves with
   { ok, outcome }: outcome is 'redirect' (a real browser tab, the page is leaving), 'signed-in',
   'error', 'dead-end' (this window cannot adopt a session), or 'timeout'. Never throws. */
export async function startGoogleSignIn({
  win = globalThis.window,
  fetchFn = globalThis.fetch,
  onStatus = () => {},
  pollMs = 2000,
  maxPolls = 150,
  sleep = defaultSleep,
} = {}) {
  if (!bridgeAvailable(win)) {
    win.location.href = START_PLAIN;
    return { ok: true, outcome: 'redirect' };
  }
  onStatus({ state: 'starting' });
  let url = '';
  try {
    // A plain 302 cannot be followed by fetch (the redirect to accounts.google.com is cross-origin),
    // so the start route answers {ok, url} when asked for JSON and still sets the claim cookie here.
    const r = await fetchFn(START_BRIDGE, { headers: { Accept: 'application/json' } });
    const body = await r.json().catch(() => null);
    if (!r.ok || !body || !/^https:\/\//i.test(String(body.url || ''))) {
      throw new Error((body && body.error) || 'HTTP ' + r.status);
    }
    url = String(body.url);
  } catch (err) {
    const message = err && err.message ? err.message : String(err);
    onStatus({ state: 'error', message });
    return { ok: false, outcome: 'error', message };
  }
  let opened = false;
  try {
    opened = (await win.pywebview.api.open_external(url)) !== false;
  } catch (_err) {
    opened = false;
  }
  onStatus(opened ? { state: 'waiting', url } : { state: 'manual', url });
  for (let n = 0; n < maxPolls; n += 1) {
    await sleep(pollMs);
    let r;
    try {
      r = await fetchFn(CLAIM);
    } catch (_err) {
      continue; // the server is restarting mid-flow: keep waiting
    }
    if (r.ok) {
      onStatus({ state: 'signed-in' });
      return { ok: true, outcome: 'signed-in' };
    }
    // 403 (not the app window) and 400 (this window started no sign-in) never change by waiting.
    if (r.status === 403 || r.status === 400) {
      onStatus({ state: 'dead-end', status: r.status });
      return { ok: false, outcome: 'dead-end', status: r.status };
    }
  }
  onStatus({ state: 'timeout' });
  return { ok: false, outcome: 'timeout' };
}
