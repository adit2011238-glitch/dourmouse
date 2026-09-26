/* VOICE: speech and typed commands in, through the server's own command parser.

   Every utterance, typed or spoken, goes to POST /api/voice/command. That parser
   is deterministic and performs nothing; this screen carries out its answer:
   open a screen, or hand a message to the companion on HOME (where every
   mail send still stops at the approval gate). Text the parser does not
   recognise is sent to the companion as an ordinary message, never dropped.

   Capture is honest about its engine: server Whisper when it is configured and
   this window can record, else the window's own speech recognition, else typed
   only. The wake-word card is one read of what the server reports. */

import { html, setHtml } from '../../kit/html.js';
import { states } from '../../kit/states.js';
import { ring } from '../../core/ring.js';
import { clock } from '../../kit/format.js';
import { isAbort } from '../../core/api.js';
import { planFor, chooseEngine, wakewordTag, stripDataUrl, MAX_RECORD_S, LOG_CAP } from './helpers.js';

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

export default {
  id: 'VOICE',
  sub: 'voice commands',
  css: true,
  thread: false,

  async mount(root, ctx) {
    const st = { voice: null, info: null, engine: null, recording: false, secs: 0, stopTimer: null, recorder: null, stream: null, sr: null, busy: false };
    const log = ring(LOG_CAP);

    /* ---------------- skeleton ---------------- */
    setHtml(root, html`
      <div class="card"><div class="lbl">Capture</div><div id="voiCap" data-region></div>
        <div class="voi-note" id="voiNote" role="status" hidden></div></div>
      <div class="card" style="margin-top:12px"><div class="lbl">Voice commands</div>
        <div id="voiCmds" data-region></div>
        <div class="voi-row"><input class="os-field" id="voiText" type="text" autocomplete="off" spellcheck="false" placeholder="type a command" aria-label="Type a command" data-spec="Same parser as speech, typed. Useful for testing a phrase without speaking. Press Enter or RUN."><button class="os-btn" id="voiRun" type="button" data-spec="Sends the text to the server's command parser and carries out its answer. Text that is not a command goes to the companion on HOME as an ordinary message, which uses the model.">RUN</button></div>
        <div id="voiLog" class="voi-log" data-region></div></div>
      <div class="card" style="margin-top:12px"><div class="lbl">Wakeword</div><div id="voiWake" data-region></div></div>`);
    const $ = (id) => root.querySelector('#' + id);
    const capEl = $('voiCap');
    const noteEl = $('voiNote');
    const cmdsEl = $('voiCmds');
    const textEl = $('voiText');
    const runBtn = $('voiRun');
    const logEl = $('voiLog');
    const wakeEl = $('voiWake');
    root.dataset.state = 'loading';

    const note = (text, tone) => {
      noteEl.hidden = !text;
      noteEl.textContent = text || '';
      noteEl.dataset.tone = tone || '';
    };

    /* ---------------- stage bar ---------------- */
    function paintActions() {
      const eng = st.engine;
      ctx.chrome.setActions([{
        id: 'listen',
        label: st.recording ? 'STOP (' + st.secs + 's)' : st.busy ? 'WORKING' : 'LISTEN',
        kind: 'primary',
        disabled: !eng || !eng.ok || st.busy,
        pressed: st.recording,
        spec: 'Starts capture from the microphone and stops it on the next press, or after ' + MAX_RECORD_S + ' seconds. The wakeword is separate and reported below.',
        onClick: () => (st.recording ? stopListening() : startListening()),
      }]);
    }

    /* ---------------- reads ---------------- */
    async function load() {
      const [voice, info] = await Promise.allSettled([ctx.api.get('/api/voice'), ctx.api.get('/api/os/voice/info')]);
      if (ctx.signal.aborted) return;
      if (voice.status === 'fulfilled') st.voice = voice.value;
      if (info.status === 'fulfilled') st.info = info.value;
      const failure = [voice, info].find((r) => r.status === 'rejected' && !isAbort(r.reason));
      st.engine = chooseEngine(st.voice, {
        speechRecognition: Boolean(window.SpeechRecognition || window.webkitSpeechRecognition),
        mic: Boolean(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder),
      });
      paintCapture(failure && failure.reason);
      paintCommands(failure && failure.reason);
      paintWake(failure && failure.reason);
      paintLog();
      paintActions();
      root.dataset.state = st.info ? 'populated' : failure ? 'error' : 'loading';
    }

    function paintCapture(err) {
      if (!st.voice && err) {
        states.error(capEl, err, { title: 'Could not read the voice status', retry: () => load() });
        return;
      }
      const v = st.voice || {};
      const e = st.engine;
      capEl.dataset.state = e.ok ? 'populated' : 'unavailable';
      setHtml(capEl, html`
        <div class="kv"><span>Engine in use</span><span class="tag ${e.ok ? 'ok' : 'warn'}">${e.label}</span></div>
        <div class="kv"><span>Server speech to text</span><span class="tag ${v.stt && v.stt !== 'not-configured' ? 'ok' : ''}">${v.stt || 'unknown'}</span></div>
        <div class="kv"><span>Server speech out</span><span class="tag ${v.tts && v.tts !== 'not-configured' ? 'ok' : ''}">${v.tts || 'unknown'}</span></div>
        <div class="muted" style="margin-top:6px">${e.note}</div>`);
    }

    function paintCommands(err) {
      if (!st.info && err) {
        states.error(cmdsEl, err, { title: 'Could not read the command list', retry: () => load() });
        return;
      }
      const cmds = (st.info && st.info.commands) || [];
      if (!cmds.length) {
        states.empty(cmdsEl, 'The parser reported no commands.');
        return;
      }
      const ul = el('ul', 'voi-cmds');
      cmds.forEach((c) => {
        const li = el('li');
        li.append(el('code', '', c.pattern), document.createTextNode('  e.g. ' + c.example));
        ul.append(li);
      });
      const foot = el('div', 'muted', 'Anything the parser does not recognise is sent to the companion as a normal chat message rather than dropped.');
      states.populated(cmdsEl, [ul, foot]);
    }

    function paintWake(err) {
      if (!st.info && err) {
        states.error(wakeEl, err, { title: 'Could not read the wakeword state', retry: () => load() });
        return;
      }
      if (!st.info) return;
      const w = st.info.wakeword;
      const h = st.info.hands_free || {};
      const tag = wakewordTag(w);
      wakeEl.dataset.state = 'populated';
      setHtml(wakeEl, html`
        <div class="kv"><span>DOURMOUSE_WAKEWORD</span><span class="tag ${tag.tone}">${tag.text}</span></div>
        <div class="kv"><span>Listener running now</span><span class="tag ${h.running ? 'ok' : ''}">${h.running ? 'yes' : 'no'}</span></div>
        <div class="kv"><span>Inference engine</span><span class="muted">${w.inference_engine}</span></div>
        <div class="kv"><span>Audio capture</span><span class="muted">${w.capture_engine}</span></div>
        <div class="kv"><span>Model and threshold</span><span class="muted">${w.model} at ${w.threshold}</span></div>
        <div class="muted" style="margin-top:6px">${w.enabled ? (h.running ? 'The hands-free loop is running.' : 'Enabled in the environment, but the server did not start the hands-free loop' + (h.reason ? ': ' + h.reason : '.')) : 'Off by default and honestly reported. Enabling it needs DOURMOUSE_WAKEWORD=1 and the real model files present.'}</div>`);
    }

    function paintLog() {
      const rows = log.newestFirst();
      if (!rows.length) {
        states.empty(logEl, 'No utterance yet.', { hint: 'Type a command above or press LISTEN.' });
        return;
      }
      const wrap = el('div');
      rows.forEach((r) => {
        const row = el('div', 'os-row');
        row.append(el('span', 'tag ' + (r.tone || ''), r.source));
        const t = el('span', 'rt');
        t.append(el('b', '', '"' + r.heard + '"'), document.createTextNode('  ' + r.say));
        row.append(t, el('span', 'muted', clock(r.at)));
        wrap.append(row);
      });
      states.populated(logEl, wrap);
    }

    /* ---------------- one utterance ---------------- */
    async function handleUtterance(text, source) {
      const heard = String(text || '').trim();
      if (!heard) {
        note('Nothing to run. Type a command first.');
        return;
      }
      note('');
      let answer;
      try {
        answer = await ctx.api.post('/api/voice/command', { text: heard });
      } catch (err) {
        if (isAbort(err)) return;
        log.push({ at: Date.now(), source, heard, say: 'The parser could not be reached: ' + err.message, tone: 'bad' });
        paintLog();
        return;
      }
      if (ctx.signal.aborted) return;
      const plan = planFor(heard, answer);
      log.push({ at: Date.now(), source, heard, say: plan.say, tone: plan.kind === 'refuse' ? 'warn' : 'ok' });
      paintLog();
      if (plan.kind === 'chat') {
        ctx.chat.send(plan.text).catch((err) => ctx.notify({ level: 'error', title: 'Could not send to the companion', detail: err && err.message }));
        await Promise.resolve();
        location.hash = '#/home';
      } else if (plan.kind === 'navigate') {
        location.hash = '#/' + plan.slug;
      }
    }

    runBtn.addEventListener('click', () => {
      const v = textEl.value;
      textEl.value = '';
      handleUtterance(v, 'typed');
    });
    textEl.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        runBtn.click();
      }
    });

    /* ---------------- capture ---------------- */
    function endRecording() {
      if (st.stopTimer) {
        st.stopTimer();
        st.stopTimer = null;
      }
      st.recording = false;
      st.secs = 0;
      if (st.stream) {
        st.stream.getTracks().forEach((t) => t.stop());
        st.stream = null;
      }
      paintActions();
    }

    function startTimer() {
      st.secs = 0;
      st.stopTimer = ctx.every(1000, () => {
        st.secs += 1;
        paintActions();
        if (st.secs >= MAX_RECORD_S) stopListening();
      });
    }

    async function startListening() {
      const e = st.engine;
      if (!e || !e.ok || st.recording) return;
      note('');
      if (e.id === 'browser') {
        const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
        const rec = new SR();
        rec.lang = 'en-US';
        rec.interimResults = false;
        rec.continuous = false;
        st.sr = rec;
        rec.onresult = (ev) => {
          let s = '';
          for (const r of ev.results) s += r[0].transcript;
          if (s.trim()) handleUtterance(s, 'spoken');
        };
        rec.onerror = (ev) => note('Speech recognition failed: ' + (ev && ev.error ? ev.error : 'unknown error'), 'error');
        rec.onend = () => endRecording();
        try {
          rec.start();
          st.recording = true;
          startTimer();
          paintActions();
        } catch (err) {
          note('Could not start speech recognition: ' + (err && err.message ? err.message : String(err)), 'error');
        }
        return;
      }
      try {
        st.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      } catch (err) {
        note('Microphone not available: ' + (err && err.message ? err.message : String(err)), 'error');
        return;
      }
      if (ctx.signal.aborted) {
        endRecording();
        return;
      }
      const chunks = [];
      const rec = new MediaRecorder(st.stream);
      st.recorder = rec;
      rec.ondataavailable = (ev) => {
        if (ev.data && ev.data.size) chunks.push(ev.data);
      };
      rec.onstop = () => {
        const blob = new Blob(chunks, { type: rec.mimeType || 'audio/webm' });
        endRecording();
        transcribe(blob);
      };
      rec.start();
      st.recording = true;
      startTimer();
      paintActions();
    }

    function stopListening() {
      if (st.sr) {
        try {
          st.sr.stop();
        } catch (_err) {
          /* already stopped */
        }
        st.sr = null;
        return;
      }
      if (st.recorder && st.recorder.state !== 'inactive') st.recorder.stop();
    }

    function transcribe(blob) {
      if (!blob.size) {
        note('Nothing was recorded.', 'error');
        return;
      }
      st.busy = true;
      paintActions();
      note('Transcribing on this Mac.');
      const reader = new FileReader();
      reader.onload = async () => {
        try {
          const d = await ctx.api.post('/api/os/voice/transcribe', { audio_b64: stripDataUrl(reader.result) });
          if (ctx.signal.aborted) return;
          note('');
          if (!d.text) note('No speech was detected in the recording.');
          else await handleUtterance(d.text, 'spoken');
        } catch (err) {
          if (!isAbort(err)) note(err.message, 'error');
        } finally {
          st.busy = false;
          if (!ctx.signal.aborted) paintActions();
        }
      };
      reader.onerror = () => {
        st.busy = false;
        note('Could not read the recording.', 'error');
        paintActions();
      };
      reader.readAsDataURL(blob);
    }

    /* release the microphone when leaving */
    ctx.signal.addEventListener('abort', () => {
      try {
        if (st.sr) st.sr.abort();
        if (st.recorder && st.recorder.state !== 'inactive') {
          st.recorder.onstop = null;
          st.recorder.stop();
        }
      } catch (_err) {
        /* already stopped */
      }
      if (st.stream) st.stream.getTracks().forEach((t) => t.stop());
    });

    ctx.events.onResync(() => load());
    ctx.chrome.setLive(false);
    paintActions();
    states.loading(capEl, 'Reading the voice status');
    states.loading(cmdsEl, 'Reading the command list');
    states.loading(wakeEl, 'Reading the wakeword state');
    paintLog();
    await load();
  },

  async refresh(ctx, reason) {
    if (reason === 'manual') ctx.notify({ level: 'info', title: 'VOICE', detail: 'The status is read when you open this screen.', ttl: 2500 });
  },
};
