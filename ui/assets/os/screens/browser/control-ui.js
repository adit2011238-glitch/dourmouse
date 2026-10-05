/* BROWSER (Phase C2): who has the browser, the owner or the model, and the owner's two buttons.

   The pane is one browser shared by the owner and the AI. The Electron shell decides who may act
   (see ~/Documents/DOURMOUSE/C2_SHARED_CONTROL_DESIGN.md): the owner's own key, click or scroll in a
   tab always wins, and this bar shows the result and offers:
   - Stop: cancels what the model is doing now. It is told it was stopped and not to retry.
   - Take control: Stop, and the model may not start anything in the browser until Let the model act.
   It talks to the shell only through window.dourmouseShell.pane.control, whose handlers answer the
   console window alone. The bar is drawn above the page area (the native view covers the page). */

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

const TOOL_WORDS = {
  open: 'opening a page', back: 'going back', click: 'clicking', fill: 'filling a field', fill_form: 'filling a form',
  select: 'choosing an option', type: 'typing', press: 'pressing a key', submit: 'submitting a form', signin: 'signing in',
  media_play: 'playing the video', media_pause: 'pausing the video', media_seek: 'moving the video',
  media_mute: 'muting the video', media_unmute: 'unmuting the video', media_volume: 'changing the volume',
};
const OUTCOME_WORDS = {
  'owner-input': 'You used the tab, so the model stopped and was told what it had done.',
  stopped: 'Stopped. The model was told what it had done and not to retry unless you ask.',
  'owner-control': 'You took control. The model was told to wait for you.',
  'no-tab': 'The tab the model was using was closed, so it stopped.',
  expired: 'The model stopped answering, so its claim on the tab ended.',
};

export function toolWords(tool) {
  return TOOL_WORDS[String(tool || '')] || 'acting';
}

/* The shell's state as one sentence and the buttons to show. Pure, so it is tested without a DOM. */
export function controlModel(s) {
  const st = s && typeof s === 'object' ? s : {};
  const acting = Array.isArray(st.acting) ? st.acting.filter((a) => a && typeof a === 'object') : [];
  const state = ['idle', 'owner-active', 'model-acting', 'model-waiting', 'owner-control'].includes(st.state) ? st.state : 'idle';
  const last = st.last && OUTCOME_WORDS[st.last.outcome] ? OUTCOME_WORDS[st.last.outcome] : '';
  if (state === 'owner-control') {
    return { state, kind: 'owner', text: 'You have control. The model is paused until you let it act.', buttons: ['release'], last };
  }
  if (state === 'model-acting' && acting.length) {
    const a = acting[0];
    const where = Number.isInteger(a.tabId) ? (a.tabId === st.activeTab ? 'in this tab' : 'in tab ' + a.tabId) : 'in the browser';
    const more = acting.length > 1 ? ' (and ' + (acting.length - 1) + ' more)' : '';
    return { state, kind: 'model', text: 'Model is acting ' + where + ': ' + toolWords(a.tool) + more + '.', buttons: ['stop', 'take'], last };
  }
  if (state === 'model-waiting' && st.waiting) {
    return { state, kind: 'wait', text: 'The model wants to use the browser and is waiting for you to pause.', buttons: ['take'], last };
  }
  return { state, kind: 'owner', text: 'You have control.', buttons: ['take'], last };
}

const BUTTONS = {
  stop: { label: 'Stop', go: true, spec: 'Stops what the model is doing in the browser right now. It stops before its next step, keeps what it already did, and is told you stopped it and not to retry unless you ask.' },
  take: { label: 'Take control', go: false, spec: 'Stops the model and keeps it out of the browser until you press Let the model act. Your own typing, clicks and scrolling in a tab also make the model wait, without this button.' },
  release: { label: 'Let the model act', go: true, spec: 'Lets the model use the browser again. It still waits whenever you are typing, clicking or scrolling in the same tab.' },
};

/* control: window.dourmouseShell.pane.control. note(text, tone): the screen's message line. */
export function createControl({ control, note }) {
  const root = el('div', 'cb-ask cb-control');
  root.setAttribute('role', 'status');
  root.setAttribute('aria-live', 'polite');
  root.setAttribute('aria-label', 'Who has the browser');
  const dot = el('span', 'ic');
  dot.setAttribute('aria-hidden', 'true');
  const tx = el('span', 'tx');
  const ac = el('span', 'ac');
  root.append(dot, tx, ac);
  let sig = '';
  let lastSeen = null;
  let disposed = false;

  function act(name) {
    const fn = name === 'stop' ? control.stop : name === 'take' ? control.take : control.release;
    Promise.resolve(fn())
      .then((r) => {
        if (disposed) return;
        if (r && r.ok === false) note('The browser did not accept that: ' + (r.error || 'refused'), 'error');
        else if (name === 'release') note('');
      })
      .catch((err) => note('The browser did not answer: ' + (err && err.message ? err.message : String(err)), 'error'));
  }

  function update(s) {
    if (disposed) return;
    const m = controlModel(s);
    const key = m.kind + '|' + m.text + '|' + m.buttons.join(',');
    if (key !== sig) {
      sig = key;
      root.dataset.kind = m.kind;
      root.dataset.state = m.state;
      tx.textContent = m.text;
      ac.replaceChildren(...m.buttons.map((b) => {
        const spec = BUTTONS[b];
        const btn = el('button', 'cb-ask-btn' + (spec.go ? ' cb-go' : ''), spec.label);
        btn.type = 'button';
        btn.dataset.control = b;
        btn.dataset.spec = spec.spec;
        btn.addEventListener('click', () => act(b));
        return btn;
      }));
    }
    const last = s && s.last && typeof s.last === 'object' ? s.last : null;
    if (last && m.last && (!lastSeen || last.at !== lastSeen.at) && last.outcome !== 'done') {
      if (lastSeen !== null) note(m.last, 'warn'); /* the first state after mounting is history, not news */
    }
    if (last) lastSeen = last;
    else if (lastSeen === null) lastSeen = { at: 0 };
  }

  return {
    root,
    update,
    dispose() {
      disposed = true;
    },
  };
}
