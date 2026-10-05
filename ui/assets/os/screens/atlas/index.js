/* ATLAS here is the WORLD MONITOR: the public feeds the server polls (quakes,
   storms, launches, air quality and so on) and the pulse it computes from
   them. It is not the strategy lab and never links to it.

   Reads GET /api/world/pulse, which is cached on the server (a cold cache
   does a live fan-out that can take about ten seconds). REFRESH re-polls
   every feed now through POST /api/os/world/refresh, which the server
   throttles. Every count is what a source really returned; a source that
   failed says why instead of showing a number. */

import { html, setHtml } from '../../kit/html.js';
import { states } from '../../kit/states.js';
import { agoLabel } from '../../kit/format.js';
import { isAbort } from '../../core/api.js';
import { POLL_MS, channelRows, totals, pulseTone, PULSE_NOTE, signals, SIGNAL_CAP } from './helpers.js';

export default {
  id: 'ATLAS',
  sub: 'world monitor',
  css: true,
  thread: false,

  async mount(root, ctx) {
    const st = { snap: null, error: null, refreshing: false, chan: '' };

    root.dataset.state = 'loading';
    setHtml(root, html`
      <div class="atl-note" id="atlNote" role="status" hidden></div>
      <div class="grid2">
        <div class="card"><div class="lbl">Channels</div><div id="atlChannels" data-region></div></div>
        <div class="card"><div class="lbl">Pulse</div><div id="atlPulse" data-region></div></div>
      </div>
      <div class="card atl-gap"><div class="lbl" id="atlSigLbl">Strongest signals</div><div id="atlSignals" data-region></div></div>`);
    const noteEl = root.querySelector('#atlNote');
    const chanEl = root.querySelector('#atlChannels');
    const pulseEl = root.querySelector('#atlPulse');
    const sigEl = root.querySelector('#atlSignals');
    const sigLbl = root.querySelector('#atlSigLbl');

    const note = (text, tone) => {
      noteEl.hidden = !text;
      noteEl.textContent = text || '';
      noteEl.dataset.tone = tone || '';
    };

    function paintActions() {
      ctx.chrome.setActions([
        {
          id: 'refresh', label: st.refreshing ? 'REFRESHING' : 'REFRESH', disabled: st.refreshing,
          spec: 'Re-polls every registered public feed now instead of reading the server cache. It only makes outbound requests to those feeds, can take about ten seconds, and the server allows one re-poll every 15 seconds.',
          onClick: () => refresh(),
        },
      ]);
    }

    function paint() {
      if (!st.snap) return;
      const rows = channelRows(st.snap);
      const t = totals(rows);
      if (!rows.length) {
        root.dataset.state = 'empty';
        states.empty(chanEl, 'The server reported no feeds.');
        states.empty(pulseEl, 'There is no pulse without feeds.');
        states.empty(sigEl, 'There are no items without feeds.');
        return;
      }
      if (t.answered === 0) {
        root.dataset.state = 'unavailable';
        const why = rows.map((r) => r.label + ': ' + r.error).join(' | ').slice(0, 400);
        /* F9: the feeds' own error text is for whoever debugs a feed, so it sits behind For developers */
        states.unavailable(chanEl, 'None of the ' + t.feeds + ' feeds answered.', { detail: 'Check this Mac\'s network connection, then try again.', developer: why, retry: () => load('manual') });
        states.unavailable(pulseEl, 'There is no pulse while no feed answers.', { detail: 'Check this Mac\'s network connection, then press REFRESH.' });
        states.unavailable(sigEl, 'There are no items while no feed answers.');
        ctx.chrome.setLive(false);
        return;
      }
      const list = document.createElement('div');
      rows.forEach((r) => {
        const d = document.createElement('div');
        d.className = 'atl-row' + (r.ok ? '' : ' fail');
        const n = document.createElement(r.ok && r.count > 0 ? 'button' : 'span');
        n.className = 'n';
        n.textContent = r.label;
        if (r.ok && r.count > 0) {
          n.type = 'button';
          n.classList.add('atl-pick');
          n.setAttribute('aria-pressed', String(st.chan === r.name));
          n.dataset.spec = 'Shows the items this feed returned in the list below. Press again to show the strongest items of every feed.';
          n.addEventListener('click', () => {
            st.chan = st.chan === r.name ? '' : r.name;
            paint();
            const again = chanEl.querySelector('.atl-pick[aria-pressed="true"]') || chanEl.querySelector('.atl-pick');
            if (again) again.focus();
          });
        }
        d.append(n);
        if (r.ok) {
          const b = document.createElement('b');
          b.textContent = String(r.count);
          d.append(b);
        } else {
          const tag = document.createElement('span');
          tag.className = 'tag warn';
          tag.textContent = 'no answer';
          d.append(tag);
          const e = document.createElement('details');
          e.className = 'atl-err';
          const sum = document.createElement('summary');
          sum.textContent = 'Details';
          const raw = document.createElement('div');
          raw.textContent = r.error;
          e.append(sum, raw);
          d.append(e);
        }
        list.append(d);
      });
      const cap = document.createElement('div');
      cap.className = 'muted';
      cap.textContent = 'Each number is how many items that feed returned, located or not.';
      states.populated(chanEl, [list, cap]);

      const label = String(st.snap.pulse_label || 'UNKNOWN');
      const wrap = document.createElement('div');
      wrap.className = 'atl-pulse';
      const big = document.createElement('div');
      big.className = 'big tone-' + pulseTone(label);
      big.textContent = label;
      const line = document.createElement('div');
      line.className = 'muted';
      const score = Number.isFinite(Number(st.snap.pulse_score)) ? ' Score ' + Number(st.snap.pulse_score) + ' of 100.' : '';
      line.textContent = t.events + ' events across ' + t.reporting + ' channels that reported, out of ' + t.feeds + ' feeds' + (t.failed ? ' (' + t.failed + ' did not answer)' : '') + '.' + score;
      const when = document.createElement('div');
      when.className = 'muted';
      when.textContent = 'Read ' + (agoLabel(st.snap.generated_at) || 'at an unknown time') + '. The server caches this for about two minutes.';
      const why = document.createElement('div');
      why.className = 'muted';
      why.textContent = PULSE_NOTE;
      wrap.append(big, line, when, why);
      states.populated(pulseEl, wrap);

      paintSignals();
      root.dataset.state = 'populated';
      ctx.chrome.setLive(t.answered > 0);
    }

    function paintSignals() {
      if (st.chan && !rowsHave(st.chan)) st.chan = '';
      const sg = signals(st.snap, st.chan);
      sigLbl.textContent = st.chan ? 'Items from ' + st.chan.replace(/_/g, ' ') + ' (' + sg.total + ')' : 'Strongest signals';
      if (!sg.rows.length) {
        states.empty(sigEl, 'No feed returned an item.', { hint: 'A feed can answer with zero items when nothing is happening. Press REFRESH to ask again.' });
        return;
      }
      const list = document.createElement('div');
      sg.rows.forEach((s) => {
        const row = document.createElement('div');
        row.className = 'os-row atl-item';
        const tag = document.createElement('span');
        tag.className = 'tag ' + (s.rank <= 1 ? 'bad' : s.rank === 2 ? 'warn' : '');
        tag.textContent = s.severity || s.label;
        const body = document.createElement('span');
        body.className = 'rt';
        const t = document.createElement('b');
        t.textContent = s.title;
        body.append(t);
        const meta = [st.chan ? '' : s.label, s.loc, s.summary].filter(Boolean).join(' · ');
        if (meta) {
          const m = document.createElement('div');
          m.className = 'muted atl-meta';
          m.textContent = meta;
          body.append(m);
        }
        row.append(tag, body);
        if (s.link) {
          const b = document.createElement('button');
          b.type = 'button';
          b.className = 'os-btn';
          b.textContent = 'OPEN';
          b.dataset.spec = 'Opens the source page for this item in the system browser. It changes nothing here.';
          b.addEventListener('click', () => {
            if (!ctx.host.openExternal(s.link)) ctx.notify({ level: 'warn', title: 'ATLAS', detail: 'This window could not open the link.' });
          });
          row.append(b);
        }
        list.append(row);
      });
      const foot = document.createElement('div');
      foot.className = 'muted';
      foot.textContent = 'Showing ' + sg.rows.length + ' of ' + sg.total + ' items, strongest first, exactly as the feeds returned them. Nothing here is scored or rewritten by a model.';
      states.populated(sigEl, [list, foot]);
    }

    function rowsHave(name) {
      return channelRows(st.snap).some((r) => r.name === name && r.ok && r.count > 0);
    }

    async function load(reason) {
      try {
        const d = await ctx.api.get('/api/world/pulse');
        if (ctx.signal.aborted) return;
        st.snap = d;
        st.error = null;
        states.clearStale(root);
        note('');
        paint();
      } catch (err) {
        if (isAbort(err) || ctx.signal.aborted) return;
        st.error = err;
        if (st.snap) states.stale(root, 'Could not refresh: ' + err.message + ' The figures are from the last read.');
        else {
          root.dataset.state = 'error';
          states.error(chanEl, err, { title: 'Could not read the world monitor', retry: () => load('manual') });
          states.error(pulseEl, err, { title: 'Could not read the world monitor', retry: () => load('manual') });
          states.error(sigEl, err, { title: 'Could not read the world monitor', retry: () => load('manual') });
        }
      }
    }

    async function refresh() {
      if (st.refreshing) return;
      st.refreshing = true;
      paintActions();
      note('Re-polling every feed. This can take about ten seconds.', '');
      try {
        const d = await ctx.api.post('/api/os/world/refresh', {});
        if (ctx.signal.aborted) return;
        st.snap = d;
        st.error = null;
        states.clearStale(root);
        note('');
        paint();
      } catch (err) {
        if (isAbort(err) || ctx.signal.aborted) return;
        /* a throttle or a refresh already running is an ordinary answer, not a failure */
        const plain = err && err.body && (err.body.throttled || err.body.busy);
        note(err && err.message ? err.message : String(err), plain ? '' : 'error');
      } finally {
        st.refreshing = false;
        paintActions();
      }
    }

    paintActions();
    ctx.events.onResync(() => load('resync'));
    ctx.every(POLL_MS, () => load('poll'));
    states.loading(chanEl, 'Reading the feeds. A cold start can take about ten seconds');
    states.loading(pulseEl, 'Working out the pulse');
    states.loading(sigEl, 'Reading the items');
    await load('show');
  },

  async refresh(ctx, reason) {
    if (reason === 'manual') ctx.notify({ level: 'info', title: 'ATLAS', detail: 'Use REFRESH to re-poll the feeds now.', ttl: 2500 });
  },
};
