/**
 * renderer.ts — the UI listener.
 *
 * Two streams arrive interleaved and must render concurrently: reasoning into
 * a collapsible scratchpad, answer text into the output box.
 *
 * The one non-obvious thing here is batching. A fast turn emits hundreds of
 * deltas per second, and touching the DOM on each one costs a layout and a
 * paint per token — the UI visibly stutters and falls behind the process it
 * is meant to be showing live. So deltas are appended to a string buffer and
 * flushed once per animation frame. The perceived latency is identical (one
 * frame, ~16ms) and the cost drops by orders of magnitude.
 *
 * Text is written with `textContent`, never `innerHTML`. Model output is
 * untrusted input like any other: it can contain markup, and rendering it as
 * markup in a privileged Electron window is how a prompt injection becomes
 * code execution.
 */

import type { ClaudeEvent } from './preload';

const $ = <T extends HTMLElement>(id: string): T => {
  const el = document.getElementById(id);
  if (!el) throw new Error(`missing element #${id}`);
  return el as T;
};

const els = {
  prompt: $<HTMLTextAreaElement>('prompt'),
  send: $<HTMLButtonElement>('send'),
  stop: $<HTMLButtonElement>('stop'),
  status: $<HTMLElement>('status'),
  thinkWrap: $<HTMLDetailsElement>('thinkWrap'),
  thinking: $<HTMLElement>('thinking'),
  output: $<HTMLElement>('output'),
  meta: $<HTMLElement>('meta'),
};

let sessionId: string | null = null;

/* ------------------------------- batching -------------------------------- */

let pendingThinking = '';
let pendingText = '';
let frame = 0;

/** Only auto-scroll while the user is already at the bottom. */
function nearBottom(el: HTMLElement): boolean {
  return el.scrollHeight - el.scrollTop - el.clientHeight < 40;
}

function flush(): void {
  frame = 0;

  if (pendingThinking) {
    const stick = nearBottom(els.thinking);
    els.thinking.textContent += pendingThinking;
    pendingThinking = '';
    if (stick) els.thinking.scrollTop = els.thinking.scrollHeight;
  }

  if (pendingText) {
    const stick = nearBottom(els.output);
    els.output.textContent += pendingText;
    pendingText = '';
    if (stick) els.output.scrollTop = els.output.scrollHeight;
  }
}

function schedule(): void {
  if (!frame) frame = requestAnimationFrame(flush);
}

/* -------------------------------- state ---------------------------------- */

function setRunning(running: boolean): void {
  els.send.disabled = running;
  els.stop.disabled = !running;
  els.prompt.disabled = running;
  els.status.textContent = running ? 'RUNNING' : 'IDLE';
  els.status.dataset.state = running ? 'running' : 'idle';
}

function resetPanes(): void {
  if (frame) cancelAnimationFrame(frame);
  frame = 0;
  pendingThinking = '';
  pendingText = '';
  els.thinking.textContent = '';
  els.output.textContent = '';
  els.meta.textContent = '';
  // Open while reasoning; it folds itself the moment real answer text starts.
  els.thinkWrap.open = true;
  els.thinkWrap.hidden = true;
}

/* -------------------------------- events --------------------------------- */

window.claude.onEvent((ev: ClaudeEvent) => {
  // Late events from a cancelled or superseded run must not paint into the
  // current one.
  if (sessionId && 'sessionId' in ev && ev.sessionId !== sessionId) return;

  switch (ev.type) {
    case 'start':
      els.meta.textContent = ev.model ? `model ${ev.model}` : '';
      break;

    case 'thinking':
      els.thinkWrap.hidden = false;
      pendingThinking += ev.text;
      schedule();
      break;

    case 'text':
      // First real answer token: fold the scratchpad. It stays reachable via
      // the toggle rather than disappearing, so the reasoning is never lost.
      if (els.thinkWrap.open && !els.output.textContent) els.thinkWrap.open = false;
      pendingText += ev.text;
      schedule();
      break;

    case 'tool':
      els.meta.textContent = `tool: ${ev.name}`;
      break;

    case 'usage': {
      const cost = ev.costUsd === null ? '—' : `$${ev.costUsd.toFixed(4)}`;
      const tokens = ev.inputTokens + ev.outputTokens;
      els.meta.textContent = `${cost} · ${tokens.toLocaleString()} tok`;
      break;
    }

    case 'done':
      flush();
      // The result event carries the authoritative final text. If deltas and
      // it disagree (a retry, a truncated stream), trust the result.
      if (ev.finalText && ev.finalText !== els.output.textContent) {
        els.output.textContent = ev.finalText;
      }
      sessionId = null;
      setRunning(false);
      break;

    case 'error':
      flush();
      els.output.textContent += `\n\n[${ev.message}]`;
      sessionId = null;
      setRunning(false);
      break;
  }
});

/* -------------------------------- actions -------------------------------- */

async function send(): Promise<void> {
  const prompt = els.prompt.value.trim();
  if (!prompt) return;
  resetPanes();
  setRunning(true);
  try {
    sessionId = await window.claude.start(prompt);
  } catch (err) {
    setRunning(false);
    els.output.textContent = `[${(err as Error).message}]`;
  }
}

els.send.addEventListener('click', () => void send());
els.stop.addEventListener('click', () => {
  if (sessionId) void window.claude.cancel(sessionId);
});
els.prompt.addEventListener('keydown', (e) => {
  // Enter sends, Shift+Enter is a newline.
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    void send();
  }
});

/* ------------------------------- bootstrap ------------------------------- */

void (async () => {
  setRunning(false);
  const status = await window.claude.status();
  if (!status.ready) {
    els.status.textContent = 'CLI NOT FOUND';
    els.status.dataset.state = 'error';
    els.send.disabled = true;
    els.output.textContent =
      'The Claude Code CLI was not found.\n\n' +
      'Install it with:  npm i -g @anthropic-ai/claude-code\n' +
      'Or set CLAUDE_CLI_PATH to its absolute path and restart.';
    return;
  }
  els.meta.textContent = `${status.cliPath} · login via ${status.credentialSource}`;
})();
