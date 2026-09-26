/* NEWS: live headlines above a conversation with the news agent.

   The catch-up list is one read of GET /api/news; after that every headline
   arrives as a news_item event on the shared stream (no polling). The list is
   a ring of FEED_CAP items. A headline opens in the browser pane (or the
   system browser when this window has no pane). TO RESEARCH asks first, then
   writes one research record through POST /api/os/news/research. */

import { html, setHtml } from '../../kit/html.js';
import { states } from '../../kit/states.js';
import { ago } from '../../kit/format.js';
import { mountThreadView } from '../../kit/thread-view.js';
import { ring } from '../../core/ring.js';
import { isAbort } from '../../core/api.js';
import { FEED_CAP, channelWord, safeLink, itemKey, itemAt, isImportant, watcherLine, researchPrompt } from './helpers.js';

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

export default {
  id: 'NEWS',
  sub: 'live headlines',
  css: true,
  thread: true,

  async mount(root, ctx) {
    const feed = ring(FEED_CAP);
    const seen = new Set();
    let resp = null; /* the last GET /api/news answer */
    let loadError = null;
    let live = 0; /* headlines that arrived as events since this screen opened */

    root.dataset.state = 'loading';
    setHtml(root, html`
      <div class="news-status" id="newsStatus" role="status" hidden></div>
      <div id="newsConfirm"></div>
      <div class="card"><div class="lbl">Headlines</div><div id="newsFeed" data-region></div></div>
      <div class="news-thread" id="newsThread"></div>`);
    const statusEl = root.querySelector('#newsStatus');
    const confirmEl = root.querySelector('#newsConfirm');
    const feedEl = root.querySelector('#newsFeed');
    const threadEl = root.querySelector('#newsThread');

    const remember = (item) => {
      const k = itemKey(item);
      if (seen.has(k)) return false;
      seen.add(k);
      feed.push(item);
      if (seen.size > FEED_CAP * 4) {
        const keep = new Set(feed.items().map(itemKey));
        seen.clear();
        keep.forEach((x) => seen.add(x));
      }
      return true;
    };

    /* ---------------- opening a headline ---------------- */
    async function open(item) {
      const url = safeLink(item.link);
      if (!url) return;
      try {
        if (ctx.host.pane) {
          await ctx.api.post('/api/browser-pane/open', { url });
          ctx.notify({ level: 'info', title: 'Sent to the browser pane', detail: 'Open BROWSER to read it.', ttl: 3000 });
        } else if (!ctx.host.openExternal(url)) {
          ctx.notify({ level: 'warn', title: 'Nothing to open it with', detail: 'This window has no browser pane and could not open the system browser.' });
        }
      } catch (err) {
        if (!isAbort(err)) ctx.notify({ level: 'error', title: 'Could not open the headline', detail: err && err.message });
      }
    }

    /* ---------------- TO RESEARCH (asks first) ---------------- */
    function toResearch(item) {
      const entry = { id: 'news-research-' + Date.now(), prompt: researchPrompt(item), email: false, autonomous: false, state: 'pending', busy: false, error: '' };
      const card = ctx.kit.approvalCard(entry, async (ok) => {
        if (!ok) {
          confirmEl.replaceChildren();
          return true;
        }
        entry.busy = true;
        entry.error = '';
        card.paint();
        try {
          const r = await ctx.api.post('/api/os/news/research', { url: safeLink(item.link), title: item.title, channel: item.channel });
          entry.state = 'approved';
          ctx.notify({
            level: 'ok',
            title: r.created ? 'Research record started' : 'Added to an existing research record',
            detail: r.source_added ? 'The link is its source. Continue it from RESEARCH.' : 'That link was already a source.',
            ttl: 4000,
          });
          confirmEl.replaceChildren();
        } catch (err) {
          entry.error = err && err.message ? err.message : String(err);
        }
        entry.busy = false;
        if (confirmEl.contains(card.el)) card.paint();
        return true;
      });
      confirmEl.replaceChildren(card.el);
      card.focus();
    }

    /* Esc dismisses the ask without doing it (the shell's own Esc stack is not
       reachable from a screen, so this listens on the screen's own root). */
    root.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape' || e.target.closest('input, textarea')) return;
      if (confirmEl.firstChild) confirmEl.replaceChildren();
    });

    /* ---------------- painting ---------------- */
    function paintStatus() {
      const w = watcherLine(resp);
      statusEl.hidden = false;
      statusEl.textContent = w.text;
      statusEl.dataset.tone = w.tone;
    }

    function rowFor(item) {
      const row = el('div', 'os-row news-row');
      row.append(el('span', 'tag', channelWord(item.channel)));
      if (isImportant(item)) row.append(el('span', 'tag warn', 'important'));
      const url = safeLink(item.link);
      const title = el('button', 'news-title', String(item.title || '(no title)'));
      title.type = 'button';
      title.dataset.spec = url
        ? 'Opens this article in the browser pane, or in the system browser when this window has no pane.'
        : 'This headline carries no web link, so there is nothing to open.';
      if (!url) title.disabled = true;
      else title.addEventListener('click', () => open(item));
      row.append(title);
      const when = ago(itemAt(item));
      if (when) row.append(el('span', 'news-when', when));
      if (url) {
        const b = el('button', 'os-btn', 'TO RESEARCH');
        b.type = 'button';
        b.dataset.spec = 'Asks first, then creates a research record with this headline as its question and this link as its first source. It writes one local row and calls no model.';
        b.addEventListener('click', () => toResearch(item));
        row.append(b);
      }
      return row;
    }

    function paintFeed() {
      const items = feed.newestFirst();
      if (!items.length) {
        if (loadError && !resp) {
          root.dataset.state = 'error';
          states.error(feedEl, loadError, { title: 'Could not read the headlines', retry: () => load('manual') });
          return;
        }
        if (!resp || resp.running === false) {
          root.dataset.state = 'unavailable';
          states.unavailable(feedEl, 'The news watcher is not running, so there is nothing to show.', {
            detail: 'It starts with the server. Headlines appear here the moment it is running and has polled once.',
          });
        } else {
          root.dataset.state = 'empty';
          states.empty(feedEl, 'No headlines yet.', { hint: 'The watcher has not found a new item since it started. The first batch usually arrives within a few minutes.' });
        }
        return;
      }
      root.dataset.state = 'populated';
      const foot = el('div', 'muted news-foot');
      foot.textContent = 'Newest ' + items.length + ' headlines' + (feed.size >= FEED_CAP ? ' (older ones drop off at ' + FEED_CAP + ')' : '') + '. ' + (live ? live + ' arrived live since you opened this screen.' : 'New ones appear here as they arrive.');
      states.populated(feedEl, [...items.map(rowFor), foot]);
    }

    /* ---------------- reads ---------------- */
    async function load(reason) {
      try {
        const d = await ctx.api.get('/api/news?limit=' + FEED_CAP);
        if (ctx.signal.aborted) return;
        resp = d;
        loadError = null;
        states.clearStale(root);
        const items = Array.isArray(d.items) ? d.items.slice().reverse() : []; /* oldest first so the ring ends newest */
        items.forEach(remember);
        paintStatus();
        paintFeed();
        ctx.chrome.setLive(Boolean(d.running));
      } catch (err) {
        if (isAbort(err) || ctx.signal.aborted) return;
        loadError = err;
        if (feed.size) states.stale(root, 'Could not refresh: ' + err.message + ' The headlines below are the ones already received.');
        else paintFeed();
      }
    }

    ctx.events.on('news_item', (evt) => {
      const item = evt && evt.item;
      if (!item || typeof item !== 'object') return;
      if (!remember(item)) return;
      live += 1;
      if (isImportant(item)) ctx.notify({ level: 'warn', title: 'Important headline', detail: String(item.title || '').slice(0, 160), ttl: 5000 });
      paintFeed();
    });
    ctx.events.onResync(() => load('resync'));

    /* ---------------- the conversation ---------------- */
    const view = mountThreadView(threadEl, ctx, {
      scroller: root.parentElement,
      placeholder: 'Ask the news agent about a headline or a topic',
      emptyMessage: 'No conversation about the news yet.',
      emptyHint: 'Ask below. The headlines above update on their own.',
    });

    states.loading(feedEl, 'Reading the headlines the watcher has collected');
    await Promise.all([load('show'), view.start()]);
  },

  async refresh(ctx, reason) {
    if (reason === 'manual') ctx.notify({ level: 'info', title: 'NEWS', detail: 'Headlines arrive on their own; nothing to refresh.', ttl: 2500 });
  },
};
