/* MEDIA: one player and reader surface: audio, video, images and PDFs, a queue,
   a now-playing bar, media keys, a YouTube / Spotify launcher, and this
   screen's own thread.

   The bytes come from the routes that already exist (/api/files/media with real
   byte ranges, /api/files/media-status while ffmpeg converts, /api/files/image,
   /api/files/subtitle.vtt). The screen never builds one of those URLs from text
   the owner typed: it asks POST /api/os/media/open first, and that route only
   vouches for a real file of a media type inside a short list of folders.

   Every figure is read: the duration and size line from the probe and the media
   element, the format cards from the server's own lists. Opening a file reads
   it and adds it to a recent list; nothing else on disk changes. */

import { html, setHtml } from '../../kit/html.js';
import { states } from '../../kit/states.js';
import { mountThreadView } from '../../kit/thread-view.js';
import { agoLabel } from '../../kit/format.js';
import { isAbort } from '../../core/api.js';
import {
  clock, sizeLabel, infoLine, mediaErrorText, formatText, convertLabel, CONVERT_POLL_MS, CONVERT_POLL_MAX,
  neighbour, reorderTarget, queueLabel, seekTarget, controlPlan, parseWebLink,
} from './helpers.js';
import { createPdfReader } from './pdf-view.js';

const REPORT_MS = 4000;
const SEEK_STEP = 5;
const VOL_STEP = 0.1;

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function spec(node, text) {
  node.dataset.spec = text;
  return node;
}

export default {
  id: 'MEDIA',
  sub: 'player and library',
  css: true,
  thread: true,

  async mount(root, ctx) {
    const st = { lib: null, openPanel: false, row: null, media: null, seq: 0, stopPoll: null, dragging: false, facts: {}, queue: [], aliases: {}, pendingPlay: false, pendingSeek: null, lastReport: '', volume: 1, muted: false, rate: 1 };

    /* ---------------- skeleton ---------------- */
    const noteEl = el('div', 'med-note');
    noteEl.hidden = true;
    noteEl.setAttribute('role', 'status');

    const openEl = el('div', 'card med-open');
    openEl.hidden = true;
    openEl.append(el('div', 'lbl', 'Open a file'));
    const pathRow = el('div', 'med-pathrow');
    const pathIn = el('input', 'os-field');
    pathIn.type = 'text';
    pathIn.placeholder = 'full path, for example ~/Movies/clip.mp4';
    pathIn.setAttribute('aria-label', 'Path of the file to open');
    pathIn.autocomplete = 'off';
    pathIn.spellcheck = false;
    spec(pathIn, 'The full path of an audio, video or image file. MEDIA only plays files inside the workspace, uploads, Movies, Music, Downloads, Desktop and Documents folders.');
    const goBtn = el('button', 'os-btn os-btn--primary', 'OPEN');
    goBtn.type = 'button';
    spec(goBtn, 'Checks the path on the server, records it in the recent list and loads it into the player. It reads the file; it changes nothing.');
    pathRow.append(pathIn, goBtn);
    const srcEl = el('div', 'med-src');
    srcEl.setAttribute('role', 'group');
    srcEl.setAttribute('aria-label', 'Where to look for a file');
    const listEl = el('div', 'med-list');
    listEl.dataset.region = '';
    openEl.append(pathRow, srcEl, listEl);

    const playerEl = el('div', 'card med-player');
    const stageEl = el('div', 'med-stage');
    stageEl.dataset.region = '';
    const barEl = el('div', 'med-bar');
    const infoEl = el('div', 'muted med-info');
    const tx = el('div', 'med-tx');
    const prevBtn = el('button', 'os-btn med-skip', '⏮');
    prevBtn.type = 'button';
    prevBtn.disabled = true;
    prevBtn.setAttribute('aria-label', 'Previous');
    spec(prevBtn, 'Previous queue item. Past the first three seconds of a track it restarts the track instead. Shortcut: Shift+Left.');
    const nextBtn = el('button', 'os-btn med-skip', '⏭');
    nextBtn.type = 'button';
    nextBtn.disabled = true;
    nextBtn.setAttribute('aria-label', 'Next');
    spec(nextBtn, 'Next queue item. Shortcut: Shift+Right.');
    const playBtn = el('button', 'os-btn med-play', '▶');
    playBtn.type = 'button';
    playBtn.disabled = true;
    playBtn.setAttribute('aria-label', 'Play');
    spec(playBtn, 'Play and pause. The clock, the scrubber and this glyph are the only things that change during playback.');
    const curEl = el('span', 'muted', '');
    const seek = el('input');
    seek.type = 'range';
    seek.min = '0';
    seek.max = '0';
    seek.step = '0.1';
    seek.value = '0';
    seek.disabled = true;
    seek.setAttribute('aria-label', 'Position');
    spec(seek, 'Scrubber. Releasing it seeks the media element, and the browser asks the server for a byte range from that point; the server answers 206 with exactly those bytes.');
    const durEl = el('span', 'muted', '');
    const muteBtn = el('button', 'os-btn med-skip', 'MUTE');
    muteBtn.type = 'button';
    muteBtn.setAttribute('aria-label', 'Mute');
    spec(muteBtn, 'Mutes and unmutes the player. Shortcut: M.');
    const vol = el('input', 'med-vol');
    vol.type = 'range';
    vol.min = '0';
    vol.max = '1';
    vol.step = '0.05';
    vol.value = '1';
    vol.setAttribute('aria-label', 'Volume');
    spec(vol, 'Volume. Up and Down arrows change it by ten percent.');
    /* F8: playback speed and full screen */
    const RATES = [1, 1.25, 1.5, 2, 0.75];
    const speedBtn = el('button', 'os-btn med-skip', '1x');
    speedBtn.type = 'button';
    speedBtn.setAttribute('aria-label', 'Playback speed 1x');
    spec(speedBtn, 'Cycles the playback speed: 1x, 1.25x, 1.5x, 2x, 0.75x. It stays until you change it.');
    const fsBtn = el('button', 'os-btn med-skip', 'FULL SCREEN');
    fsBtn.type = 'button';
    fsBtn.hidden = true;
    spec(fsBtn, 'Shows the video full screen. Esc leaves it. Shortcut: F.');
    const queueAddBtn = el('button', 'os-btn med-skip', '+ QUEUE');
    queueAddBtn.type = 'button';
    queueAddBtn.disabled = true;
    spec(queueAddBtn, 'Adds the open audio or video file to the end of the queue. The queue is saved, so it survives a restart.');
    tx.append(prevBtn, playBtn, nextBtn, curEl, seek, durEl, muteBtn, vol, speedBtn, fsBtn, queueAddBtn);
    const queueNote = el('div', 'muted med-qpos');
    const pdfBarEl = el('div', 'med-pdfbar');
    pdfBarEl.hidden = true;
    barEl.append(infoEl, queueNote, tx);
    playerEl.append(stageEl, pdfBarEl, barEl);

    const queueEl = el('div', 'card med-queue');
    const queueHead = el('div', 'med-qhead');
    queueHead.append(el('div', 'lbl', 'Queue'));
    const queueClear = el('button', 'os-btn', 'CLEAR');
    queueClear.type = 'button';
    spec(queueClear, 'Empties the queue. The files themselves are not touched.');
    queueHead.append(queueClear);
    const queueList = el('div', 'med-qlist');
    queueList.dataset.region = '';
    queueEl.append(queueHead, queueList);

    const webEl = el('div', 'card med-web');
    webEl.append(el('div', 'lbl', 'YouTube and Spotify'));
    const webRow = el('div', 'med-pathrow');
    const webIn = el('input', 'os-field');
    webIn.type = 'text';
    webIn.placeholder = 'paste a link, or type a search';
    webIn.setAttribute('aria-label', 'A YouTube or Spotify link, or a search');
    webIn.autocomplete = 'off';
    webIn.spellcheck = false;
    spec(webIn, 'A YouTube or Spotify link, or words to search for. Only https links on those two services are opened; the page opens in the shared browser pane (or the system browser outside the desktop app), because this window cannot frame another site.');
    const ytBtn = el('button', 'os-btn os-btn--primary', 'YOUTUBE');
    ytBtn.type = 'button';
    spec(ytBtn, 'Opens the link or search on YouTube in the browser pane. Empty opens the YouTube home page.');
    const spBtn = el('button', 'os-btn os-btn--primary', 'SPOTIFY');
    spBtn.type = 'button';
    spec(spBtn, 'Opens the link or search on Spotify Web in the browser pane. Spotify asks you to sign in there; this app never sees that login.');
    webRow.append(webIn, ytBtn, spBtn);
    webEl.append(webRow);

    const fmtEl = el('div', 'grid3');
    fmtEl.dataset.region = '';
    const threadEl = el('div', 'med-thread');

    root.replaceChildren(noteEl, openEl, playerEl, queueEl, webEl, fmtEl, threadEl);
    root.tabIndex = -1;
    root.dataset.state = 'empty';

    const note = (text) => {
      noteEl.hidden = !text;
      noteEl.textContent = text || '';
    };

    /* ---------------- stage bar ---------------- */
    const actions = () => [{
      id: 'open', label: 'OPEN FILE', kind: 'primary', pressed: st.openPanel,
      spec: 'Shows the path field and the folders MEDIA may read. It plays audio, video and images; anything ffmpeg cannot read is refused with the reason.',
      onClick: () => togglePanel(),
    }];
    const view = mountThreadView(threadEl, ctx, {
      scroller: root.parentElement,
      placeholder: 'Ask about a file, or ask for something to be played',
      emptyMessage: 'No conversation yet.',
      emptyHint: 'Ask a question about media. Enter sends, Shift+Enter starts a new line.',
      actions,
    });

    function togglePanel(force) {
      st.openPanel = typeof force === 'boolean' ? force : !st.openPanel;
      openEl.hidden = !st.openPanel;
      if (st.offEsc) {
        st.offEsc();
        st.offEsc = null;
      }
      if (st.openPanel) st.offEsc = ctx.keys.pushEsc(() => togglePanel(false));
      view.paintChrome();
      if (st.openPanel) {
        pathIn.focus();
        if (!st.lib) loadLibrary();
      }
    }

    const pdf = createPdfReader(ctx, { stage: stageEl, bar: pdfBarEl, note, scroller: root.parentElement });

    /* ---------------- reporting ---------------- */
    /* What the screen reports to the server (POST /api/os/media/player-state)
       so a tool can ask what is playing. Sent on every state change, and every
       few seconds while playing so the position stays fresh; skipped when
       nothing changed. */
    function snapshot() {
      const m = st.media;
      const row = st.row;
      const base = { path: row ? row.path : '', kind: row ? row.kind : null };
      if (row && row.kind === 'pdf') return { ...base, playing: false, page: pdf.state().page };
      return { ...base, playing: Boolean(m && !m.paused && !m.ended), position: m ? m.currentTime : null, duration: m && Number.isFinite(m.duration) ? m.duration : null };
    }
    async function report(force) {
      const snap = snapshot();
      const key = JSON.stringify({ ...snap, position: snap.playing ? null : Math.round((snap.position || 0) * 2) });
      if (!force && !snap.playing && key === st.lastReport) return;
      st.lastReport = key;
      try {
        await ctx.api.post('/api/os/media/player-state', snap);
      } catch (_err) {
        /* reporting is best effort; playback never depends on it */
      }
    }

    /* ---------------- queue ---------------- */
    function paintQueue() {
      const rows = st.queue;
      queueClear.disabled = !rows.length;
      if (!rows.length) {
        states.empty(queueList, 'The queue is empty.', { hint: 'Open an audio or video file and press + QUEUE.' });
      } else {
        const wrap = el('div');
        rows.forEach((r, i) => {
          const row = el('div', 'os-row med-qrow');
          if (st.row && st.row.path === r.path) row.classList.add('now');
          const play = el('button', 'os-btn med-file', (i + 1) + '. ' + r.name);
          play.type = 'button';
          spec(play, 'Plays ' + r.name + ' now.');
          play.addEventListener('click', () => openPath(r.path, { autoplay: true }));
          const up = el('button', 'os-btn med-skip', '▲');
          up.type = 'button';
          up.setAttribute('aria-label', 'Move ' + r.name + ' up');
          up.disabled = reorderTarget(i, -1, rows.length) < 0;
          spec(up, 'Moves ' + r.name + ' one place earlier in the queue.');
          up.addEventListener('click', () => queueOp({ op: 'move', path: r.path, to: i - 1 }));
          const down = el('button', 'os-btn med-skip', '▼');
          down.type = 'button';
          down.setAttribute('aria-label', 'Move ' + r.name + ' down');
          down.disabled = reorderTarget(i, 1, rows.length) < 0;
          spec(down, 'Moves ' + r.name + ' one place later in the queue.');
          down.addEventListener('click', () => queueOp({ op: 'move', path: r.path, to: i + 1 }));
          const del = el('button', 'os-btn med-skip', '✕');
          del.type = 'button';
          del.setAttribute('aria-label', 'Remove ' + r.name);
          spec(del, 'Takes ' + r.name + ' out of the queue. The file is not touched.');
          del.addEventListener('click', () => queueOp({ op: 'remove', path: r.path }));
          row.append(play, up, down, del);
          wrap.append(row);
        });
        states.populated(queueList, wrap);
      }
      paintNav();
    }

    function paintNav() {
      const here = st.row ? st.row.path : '';
      prevBtn.disabled = !st.media;
      nextBtn.disabled = neighbour(st.queue, here, 1) < 0;
      queueAddBtn.disabled = !(st.row && (st.row.kind === 'audio' || st.row.kind === 'video')) || st.queue.some((r) => r.path === here);
      queueNote.textContent = st.row ? queueLabel(st.queue, st.row.path) && 'Queue: ' + queueLabel(st.queue, st.row.path) : '';
    }

    async function loadQueue() {
      try {
        const d = await ctx.api.get('/api/os/media/queue');
        if (ctx.signal.aborted) return;
        st.queue = d.queue || [];
        paintQueue();
      } catch (err) {
        if (isAbort(err)) return;
        states.error(queueList, err, { title: 'Could not read the queue', retry: () => loadQueue() });
      }
    }

    async function queueOp(body) {
      try {
        const d = await ctx.api.post('/api/os/media/queue', body);
        if (ctx.signal.aborted) return;
        st.queue = d.queue || [];
        paintQueue();
      } catch (err) {
        if (!isAbort(err)) note('Queue: ' + err.message);
      }
    }

    function skip(step) {
      if (st.row && st.row.kind === 'pdf') {
        pdf.step(step);
        return;
      }
      const m = st.media;
      if (step < 0 && m && m.currentTime > 3) {
        m.currentTime = 0;
        return;
      }
      const at = neighbour(st.queue, st.row ? st.row.path : '', step);
      if (at < 0) {
        note(step > 0 ? 'Nothing is queued after this.' : 'Nothing is queued before this.');
        return;
      }
      openPath(st.queue[at].path, { autoplay: true });
    }

    /* ---------------- library ---------------- */
    function paintSources() {
      srcEl.replaceChildren();
      const mk = (label, key, title, disabled) => {
        const b = el('button', 'tag', label);
        b.type = 'button';
        b.disabled = Boolean(disabled);
        spec(b, title);
        b.addEventListener('click', () => (key === 'recent' ? paintRecent() : browse(key)));
        srcEl.append(b);
      };
      mk('RECENT', 'recent', 'Shows the files opened here before. A file that has since been moved or deleted is left out.');
      ((st.lib && st.lib.roots) || []).forEach((r) => {
        mk(r.name.toUpperCase(), r.name, r.exists ? 'Lists the audio, video and image files directly inside ' + r.path + ' (not subfolders, newest first, at most 100).' : r.path + ' does not exist on this machine.', !r.exists);
      });
    }

    function fileRows(files, footer) {
      if (!files.length) return false;
      const wrap = el('div');
      files.forEach((f) => {
        const row = el('button', 'os-row med-file');
        row.type = 'button';
        spec(row, 'Opens ' + f.name + ' in the player.');
        row.append(el('span', 'tag', f.kind || 'file'), el('span', 'rt', f.name), el('span', 'muted', [sizeLabel(f.size), agoLabel(f.mtime)].filter(Boolean).join(' · ')));
        row.addEventListener('click', () => openPath(f.path));
        wrap.append(row);
      });
      if (footer) wrap.append(el('div', 'muted', footer));
      states.populated(listEl, wrap);
      return true;
    }

    function paintRecent() {
      st.view = 'recent';
      const recent = (st.lib && st.lib.recent) || [];
      if (!fileRows(recent)) states.empty(listEl, 'No file has been opened here yet.', { hint: 'Type a path above, or pick a folder to look in.' });
    }

    async function loadLibrary() {
      if (!st.lib) states.loading(listEl, 'Reading the recent files');
      try {
        const d = await ctx.api.get('/api/os/media/library');
        if (ctx.signal.aborted) return;
        st.lib = d;
        states.clearStale(listEl);
        paintSources();
        if (!st.view || st.view === 'recent') paintRecent();
        paintFormats();
      } catch (err) {
        if (isAbort(err)) return;
        if (st.lib) states.stale(listEl, 'Could not refresh: ' + err.message);
        else {
          states.error(listEl, err, { title: 'Could not read the library', retry: () => loadLibrary() });
          states.error(fmtEl, err, { title: 'Could not read the supported formats', retry: () => loadLibrary() });
        }
      }
    }

    async function browse(name) {
      st.view = name;
      states.loading(listEl, 'Reading ' + name);
      try {
        const d = await ctx.api.get('/api/os/media/library?root=' + encodeURIComponent(name));
        if (ctx.signal.aborted) return;
        const foot = d.truncated ? 'Showing the newest ' + d.files.length + ' of ' + d.total + '.' : '';
        if (!fileRows(d.files, foot)) states.empty(listEl, 'No audio, video or image files directly inside ' + d.path + '.', { hint: 'Subfolders are not searched. Type a full path to open a file inside one.' });
      } catch (err) {
        if (isAbort(err)) return;
        states.error(listEl, err, { title: 'Could not read that folder', retry: () => browse(name) });
      }
    }

    function paintFormats() {
      const f = st.lib && st.lib.formats;
      if (!f) return;
      fmtEl.dataset.state = 'populated';
      setHtml(fmtEl, html`
        <div class="card"><div class="lbl">Plays directly</div><div class="muted"><b>Audio</b> ${formatText(f.audio)}<br><b>Video</b> ${formatText(f.video)}<br><b>Images</b> ${formatText(f.image)}</div></div>
        <div class="card"><div class="lbl">Converted first</div><div class="muted"><b>Video</b> ${formatText(f.convert_video)}<br><b>Audio</b> ${formatText(f.convert_audio)}<br>${st.lib.ffmpeg ? 'ffmpeg is present, so these play after a repackage or a conversion.' : 'ffmpeg is NOT installed here, so these cannot be converted and will be refused.'}</div></div>
        <div class="card"><div class="lbl">Refused honestly</div><div class="muted">Anything ffmpeg cannot read, a path outside the allowed folders, and any other file type. A blank player would be a lie, so the reason is shown instead.</div></div>`);
    }

    /* ---------------- player ---------------- */
    function stopPoll() {
      if (st.stopPoll) {
        st.stopPoll();
        st.stopPoll = null;
      }
    }

    function dropMedia() {
      stopPoll();
      if (st.media) {
        try {
          st.media.pause();
        } catch (_err) {
          /* nothing playing */
        }
        st.media.removeAttribute('src');
        st.media.querySelectorAll('track').forEach((t) => t.remove());
        try {
          st.media.load();
        } catch (_err) {
          /* already released */
        }
        st.media = null;
      }
      pdf.close();
    }

    function resetTransport(on) {
      playBtn.disabled = !on;
      seek.disabled = !on;
      pdfBarEl.hidden = true;
      barEl.hidden = false;
      fsBtn.hidden = true;
      seek.max = '0';
      seek.value = '0';
      curEl.textContent = '';
      durEl.textContent = '';
      playBtn.textContent = '▶';
      playBtn.setAttribute('aria-label', 'Play');
    }

    function paintInfo() {
      infoEl.textContent = st.row ? infoLine(st.row, st.facts) : '';
    }

    function playerError(text) {
      dropMedia();
      resetTransport(false);
      root.dataset.state = 'error';
      stageEl.dataset.state = 'error';
      stageEl.replaceChildren(el('div', 'med-msg bad', text));
      note('');
    }

    function attach(row, isVideo) {
      const media = document.createElement(isVideo ? 'video' : 'audio');
      media.preload = 'metadata';
      media.playsInline = true;
      if (isVideo) media.setAttribute('aria-label', row.name);
      (row.subtitles || []).forEach((s, i) => {
        const t = document.createElement('track');
        t.kind = 'subtitles';
        t.label = s.label || 'Subtitles';
        t.src = s.url;
        if (i === 0) t.default = true;
        media.append(t);
      });
      st.media = media;
      media.addEventListener('loadedmetadata', () => {
        st.facts.duration = Number.isFinite(media.duration) ? media.duration : st.facts.duration;
        if (isVideo) {
          st.facts.width = media.videoWidth;
          st.facts.height = media.videoHeight;
        }
        seek.max = String(Number.isFinite(media.duration) ? media.duration : 0);
        durEl.textContent = clock(media.duration);
        curEl.textContent = clock(media.currentTime) || '0:00';
        resetEnable();
        paintInfo();
        media.volume = st.volume;
        media.muted = st.muted;
        media.playbackRate = st.rate;
        if (st.pendingSeek !== null) {
          media.currentTime = seekTarget(0, st.pendingSeek, media.duration);
          st.pendingSeek = null;
        }
        report(true);
      });
      media.addEventListener('seeked', () => report(true));
      media.addEventListener('timeupdate', () => {
        curEl.textContent = clock(media.currentTime) || '0:00';
        if (!st.dragging) seek.value = String(media.currentTime);
      });
      media.addEventListener('play', () => {
        playBtn.textContent = '❚❚';
        playBtn.setAttribute('aria-label', 'Pause');
        report(true);
      });
      const paused = () => {
        playBtn.textContent = '▶';
        playBtn.setAttribute('aria-label', 'Play');
        if (st.media === media) report(true);
      };
      media.addEventListener('pause', paused);
      media.addEventListener('ended', () => {
        paused();
        if (st.media !== media) return;
        const at = neighbour(st.queue, st.row ? st.row.path : '', 1);
        if (at >= 0 && st.row && st.queue.some((r) => r.path === st.row.path)) openPath(st.queue[at].path, { autoplay: true });
      });
      media.addEventListener('error', () => {
        if (st.media !== media) return;
        playerError(mediaErrorText(media.error && media.error.code));
      });
      stageEl.dataset.state = 'populated';
      root.dataset.state = 'populated';
      fsBtn.hidden = !isVideo;
      if (isVideo) stageEl.replaceChildren(media);
      else stageEl.replaceChildren(el('div', 'med-msg', 'Audio: ' + row.name));
      media.src = row.urls.media;
      playBtn.disabled = false;
      seek.disabled = false;
      paintNav();
      if (st.pendingPlay) {
        st.pendingPlay = false;
        media.play().catch((err) => note('Could not start playback: ' + (err && err.message ? err.message : String(err))));
      }
    }

    function resetEnable() {
      playBtn.disabled = false;
      seek.disabled = false;
    }

    async function startAfterConvert(row, my) {
      let polls = 0;
      let inflight = false;
      const started = Date.now();
      states.loading(stageEl, convertLabel(null));
      root.dataset.state = 'loading';
      const check = async () => {
        if (my !== st.seq || ctx.signal.aborted || inflight) return;
        polls += 1;
        let job;
        inflight = true;
        try {
          job = await ctx.api.get(row.urls.status);
        } catch (err) {
          if (isAbort(err)) return;
          stopPoll();
          playerError('Could not read the conversion status: ' + err.message);
          return;
        } finally {
          inflight = false;
        }
        if (my !== st.seq || ctx.signal.aborted) return;
        if (job.state === 'ready') {
          stopPoll();
          attach(row, row.kind === 'video');
        } else if (job.state === 'failed') {
          stopPoll();
          playerError(job.error || 'ffmpeg could not make this file playable.');
        } else if (polls >= CONVERT_POLL_MAX || Date.now() - started > CONVERT_POLL_MAX * CONVERT_POLL_MS) {
          stopPoll();
          playerError('Still converting after a long wait. Press OPEN again later to check.');
        } else {
          stageEl.replaceChildren(el('div', 'med-msg', convertLabel(job)));
        }
      };
      st.stopPoll = ctx.every(CONVERT_POLL_MS, check);
      await check();
    }

    async function openPath(raw, opts = {}) {
      const path = String(raw || '').trim();
      if (!path) {
        note('Type a full path first.');
        pathIn.focus();
        return;
      }
      st.seq += 1;
      const my = st.seq;
      st.pendingPlay = Boolean(opts.autoplay);
      st.pendingSeek = typeof opts.seek === 'number' ? opts.seek : null;
      dropMedia();
      resetTransport(false);
      st.row = null;
      st.facts = {};
      infoEl.textContent = '';
      note('');
      states.loading(stageEl, 'Checking the file');
      root.dataset.state = 'loading';
      let row;
      try {
        row = await ctx.api.post('/api/os/media/open', { path });
      } catch (err) {
        if (isAbort(err) || my !== st.seq) return;
        stageEl.dataset.state = 'error';
        stageEl.replaceChildren(el('div', 'med-msg bad', err.message));
        root.dataset.state = 'error';
        infoEl.textContent = '';
        return;
      }
      if (my !== st.seq || ctx.signal.aborted) return;
      st.row = row;
      st.aliases[path] = row.path;
      st.facts = { ...(row.probe || {}) };
      paintInfo();
      paintQueue();
      ctx.chrome.setSub(row.name);
      pathIn.value = row.path;
      if (row.kind === 'pdf') {
        barEl.hidden = true;
        pdfBarEl.hidden = false;
        root.dataset.state = 'populated';
        if (!pdf.open(row)) root.dataset.state = 'error';
        infoEl.textContent = row.name + (row.page_count ? ' · ' + row.page_count + ' pages' : '') + ' · ' + sizeLabel(row.size);
        queueNote.textContent = '';
      } else if (row.kind === 'image') {
        const img = el('img');
        img.alt = row.name;
        img.addEventListener('error', () => playerError('This window could not display that image.'));
        img.addEventListener('load', () => {
          st.facts.width = img.naturalWidth;
          st.facts.height = img.naturalHeight;
          paintInfo();
        });
        img.src = row.urls.image;
        stageEl.dataset.state = 'populated';
        root.dataset.state = 'populated';
        stageEl.replaceChildren(img);
      } else if (row.needs_convert) {
        await startAfterConvert(row, my);
      } else {
        attach(row, row.kind === 'video');
      }
      loadLibrary();
      report(true);
    }

    /* ---------------- controls ---------------- */
    playBtn.addEventListener('click', () => togglePlay());
    prevBtn.addEventListener('click', () => skip(-1));
    nextBtn.addEventListener('click', () => skip(1));
    queueAddBtn.addEventListener('click', () => st.row && queueOp({ op: 'add', path: st.row.path }));
    queueClear.addEventListener('click', () => queueOp({ op: 'clear' }));
    function applyAudio() {
      muteBtn.textContent = st.muted ? 'UNMUTE' : 'MUTE';
      muteBtn.setAttribute('aria-label', st.muted ? 'Unmute' : 'Mute');
      if (st.media) {
        st.media.volume = st.volume;
        st.media.muted = st.muted;
      }
    }
    muteBtn.addEventListener('click', () => {
      st.muted = !st.muted;
      applyAudio();
    });
    vol.addEventListener('input', () => {
      st.volume = Number(vol.value);
      if (st.volume > 0) st.muted = false;
      applyAudio();
    });
    function setRate(rate) {
      st.rate = rate;
      speedBtn.textContent = rate + 'x';
      speedBtn.setAttribute('aria-label', 'Playback speed ' + rate + 'x');
      if (st.media) st.media.playbackRate = rate;
    }
    speedBtn.addEventListener('click', () => setRate(RATES[(RATES.indexOf(st.rate) + 1) % RATES.length]));
    function fullScreen() {
      const target = stageEl.querySelector('video') || null;
      if (!target) return;
      const want = target.requestFullscreen || target.webkitRequestFullscreen;
      if (!want) {
        note('This window cannot show a video full screen.');
        return;
      }
      Promise.resolve(want.call(target)).catch((err) => note('Could not go full screen: ' + (err && err.message ? err.message : String(err))));
    }
    fsBtn.addEventListener('click', fullScreen);
    function nudgeVolume(delta) {
      st.volume = Math.max(0, Math.min(1, Math.round((st.volume + delta) * 100) / 100));
      vol.value = String(st.volume);
      if (delta > 0) st.muted = false;
      applyAudio();
    }
    function togglePlay() {
      const m = st.media;
      if (!m) return;
      if (m.paused) m.play().catch((err) => note('Could not start playback: ' + (err && err.message ? err.message : String(err))));
      else m.pause();
    }

    /* ---------------- YouTube and Spotify ---------------- */
    async function openWeb(service) {
      const r = parseWebLink(webIn.value, service);
      if (!r.ok) {
        note(r.error);
        webIn.focus();
        return;
      }
      try {
        if (ctx.host.pane) {
          await ctx.api.post('/api/browser-pane/open', { url: r.url });
          note('Sent to the browser pane. Open BROWSER to see it.');
        } else if (ctx.host.openExternal(r.url)) {
          note('Opened in the system browser.');
        } else {
          note('This window has no browser pane and could not open the system browser. The link is ' + r.url);
        }
      } catch (err) {
        if (!isAbort(err)) note('Could not open it: ' + err.message);
      }
    }
    ytBtn.addEventListener('click', () => openWeb('youtube'));
    spBtn.addEventListener('click', () => openWeb('spotify'));
    webIn.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        openWeb(/spotify/i.test(webIn.value) ? 'spotify' : 'youtube');
      }
    });

    /* ---------------- media keys ---------------- */
    /* Arrows, M and Shift+arrows go through ctx.keys (they are ignored while
       typing). The keymap cannot bind the space bar, so space is handled on
       this screen's own root, and only when focus is not on a control that
       already uses it. */
    const reading = () => Boolean(st.row && st.row.kind === 'pdf');
    ctx.keys.bind('ArrowRight', () => (reading() ? pdf.step(1) : st.media && (st.media.currentTime = seekTarget(st.media.currentTime, SEEK_STEP, st.media.duration))));
    ctx.keys.bind('ArrowLeft', () => (reading() ? pdf.step(-1) : st.media && (st.media.currentTime = seekTarget(st.media.currentTime, -SEEK_STEP, st.media.duration))));
    ctx.keys.bind('ArrowUp', () => st.media && nudgeVolume(VOL_STEP));
    ctx.keys.bind('ArrowDown', () => st.media && nudgeVolume(-VOL_STEP));
    ctx.keys.bind('m', () => {
      st.muted = !st.muted;
      applyAudio();
    });
    ctx.keys.bind('f', () => fullScreen());
    ctx.keys.bind('Shift+ArrowRight', () => skip(1));
    ctx.keys.bind('Shift+ArrowLeft', () => skip(-1));
    root.addEventListener('keydown', (e) => {
      if (e.key !== ' ' || e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey) return;
      const t = e.target;
      const tag = t && t.tagName ? t.tagName.toUpperCase() : '';
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || tag === 'BUTTON' || (t && t.isContentEditable)) return;
      if (!st.media) return;
      e.preventDefault();
      togglePlay();
    });
    stageEl.addEventListener('click', () => {
      stageEl.tabIndex = -1;
      stageEl.focus();
    });

    /* ---------------- player_control from the server ---------------- */
    /* POST /api/player/control broadcasts {type: 'player_control', action,
       seconds?, path}. Apply it here; the state is reported back through
       /api/os/media/player-state. Only works while this screen is mounted. */
    ctx.events.on('player_control', async (evt) => {
      const plan = controlPlan(evt, st.row ? st.row.path : '', st.aliases);
      if (!plan) return;
      if (plan.reopen) {
        await openPath(plan.path, { autoplay: plan.action === 'play', seek: plan.action === 'seek' ? plan.seconds : null });
        return;
      }
      const m = st.media;
      if (!m) {
        note('The player was asked to ' + plan.action + ' but no audio or video is open.');
        return;
      }
      if (plan.action === 'pause') m.pause();
      else if (plan.action === 'seek') m.currentTime = seekTarget(0, plan.seconds, m.duration);
      else m.play().catch((err) => note('Could not start playback: ' + (err && err.message ? err.message : String(err))));
      report(true);
    });
    ctx.every(REPORT_MS, () => report(false));

    seek.addEventListener('input', () => {
      st.dragging = true;
      curEl.textContent = clock(Number(seek.value)) || '0:00';
    });
    seek.addEventListener('change', () => {
      st.dragging = false;
      if (st.media) st.media.currentTime = Number(seek.value);
    });
    goBtn.addEventListener('click', () => openPath(pathIn.value));
    pathIn.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        openPath(pathIn.value);
      }
    });
    ctx.signal.addEventListener('abort', () => {
      dropMedia();
      /* Leaving the screen cannot report: ctx.api is cancelled and raw requests are banned.
         player-state carries the age of the last report, so a reader sees it go stale. */
    });
    ctx.events.onResync(() => {
      loadLibrary();
      loadQueue();
    });

    /* ---------------- start ---------------- */
    states.empty(stageEl, 'No file is open.', { hint: 'Press OPEN FILE, then type a path or pick a folder.' });
    states.loading(fmtEl, 'Reading the supported formats');
    await view.start();
    await loadLibrary();
    loadQueue();
    ctx.chrome.setLive(false);
  },

  async refresh(ctx, reason) {
    if (reason === 'manual') ctx.notify({ level: 'info', title: 'MEDIA', detail: 'The player and the thread are live; nothing to refresh.', ttl: 2500 });
  },

  unmount() {},
};
