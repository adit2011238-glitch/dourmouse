/* Pure helpers for VOICE. No DOM at import time, so node can test them. */

/* The parser's panel ids (voice_commands.py) mapped to the OS screens that do
   the same job. A panel with no OS screen is refused with the reason, never
   opened as something else. */
export const PANEL_SCREENS = {
  mail: { slug: 'comms', label: 'COMMS' },
  chat: { slug: 'home', label: 'HOME' },
  research: { slug: 'research', label: 'RESEARCH' },
  map: { slug: 'atlas', label: 'ATLAS' },
};
const CONSOLE_ONLY = {
  globe: 'The globe stays in the classic console; the OS shell has no screen for it.',
  design3d: 'The 3D editor stays in the classic console; the OS shell has no screen for it.',
};

/* What to do with the parser's answer. `answer` is the body of
   POST /api/voice/command. Returns { kind, ... } and never performs anything. */
export function planFor(text, answer) {
  const raw = String(text || '').trim();
  if (!answer || !answer.recognized || !answer.command) {
    return { kind: 'chat', text: raw, say: 'Not a command. Sent to the companion on HOME as an ordinary message.' };
  }
  const c = answer.command;
  const a = c.args || {};
  if (c.action === 'open_panel') {
    const t = PANEL_SCREENS[a.panel];
    if (t) return { kind: 'navigate', slug: t.slug, say: 'Opening ' + t.label + '.' };
    return { kind: 'refuse', say: CONSOLE_ONLY[a.panel] || 'There is no OS screen for "' + a.panel + '".' };
  }
  if (c.action === 'close_panel') {
    return { kind: 'refuse', say: 'The OS shell shows one screen at a time; there is no panel to close. Pick another screen from the dock.' };
  }
  if (c.action === 'search') {
    return { kind: 'chat', text: 'search for ' + a.query, say: 'Sending "search for ' + a.query + '" to the companion on HOME.' };
  }
  if (c.action === 'email') {
    return { kind: 'chat', text: 'email ' + a.person + ': ' + a.message, say: 'Asking the companion on HOME to email ' + a.person + '. Nothing is sent until you approve it there.' };
  }
  return { kind: 'refuse', say: 'The parser returned an action this screen does not know: ' + String(c.action) };
}

/* Which capture engine is really available. `voice` is GET /api/voice, `env` is
   { speechRecognition: bool, mic: bool }. Server speech to text needs both the
   gate on and faster-whisper installed, plus a microphone in this window. */
export function chooseEngine(voice, env) {
  const serverOk = Boolean(voice) && voice.stt && voice.stt !== 'not-configured';
  if (serverOk && env.mic) {
    return { id: 'server', label: 'server Whisper (' + (voice.whisper_model || 'model') + ')', ok: true, note: 'Audio is recorded here, sent to this Mac only, and transcribed by local Whisper.' };
  }
  if (env.speechRecognition) {
    return { id: 'browser', label: 'this window\'s speech recognition', ok: true, note: 'Recognition is done by the window, which may use its vendor\'s service. Server Whisper is ' + (serverOk ? 'available but this window has no microphone access.' : 'not configured.') };
  }
  const why = serverOk
    ? 'This window has no microphone API.'
    : 'Server Whisper is not configured (set DOURMOUSE_VOICE=1 and install faster-whisper) and this window has no speech recognition.';
  return { id: 'none', label: 'none', ok: false, note: why + ' Typed commands still work.' };
}

export function wakewordTag(w) {
  if (!w) return { tone: '', text: 'unknown' };
  if (!w.enabled) return { tone: '', text: 'off' };
  if (w.inference_engine === 'not-configured' || w.capture_engine === 'not-configured') return { tone: 'warn', text: 'on, missing parts' };
  return { tone: 'ok', text: 'enabled' };
}

/* base64 payload of a data: URL. '' when it is not one. */
export function stripDataUrl(url) {
  const s = String(url || '');
  const i = s.indexOf(',');
  return s.startsWith('data:') && i > 0 ? s.slice(i + 1) : '';
}

export const MAX_RECORD_S = 30;
export const LOG_CAP = 30;
