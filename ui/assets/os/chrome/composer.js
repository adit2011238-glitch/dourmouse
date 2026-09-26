/* The directive box at the bottom of the stage. Enter sends, Shift+Enter is a
   newline. It routes to the current screen's own thread on the six thread
   screens and to HOME's otherwise (the screen supplies onSend). While a turn
   runs, SEND becomes QUEUE and STOP appears. */

import { html, setHtml } from '../kit/html.js';

export function createComposer({ root }) {
  let opts = null;
  setHtml(root, html`
    <label class="sr-only" for="cin">Directive</label>
    <textarea class="os-field" id="cin" rows="1" placeholder="Ask anything, or /all &lt;goal&gt; to put every resource on it" data-spec="Directive box. Enter sends, Shift+Enter is a newline. Routes to THIS screen's own thread on the six thread screens, otherwise falls back to HOME's."></textarea>
    <span class="cq" id="cqCount" hidden></span>
    <button type="button" class="os-btn os-btn--danger" id="cstop" hidden data-spec="Stops the run: aborts the stream and declines any open approval so the server thread wakes.">STOP</button>
    <button type="button" class="os-btn os-btn--primary" id="csend" data-spec="Sends the directive. While a run is in flight it queues instead.">SEND</button>`);
  const ta = root.querySelector('#cin');
  const send = root.querySelector('#csend');
  const stop = root.querySelector('#cstop');
  const count = root.querySelector('#cqCount');

  function fit() {
    ta.style.height = 'auto';
    ta.style.height = Math.min(ta.scrollHeight, 130) + 'px';
  }
  function submit() {
    const text = ta.value.trim();
    if (!text || !opts || !opts.onSend) return;
    ta.value = '';
    fit();
    opts.onSend(text);
  }
  ta.addEventListener('input', fit);
  ta.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      submit();
    }
  });
  send.addEventListener('click', submit);
  stop.addEventListener('click', () => opts && opts.onStop && opts.onStop());

  return {
    set(next) {
      opts = next;
      if (!next) {
        root.hidden = true;
        return;
      }
      root.hidden = false;
      ta.placeholder = next.placeholder || 'Ask anything, or /all <goal> to put every resource on it';
      ta.disabled = Boolean(next.disabled);
      send.textContent = next.busy ? 'QUEUE' : 'SEND';
      stop.hidden = !next.busy;
      const q = next.queued || 0;
      count.hidden = !q;
      count.textContent = q ? q + ' queued' : '';
    },
    focus: () => ta.focus(),
    reset() {
      opts = null;
      root.hidden = true;
    },
  };
}
