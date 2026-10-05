/* Pure helpers for APPS (no DOM at import time, so node can test them). */

const PANE_URL = 'x-apple.systempreferences:com.apple.preference.security?';

/* Accessibility, from GET /api/os/apps/status. "unknown" is a real answer: the
   server could not ask macOS, and the screen says so instead of guessing. */
export function trustView(status) {
  const s = status && typeof status === 'object' ? status : {};
  if (s.trusted === true) {
    return { tone: 'ok', word: 'granted', text: 'macOS lets the process running Dourmouse read and operate other apps. The model can still only touch an app you allow below, and each action asks you first unless auto approve is on in SETTINGS.', backend: s.backend || '' };
  }
  if (s.trusted === false) {
    return { tone: 'warn', word: 'not granted', text: s.trust_help || s.backend_note || 'Accessibility permission is not granted to the process running Dourmouse. Nothing can be read or done in any app until it is.', backend: s.backend || '' };
  }
  return { tone: 'warn', word: 'unknown', text: 'The server did not say whether Accessibility is granted.', backend: s.backend || '' };
}

export function killView(kill) {
  const k = kill && typeof kill === 'object' ? kill : {};
  if (!k.engaged) return { engaged: false, word: 'off', line: 'App driving is allowed for the apps below. The kill switch is not engaged.' };
  const bits = [];
  if (k.reason) bits.push('Reason: ' + k.reason);
  if (k.by) bits.push('Stopped by: ' + k.by);
  if (k.file_error) bits.push('The kill file could not be written (' + k.file_error + '), so only this server process is stopped.');
  return { engaged: true, word: 'engaged', line: 'The model cannot drive any app. ' + (bits.length ? bits.join('. ') + '.' : 'No reason was recorded.') };
}

/* Running apps, allowed first, then the rest by name, denied last. */
export function runningRows(running) {
  const rows = (Array.isArray(running) ? running : []).filter((a) => a && typeof a.name === 'string' && a.name).map((a) => ({
    name: a.name,
    bundle_id: a.bundle_id || '',
    allowed: Boolean(a.allowed),
    denied: Boolean(a.denied),
    reason: a.deny_reason || '',
  }));
  const rank = (r) => (r.denied ? 2 : r.allowed ? 0 : 1);
  return rows.sort((x, y) => rank(x) - rank(y) || x.name.localeCompare(y.name));
}

const KIND_TONE = { executed: 'ok', refused: 'bad', failed: 'bad', dry_run: 'warn' };

/* The recent driving log, newest first. Each row is a sentence, not a raw dump. */
export function logRows(recent) {
  const rows = (Array.isArray(recent) ? recent : []).filter((r) => r && typeof r === 'object').map((r) => {
    const parts = [String(r.action || 'action').replace(/_/g, ' ')];
    if (r.app) parts.push('in ' + r.app);
    if (r.actor) parts.push('by ' + r.actor);
    return { at: r.at, kind: String(r.kind || ''), tone: KIND_TONE[r.kind] || '', text: parts.join(' '), detail: typeof r.error === 'string' ? r.error : typeof r.reason === 'string' ? r.reason : '' };
  });
  return rows.reverse();
}

/* An owner-only route refused by the owner gate answers 403 "owner only". Any
   other refusal keeps the server's own words (a deny-listed app is also a 403). */
export function isOwnerOnly(err) {
  return Boolean(err) && err.status === 403 && String(err.message || '').toLowerCase() === 'owner only';
}

export function ownerOnlyText(err) {
  const detail = err && err.body && typeof err.body.detail === 'string' ? err.body.detail : '';
  return 'Owner only. ' + (detail ? detail.charAt(0).toUpperCase() + detail.slice(1) + '.' : 'This can only be done from the Dourmouse app window.');
}

export function errorText(err) {
  if (isOwnerOnly(err)) return ownerOnlyText(err);
  return err && err.message ? err.message : String(err);
}

export function allowPrompt(row) {
  return 'Allow the model to read and operate ' + row.name + '? It will be able to see the text and buttons in ' + row.name + ' and to click, type and scroll there. Each action asks you first, unless auto approve is on in SETTINGS. It still can never drive a terminal, System Settings, a password manager or Dourmouse itself. You can remove ' + row.name + ' here at any time.';
}

export function resumePrompt() {
  return 'Resume app driving? The kill switch is released, so the model can again read and operate the apps on the allow list. Each action asks you first, unless auto approve is on in SETTINGS.';
}

export function paneUrl(pane) {
  return PANE_URL + String(pane || '');
}
