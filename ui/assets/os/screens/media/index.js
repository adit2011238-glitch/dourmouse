/* MEDIA: a player for audio, video and images, plus this screen's own thread.

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
import { clock, sizeLabel, infoLine, mediaErrorText, formatText, convertLabel, CONVERT_POLL_MS, CONVERT_POLL_MAX } from './helpers.js';

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
    const st = { lib: null, openPanel: false, row: null, media: null, seq: 0, stopPoll: null, dragging: false, facts: {} };

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
    tx.append(playBtn, curEl, seek, durEl);
    barEl.append(infoEl, tx);
    playerEl.append(stageEl, barEl);

    const fmtEl = el('div', 'grid3');
    fmtEl.dataset.region = '';
    const threadEl = el('div', 'med-thread');

    root.replaceChildren(noteEl, openEl, playerEl, fmtEl, threadEl);
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
    }

    function resetTransport(on) {
      playBtn.disabled = !on;
      seek.disabled = !on;
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
      });
      media.addEventListener('timeupdate', () => {
        curEl.textContent = clock(media.currentTime) || '0:00';
        if (!st.dragging) seek.value = String(media.currentTime);
      });
      media.addEventListener('play', () => {
        playBtn.textContent = '❚❚';
        playBtn.setAttribute('aria-label', 'Pause');
      });
      const paused = () => {
        playBtn.textContent = '▶';
        playBtn.setAttribute('aria-label', 'Play');
      };
      media.addEventListener('pause', paused);
      media.addEventListener('ended', paused);
      media.addEventListener('error', () => {
        if (st.media !== media) return;
        playerError(mediaErrorText(media.error && media.error.code));
      });
      stageEl.dataset.state = 'populated';
      root.dataset.state = 'populated';
      if (isVideo) stageEl.replaceChildren(media);
      else stageEl.replaceChildren(el('div', 'med-msg', 'Audio: ' + row.name));
      media.src = row.urls.media;
      playBtn.disabled = false;
      seek.disabled = false;
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

    async function openPath(raw) {
      const path = String(raw || '').trim();
      if (!path) {
        note('Type a full path first.');
        pathIn.focus();
        return;
      }
      st.seq += 1;
      const my = st.seq;
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
      st.facts = { ...(row.probe || {}) };
      paintInfo();
      ctx.chrome.setSub(row.name);
      pathIn.value = row.path;
      if (row.kind === 'image') {
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
    }

    /* ---------------- controls ---------------- */
    playBtn.addEventListener('click', () => {
      const m = st.media;
      if (!m) return;
      if (m.paused) {
        m.play().catch((err) => note('Could not start playback: ' + (err && err.message ? err.message : String(err))));
      } else {
        m.pause();
      }
    });
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
    ctx.signal.addEventListener('abort', dropMedia);
    ctx.events.onResync(() => loadLibrary());

    /* ---------------- start ---------------- */
    states.empty(stageEl, 'No file is open.', { hint: 'Press OPEN FILE, then type a path or pick a folder.' });
    states.loading(fmtEl, 'Reading the supported formats');
    await view.start();
    await loadLibrary();
    ctx.chrome.setLive(false);
  },

  async refresh(ctx, reason) {
    if (reason === 'manual') ctx.notify({ level: 'info', title: 'MEDIA', detail: 'The player and the thread are live; nothing to refresh.', ttl: 2500 });
  },

  unmount() {},
};
