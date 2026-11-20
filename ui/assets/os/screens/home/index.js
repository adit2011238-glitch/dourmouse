/* HOME: greeting, one ask box with a project picker, and three cards (Needs you, In the office, Today) plus
   Continue tiles for projects. Everything is read from GET /api/os/today, which reads real stores only.

   An ask from here is a quick ask unless a project is picked in the box:
   - quick: it talks on a throwaway tab ("quick:<id>"). The server keeps no ledger worth keeping and learns
     nothing from it. "Save to project" copies the exchange into a project thread on request.
   - in a project: a project thread is created and the ask talks on that thread's tab
     ("pj:<project>:<thread>", so the project's instructions and files seed it); when the answer is done the
     exchange is saved with POST /api/os/project/thread/turn. */

import { html, setHtml } from '../../kit/html.js';
import { md } from '../../kit/md.js';
import { whenShort } from '../../kit/format.js';
import { icon } from '../../kit/icons.js';
import { isAbort } from '../../core/api.js';
import { readSeen, markSeen } from '../apps/walkthrough.js';
import {
  greetingLine, subLine, coverVar, avatarCover, statusWord, officeLine, calendarEmpty, quickTab, projectTab,
  threadTitle, tileLine, needsCount, answered, dataAge, cardNumber, orderedStrip, privacyText, continueLine,
} from './helpers.js';

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

export default {
  id: 'HOME',
  sub: 'central agent dispatch',
  css: true,
  thread: true,

  async mount(root, ctx) {
    const st = { today: null, pick: '', tab: quickTab(), ask: null, saved: null, projects: [] };
    root.dataset.state = 'loading';
    setHtml(root, html`
      <div class="hm">
        <header class="hm-head"><h1 class="hm-title" id="hmTitle"></h1><p class="hm-sub" id="hmSub"></p></header>
        <section class="hm-strip" id="hmStrip" hidden></section>
        <form class="hm-ask" id="hmAsk" autocomplete="off">
          <label class="sr-only" for="hmIn">Ask anything, or start a project</label>
          <textarea class="hm-in" id="hmIn" rows="1" placeholder="Ask anything, or start a project" data-spec="Enter sends. Shift+Enter starts a new line. With no project picked it is a quick ask: nothing is saved unless you save it."></textarea>
          <label class="sr-only" for="hmPick">Project</label>
          <select class="hm-pick" id="hmPick" data-spec="Quick ask keeps nothing. Pick a project to ask inside it: its instructions and files are used and the exchange is saved in a thread there."></select>
          <button type="submit" class="hm-go" id="hmGo" aria-label="Send" data-spec="Sends the question.">${icon('M12 19V5M5 12l7-7 7 7', '', { width: 2 })}</button>
        </form>
        <div class="hm-walk home-walk" id="hmWalk" hidden></div>
        <section class="hm-answer" id="hmAnswer" hidden></section>
        <div class="hm-cards" id="hmCards"></div>
        <p class="hm-privacy hm-muted" id="hmPrivacy" hidden></p>
        <section class="hm-continue" id="hmLeft" hidden><h2 class="hm-h2">Continue where you left off</h2><div class="hm-left" id="hmLeftList"></div></section>
        <section class="hm-continue" id="hmContinue" hidden><h2 class="hm-h2">Continue a project</h2><div class="hm-tiles" id="hmTiles"></div></section>
      </div>`);
    const $ = (id) => root.querySelector('#' + id);
    const titleEl = $('hmTitle');
    const subEl = $('hmSub');
    const form = $('hmAsk');
    const input = $('hmIn');
    const pick = $('hmPick');
    const go = $('hmGo');
    const answerEl = $('hmAnswer');
    const cardsEl = $('hmCards');
    const contEl = $('hmContinue');
    const tilesEl = $('hmTiles');
    const walkEl = $('hmWalk');
    const stripEl = $('hmStrip');
    const privacyEl = $('hmPrivacy');
    const leftEl = $('hmLeft');
    const leftList = $('hmLeftList');
    const thread = ctx.chat;

    /* ---------------- greeting and cards ---------------- */
    function paintHead() {
      const t = st.today;
      titleEl.textContent = greetingLine(t ? t.greeting : null);
      subEl.textContent = t ? subLine(t) : '';
    }

    function needsCard(t) {
      const card = el('section', 'hm-card');
      card.dataset.card = 'needs';
      const head = el('div', 'hm-card-head');
      head.append(el('h2', 'hm-h2', 'Needs you'), el('span', 'hm-count', needsCount(t.needs_total)));
      card.append(head);
      if (!t.needs_you.length) {
        card.append(el('p', 'hm-muted', 'Nothing needs you.'));
        return card;
      }
      t.needs_you.slice(0, 3).forEach((it) => {
        const row = el('div', 'hm-need');
        row.append(el('span', 'hm-dot'));
        const text = el('div', 'hm-need-t');
        text.append(el('div', 'hm-need-title', it.title));
        const since = whenShort(it.since);
        const sub = [it.note, since ? 'since ' + since : ''].filter(Boolean).join(', ');
        if (sub) text.append(el('div', 'hm-muted', sub));
        row.append(text);
        const review = el('a', 'hm-btn', 'Review');
        review.href = '#/' + (it.target || 'goals');
        review.dataset.spec = 'Opens the place where this is waiting for you. It changes nothing.';
        row.append(review);
        card.append(row);
      });
      if (t.needs_total > 3) card.append(el('p', 'hm-muted', '+' + (t.needs_total - 3) + ' more'));
      return card;
    }

    function officeCard(t) {
      const card = el('section', 'hm-card');
      card.dataset.card = 'office';
      const head = el('div', 'hm-card-head');
      const open = el('a', 'hm-link', 'Open office');
      open.href = '#/office';
      head.append(el('h2', 'hm-h2', 'In the office'), open);
      card.append(head);
      const grid = el('div', 'hm-avatars');
      t.office.agents.forEach((a, i) => {
        const av = el('span', 'hm-av hm-av--' + a.status, a.initials);
        av.style.background = avatarCover(i);
        av.title = a.name + ', ' + statusWord(a.status);
        av.setAttribute('role', 'img');
        av.setAttribute('aria-label', a.name + ', ' + statusWord(a.status));
        grid.append(av);
      });
      card.append(grid);
      const line = officeLine(t.office);
      const p = el('p', 'hm-office-line');
      if (line) p.append(el('b', '', line.agent), document.createTextNode(' ' + line.text));
      else p.className = 'hm-muted';
      if (!line) p.textContent = 'No agent is working right now.';
      card.append(p);
      return card;
    }

    /* The Today strip: six cards, each one number, one line and one action, with the age of its data. A card that
       could not be read says so on its own and the others stay. Collapsed state is remembered on this Mac only, through the screen prefs. */
    function collapsed() {
      return ctx.prefs.get('stripCollapsed') === '1';
    }
    function setCollapsed(v) {
      ctx.prefs.set('stripCollapsed', v ? '1' : '0');
    }

    function stripCard(c) {
      const card = el('section', 'hm-sc hm-sc--' + c.state);
      card.dataset.card = c.key;
      card.dataset.state = c.state;
      const head = el('div', 'hm-sc-head');
      head.append(el('h3', 'hm-sc-title', c.title), el('span', 'hm-sc-age', c.state === 'unavailable' ? '' : dataAge(c.at)));
      card.append(head, el('div', 'hm-sc-num', cardNumber(c)), el('p', 'hm-muted hm-sc-line', c.line || ''));
      if (c.action && c.action.target) {
        const a = el('a', 'hm-link', c.action.label || 'Open');
        a.href = '#/' + c.action.target;
        a.dataset.spec = 'Opens ' + c.title + '. It changes nothing.';
        card.append(a);
      }
      return card;
    }

    function paintStrip() {
      const t = st.today;
      if (!t) return;
      const cards = orderedStrip(t.strip);
      stripEl.hidden = !cards.length;
      if (!cards.length) return;
      const isCollapsed = collapsed();
      const head = el('div', 'hm-strip-head');
      const toggle = el('button', 'hm-btn hm-strip-toggle', isCollapsed ? 'Show Today' : 'Hide Today');
      toggle.type = 'button';
      toggle.setAttribute('aria-expanded', String(!isCollapsed));
      toggle.dataset.spec = 'Hides or shows the six Today cards. Remembered on this Mac.';
      toggle.addEventListener('click', () => { setCollapsed(!collapsed()); paintStrip(); });
      head.append(el('h2', 'hm-h2', 'Today'), toggle);
      const body = el('div', 'hm-sc-grid');
      body.hidden = isCollapsed;
      cards.forEach((c) => body.append(stripCard(c)));
      stripEl.replaceChildren(head, body);
    }

    function paintLeft() {
      const items = (st.today && st.today.continue) || [];
      leftEl.hidden = !items.length;
      leftList.replaceChildren(...items.slice(0, 3).map((it) => {
        const row = el('a', 'hm-left-row');
        row.href = '#/' + it.target;
        row.dataset.spec = 'Opens this thread in its project.';
        const text = el('span', 'hm-left-t');
        text.append(el('span', 'hm-need-title', it.title), el('span', 'hm-muted', continueLine(it)));
        row.append(text, el('span', 'hm-muted', whenShort(it.at)));
        return row;
      }));
    }

    function paintPrivacy() {
      const t = st.today;
      const text = t ? privacyText(t.privacy) : '';
      privacyEl.hidden = !text;
      privacyEl.textContent = text;
    }

    function paintCards() {
      const t = st.today;
      if (!t) return;
      cardsEl.replaceChildren(needsCard(t), officeCard(t));
    }

    function paintTiles() {
      const t = st.today;
      if (!t) return;
      st.projects = t.projects;
      contEl.hidden = !t.projects.length;
      tilesEl.replaceChildren(...t.projects.map((p) => {
        const tile = el('button', 'hm-tile');
        tile.type = 'button';
        tile.dataset.project = p.id;
        tile.dataset.spec = 'Picks this project in the ask box, so your next question is asked inside it.';
        const cover = el('span', 'hm-cover');
        cover.style.background = coverVar(p);
        tile.append(cover);
        const body = el('span', 'hm-tile-body');
        body.append(el('span', 'hm-tile-name', p.name), el('span', 'hm-muted', tileLine(p)));
        tile.append(body);
        tile.addEventListener('click', () => {
          pick.value = p.id;
          st.pick = p.id;
          input.focus();
        });
        return tile;
      }));
    }

    function paintPicker() {
      const prev = st.pick;
      pick.replaceChildren();
      const quick = el('option', '', 'Quick ask');
      quick.value = '';
      pick.append(quick);
      st.projects.forEach((p) => {
        const o = el('option', '', p.name);
        o.value = p.id;
        pick.append(o);
      });
      pick.value = st.projects.some((p) => p.id === prev) ? prev : '';
      st.pick = pick.value;
    }

    let loadedOnce = false;
    async function load() {
      try {
        st.today = await ctx.api.get('/api/os/today');
        loadedOnce = true;
        root.dataset.state = 'populated';
        root.classList.remove('hm-stale');
        paintHead();
        paintStrip();
        paintCards();
        paintTiles();
        paintLeft();
        paintPrivacy();
        paintPicker();
      } catch (err) {
        if (isAbort(err)) return;
        if (loadedOnce) {
          root.classList.add('hm-stale');
          return;
        }
        root.dataset.state = 'error';
        titleEl.textContent = greetingLine(null);
        subEl.textContent = 'Could not read today: ' + (err && err.message ? err.message : err);
      }
    }

    /* ---------------- ask ---------------- */
    const projectName = (id) => (st.projects.find((p) => p.id === id) || {}).name || '';

    function paintAnswer() {
      const a = st.ask;
      if (!a) {
        answerEl.hidden = true;
        answerEl.replaceChildren();
        return;
      }
      answerEl.hidden = false;
      const turn = a.turn;
      const box = el('div', 'hm-turn');
      box.append(el('div', 'hm-you', a.text));
      const reply = el('div', 'hm-reply');
      if (turn && turn.reply) setHtml(reply, md(turn.reply));
      else if (turn && turn.status === 'error') reply.append(el('span', 'hm-err', turn.error || 'The request failed.'));
      else reply.append(el('span', 'hm-muted', 'Working'));
      box.append(reply);
      if (turn && turn.status === 'error' && turn.reply) box.append(el('div', 'hm-err', turn.error));
      const aprs = el('div', 'hm-approvals');
      (turn && turn.approvals || []).forEach((id) => {
        const entry = ctx.approvals.get(id);
        if (entry) ctx.approvals.render(aprs, { id: entry.id, prompt: entry.prompt, tool: entry.tool, autonomous: entry.autonomous });
      });
      box.append(aprs);
      const done = turn && turn.status !== 'thinking' && turn.status !== 'generating' && turn.status !== 'working' && turn.status !== 'waiting';
      const bar = el('div', 'hm-actions');
      if (!done) {
        const stop = el('button', 'hm-btn', 'Stop');
        stop.type = 'button';
        stop.addEventListener('click', () => thread.stop());
        bar.append(stop);
      } else {
        if (a.project) {
          bar.append(el('span', 'hm-muted', a.saved === false ? 'Not saved: ' + a.saveError : 'Saved to ' + projectName(a.project)));
        } else if (answered(turn)) {
          bar.append(saveControl(a));
        }
        const fresh = el('button', 'hm-btn', 'New ask');
        fresh.type = 'button';
        fresh.addEventListener('click', () => {
          st.ask = null;
          st.tab = quickTab();
          paintAnswer();
          input.focus();
        });
        bar.append(fresh);
      }
      box.append(bar);
      answerEl.replaceChildren(box);
    }

    /* Save a quick ask into a project on request: a new thread there holding this one exchange. */
    function saveControl(a) {
      const wrap = el('span', 'hm-save');
      if (!st.projects.length) return wrap;
      const sel = el('select', 'hm-pick hm-pick--small');
      sel.setAttribute('aria-label', 'Project to save to');
      st.projects.forEach((p) => {
        const o = el('option', '', p.name);
        o.value = p.id;
        sel.append(o);
      });
      const btn = el('button', 'hm-btn', 'Save to project');
      btn.type = 'button';
      btn.dataset.spec = 'Copies this exchange into a new thread of the project you picked. Nothing else from this ask is kept.';
      btn.addEventListener('click', async () => {
        btn.disabled = true;
        try {
          const th = await ctx.api.post('/api/os/project/thread/new', { id: sel.value, title: threadTitle(a.text) });
          await ctx.api.post('/api/os/project/thread/turn', { id: sel.value, thread: th.thread.id, user: a.text, assistant: a.turn.reply });
          ctx.notify({ level: 'ok', title: 'Saved to ' + projectName(sel.value), ttl: 2500 });
          wrap.replaceChildren(el('span', 'hm-muted', 'Saved to ' + projectName(sel.value)));
        } catch (err) {
          btn.disabled = false;
          ctx.notify({ level: 'error', title: 'Could not save', detail: err && err.message });
        }
      });
      wrap.append(sel, btn);
      return wrap;
    }

    async function send(text) {
      const projectId = st.pick;
      let tab = st.tab;
      let threadId = '';
      if (projectId) {
        try {
          const th = await ctx.api.post('/api/os/project/thread/new', { id: projectId, title: threadTitle(text) });
          threadId = th.thread.id;
          tab = projectTab(projectId, threadId);
        } catch (err) {
          ctx.notify({ level: 'error', title: 'Could not start the thread', detail: err && err.message });
          return;
        }
      }
      st.ask = { text, project: projectId, thread: threadId, turn: null, saved: null, saveError: '' };
      const a = st.ask;
      paintAnswer();
      go.disabled = true;
      const off = thread.subscribe(() => {
        const turns = thread.turns();
        const last = turns[turns.length - 1];
        if (last && last.text === text && st.ask === a) {
          a.turn = last;
          paintAnswer();
        }
      });
      try {
        const turn = await thread.send(text, { tabId: tab });
        if (turn && st.ask === a) {
          a.turn = turn;
          if (projectId && answered(turn)) {
            try {
              await ctx.api.post('/api/os/project/thread/turn', { id: projectId, thread: threadId, user: text, assistant: turn.reply });
              a.saved = true;
            } catch (err) {
              a.saved = false;
              a.saveError = err && err.message ? err.message : 'unknown error';
            }
          } else if (projectId) {
            /* no answer: the empty thread made for it is archived so the project does not collect blank threads */
            a.saved = false;
            a.saveError = 'there was no answer to keep';
            try {
              await ctx.api.post('/api/os/project/thread/archive', { id: projectId, thread: threadId });
            } catch (err) {
              a.saveError += ', and the empty thread could not be archived: ' + (err && err.message ? err.message : 'unknown error');
            }
          }
        }
      } finally {
        off();
        go.disabled = false;
        paintAnswer();
        load();
      }
    }

    form.addEventListener('submit', (e) => {
      e.preventDefault();
      const text = input.value.trim();
      if (!text || go.disabled) return;
      input.value = '';
      grow();
      send(text);
    });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        form.requestSubmit();
      }
    });
    function grow() {
      input.style.height = 'auto';
      input.style.height = Math.min(input.scrollHeight, 160) + 'px';
    }
    input.addEventListener('input', grow);
    pick.addEventListener('change', () => { st.pick = pick.value; });

    /* ---------------- first-run permissions guide ----------------
       Offered once, ever: it is recorded as seen the moment it is shown (in the
       server's settings store), so it never comes back. The guide itself stays
       on the APPS screen. A read that fails or comes from a stale copy offers
       nothing rather than guessing. */
    async function offerWalkthrough() {
      const seen = await readSeen(ctx);
      if (seen !== false || ctx.signal.aborted) return;
      const text = el('span', 'home-walk-t', 'New here? See which macOS permissions Dourmouse uses and what each one lets it do. macOS only asks when a feature needs one.');
      const go = el('a', 'hm-btn', 'Open the permissions guide');
      go.href = '#/apps';
      go.dataset.spec = 'Opens APPS, where each macOS permission Dourmouse uses is explained with a button to its System Settings page. It changes no permission.';
      const later = el('button', 'hm-btn', 'Not now');
      later.type = 'button';
      later.dataset.spec = 'Hides this for good. The guide stays on the APPS screen.';
      later.addEventListener('click', () => { walkEl.hidden = true; });
      walkEl.replaceChildren(text, go, later);
      walkEl.hidden = false;
      const err = await markSeen(ctx);
      if (err) ctx.notify({ level: 'warn', title: 'Permissions guide', detail: 'Could not save that this was shown, so it may appear again: ' + err });
    }

    ctx.every(30000, load);
    ctx.events.onResync(() => load());
    ctx.events.onStatus((s) => { if (s === 'error') root.classList.add('hm-stale'); });
    await load();
    offerWalkthrough();
    /* the shell moves focus to the stage title once a screen has mounted; ask again after that so typing starts at once */
    input.focus();
    setTimeout(() => { if (!ctx.signal.aborted) input.focus(); }, 120);
  },

  async refresh(ctx, reason) {
    if (reason === 'manual') {
      ctx.notify({ level: 'info', title: 'Home', detail: 'Home refreshes itself every 30 seconds.', ttl: 2000 });
    }
  },
};
