/* The Ask panel (plan 4.9, PL-04). One input, a mode, a streaming answer, an approval card for low-risk
   actions and a line that sends everything else to the app. Return sends, Tab changes mode, Escape closes
   and returns focus, Cmd Return keeps the exchange in a project and opens it.

   Ask goes the ordinary chat path (POST /api/chat) on a throwaway "quick:" tab, so the same gates apply and
   nothing from it is stored. Note and Remind call their own routes. Do needs Skills, which are not built. */

import { createApi } from '../core/api.js';
import { createApprovals } from '../core/approvals.js';
import { createChat } from '../core/chat.js';
import { createAppearance } from '../core/appearance.js';
import { md } from '../kit/md.js';
import {
  MODES, MODE_LABEL, parseInput, newTab, keepConversation, buildPrompt, approvalHandling, openAppLine, clipLabel,
  whenLine, projectHash,
} from './helpers.js';

const shell = window.dourmouseShell && window.dourmouseShell.ask ? window.dourmouseShell.ask : null;
const $ = (id) => document.getElementById(id);

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function boot() {
  createAppearance().start();
  const api = createApi();
  const inEl = $('akIn');
  const modeEl = $('akMode');
  const chipsEl = $('akChips');
  const bodyEl = $('akBody');
  const sepEl = $('akSep');
  const hintEl = $('akHint');
  const root = $('ak');

  const st = {
    settings: { default_mode: 'ask', keep_minutes: 10 },
    modes: { ask: true, note: true, remind: true, do: false },
    mode: 'ask', /* what the owner chose with Tab; a prefix overrides it while typing */
    tab: newTab(),
    thread: null,
    last: null, /* { text, turn } of the exchange on screen */
    result: null, /* a note or reminder line */
    closedAt: 0,
    clip: null, /* { text, cut, on } */
    projects: null,
    busy: false,
  };

  const scope = { tabId: () => st.tab };
  const approvals = createApprovals({ api, scope });
  const chat = createChat({ api, scope, approvals, threadScreens: [], storage: null });
  const thread = () => chat.thread('ASK');

  /* ---------------- layout ---------------- */
  let lastHeight = 0;
  function fit() {
    const h = Math.ceil(root.getBoundingClientRect().height);
    if (h === lastHeight || !shell) return;
    lastHeight = h;
    shell.resize(h).catch(() => {});
  }
  if (typeof ResizeObserver === 'function') new ResizeObserver(fit).observe(root);

  /* ---------------- mode ---------------- */
  function current() {
    return parseInput(inEl.value, st.mode);
  }
  function paintMode() {
    const { mode } = current();
    modeEl.textContent = MODE_LABEL[mode];
    inEl.placeholder = mode === 'note' ? 'Write a note' : mode === 'remind' ? 'What, and when. Call the bank tomorrow at 9' : mode === 'do' ? 'Name a skill' : 'Ask anything';
  }
  function cycleMode() {
    const usable = MODES.filter((m) => st.modes[m]);
    const at = usable.indexOf(current().mode);
    st.mode = usable[(at + 1) % usable.length];
    /* a prefix would override the chosen mode, so the prefix is dropped when the owner picks one */
    const stripped = current().text;
    inEl.value = stripped;
    paintMode();
    paintChips();
  }

  function clearInput() {
    inEl.value = '';
    inEl.style.height = 'auto';
    paintMode();
    paintChips();
  }

  /* ---------------- context chips (opt in, never read until clicked) ---------------- */
  function paintChips() {
    chipsEl.replaceChildren();
    if (current().mode !== 'ask') return;
    const b = el('button', 'ak-chip', st.clip ? clipLabel(st.clip.text, st.clip.cut) : 'Add clipboard');
    b.type = 'button';
    b.setAttribute('aria-pressed', String(Boolean(st.clip && st.clip.on)));
    b.title = 'Sends what you copied along with the question. Nothing is read until you click.';
    b.addEventListener('click', async () => {
      if (st.clip && st.clip.on) {
        st.clip.on = false;
      } else if (st.clip) {
        st.clip.on = true;
      } else if (shell) {
        const got = await shell.clipboard();
        if (got && got.ok) st.clip = { text: got.text, cut: got.cut, on: true };
        else show(el('p', 'ak-muted', (got && got.error) || 'The clipboard has no text.'));
      }
      paintChips();
      inEl.focus();
    });
    chipsEl.append(b);
  }

  /* ---------------- the body: answer, result or note ---------------- */
  function show(...nodes) {
    bodyEl.replaceChildren(...nodes);
    const any = nodes.length > 0;
    bodyEl.hidden = !any;
    sepEl.hidden = !any;
    fit();
  }

  function approvalNodes(turn) {
    const out = [];
    (turn.approvals || []).forEach((id) => {
      const entry = approvals.get(id);
      if (!entry) return;
      if (entry.state !== 'pending') {
        out.push(el('p', 'ak-muted', (entry.state === 'approved' ? 'Approved: ' : 'Declined: ') + entry.prompt));
        return;
      }
      if (approvalHandling(entry) === 'open-app') {
        const card = el('div', 'ak-card');
        card.append(el('p', 'ak-card-p', entry.prompt), el('p', 'ak-muted', openAppLine()));
        const row = el('div', 'ak-actions');
        const open = el('button', 'ak-btn ak-btn--primary', 'Open the app');
        open.type = 'button';
        open.addEventListener('click', () => shell && shell.openApp('#/home'));
        const no = el('button', 'ak-btn', 'Decline');
        no.type = 'button';
        no.addEventListener('click', () => approvals.decide(entry.id, false));
        row.append(open, no);
        card.append(row);
        out.push(card);
        return;
      }
      const card = el('div', 'ak-card');
      card.setAttribute('role', 'group');
      card.setAttribute('aria-label', 'Approval required');
      card.tabIndex = -1;
      card.append(el('p', 'ak-card-p', entry.prompt));
      if (entry.error) card.append(el('p', 'ak-err', entry.error));
      const row = el('div', 'ak-actions');
      const yes = el('button', 'ak-btn ak-btn--primary', 'Approve');
      yes.type = 'button';
      yes.disabled = entry.busy;
      yes.addEventListener('click', () => approvals.decide(entry.id, true));
      const no = el('button', 'ak-btn', 'Decline');
      no.type = 'button';
      no.disabled = entry.busy;
      no.addEventListener('click', () => approvals.decide(entry.id, false));
      row.append(no, yes);
      card.append(row);
      out.push(card);
    });
    return out;
  }

  function paintTurn() {
    const a = st.last;
    if (!a) return;
    const turn = a.turn;
    const nodes = [el('div', 'ak-you', a.text)];
    const reply = el('div', 'ak-reply');
    if (turn && turn.reply) reply.innerHTML = md(turn.reply);
    if (turn && (turn.status === 'thinking' || turn.status === 'generating' || turn.status === 'working') && !turn.reply) {
      reply.append(el('p', 'ak-muted', turn.status === 'working' ? 'Working' : 'Thinking'));
    }
    nodes.push(reply);
    if (turn && turn.error) nodes.push(el('p', 'ak-err', turn.error));
    if (turn) nodes.push(...approvalNodes(turn));
    const done = turn && ['done', 'error', 'stopped'].includes(turn.status);
    const bar = el('div', 'ak-actions');
    if (!done) {
      const stop = el('button', 'ak-btn', 'Stop');
      stop.type = 'button';
      stop.addEventListener('click', () => thread().stop());
      bar.append(stop);
    } else if (turn.reply) {
      const save = el('button', 'ak-btn', 'Keep in a project');
      save.type = 'button';
      save.title = 'Cmd Return';
      save.addEventListener('click', keepInProject);
      bar.append(save);
    }
    if (bar.childNodes.length) nodes.push(bar);
    show(...nodes);
  }

  function setHint() {
    const done = st.last && st.last.turn && st.last.turn.reply && ['done'].includes(st.last.turn.status);
    hintEl.textContent = done ? 'Return asks again. Cmd Return keeps this in a project. Esc closes.' : 'Return sends. Tab changes mode. Esc closes.';
  }

  /* ---------------- actions ---------------- */
  async function ask(text) {
    const prompt = buildPrompt(text, [st.clip && { ...st.clip, label: 'Clipboard' }]);
    st.last = { text, turn: null };
    if (st.clip) st.clip.on = false; /* context is for one question */
    paintTurn();
    const th = thread();
    const off = th.subscribe(() => {
      const turns = th.turns();
      const t = turns[turns.length - 1];
      if (st.last && t) st.last.turn = t;
      paintTurn();
      setHint();
    });
    st.busy = true;
    try {
      await th.send(prompt, { tabId: st.tab });
    } finally {
      st.busy = false;
      off();
      paintTurn();
      setHint();
      paintChips();
    }
  }

  async function note(text) {
    try {
      const r = await api.post('/api/os/ask/note', { text });
      clearInput();
      show(el('p', 'ak-ok', 'Saved to your notes: ' + r.note.title));
    } catch (err) {
      show(el('p', 'ak-err', err.message));
    }
  }

  async function remind(text) {
    try {
      const r = await api.post('/api/os/ask/remind', { text });
      clearInput();
      show(el('p', 'ak-ok', whenLine(r.reminder)));
    } catch (err) {
      show(el('p', 'ak-err', err.message));
    }
  }

  function send() {
    const { mode, text } = current();
    if (!text) return;
    st.last = null;
    if (mode === 'note') note(text);
    else if (mode === 'remind') remind(text);
    else if (mode === 'do') show(el('p', 'ak-muted', 'Skills are not built yet, so there is nothing to run. Use Ask instead.'));
    else {
      clearInput();
      ask(text);
    }
  }

  /* Cmd Return: the exchange becomes a thread of a project (the first one unless the owner has picked), then the app opens on it. */
  async function keepInProject() {
    const a = st.last;
    if (!a || !a.turn || !a.turn.reply || a.turn.status !== 'done') return;
    try {
      if (!st.projects) st.projects = (await api.get('/api/os/project/list')).projects || [];
      if (!st.projects.length) {
        show(el('p', 'ak-muted', 'There is no project to keep this in yet. Open the app to make one.'));
        return;
      }
      const p = st.projects[0];
      const title = a.text.replace(/\s+/g, ' ').slice(0, 60);
      const th = await api.post('/api/os/project/thread/new', { id: p.id, title });
      await api.post('/api/os/project/thread/turn', { id: p.id, thread: th.thread.id, user: a.text, assistant: a.turn.reply });
      if (shell) await shell.openApp(projectHash(p.id, th.thread.id));
    } catch (err) {
      show(el('p', 'ak-err', 'Could not keep it: ' + err.message));
    }
  }

  /* ---------------- shown again ---------------- */
  function onShow() {
    const hasConversation = Boolean(st.last);
    const keep = keepConversation({ closedAt: st.closedAt, now: Date.now(), keepMinutes: st.settings.keep_minutes, hasConversation });
    if (!keep) {
      thread().clear();
      st.tab = newTab();
      st.last = null;
      st.clip = null;
      clearInput();
      show();
    }
    st.mode = st.settings.default_mode || 'ask';
    paintMode();
    paintChips();
    setHint();
    inEl.focus();
    inEl.select();
    fit();
    loadSettings();
  }

  async function loadSettings() {
    try {
      const r = await api.get('/api/os/ask/settings');
      st.settings = r.settings;
      st.modes = r.modes;
    } catch (err) {
      show(el('p', 'ak-err', 'Settings not read: ' + err.message));
    }
  }

  /* ---------------- keys ---------------- */
  inEl.addEventListener('input', () => {
    inEl.style.height = 'auto';
    inEl.style.height = Math.min(120, inEl.scrollHeight) + 'px';
    paintMode();
    paintChips();
    fit();
  });
  inEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      if (e.metaKey) keepInProject();
      else send();
    } else if (e.key === 'Tab' && !e.shiftKey) {
      e.preventDefault();
      cycleMode();
    }
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      st.closedAt = Date.now();
      if (shell) shell.close();
    }
  });
  modeEl.addEventListener('click', () => {
    cycleMode();
    inEl.focus();
  });
  approvals.onChange(() => {
    paintTurn();
    fit();
  });
  window.addEventListener('blur', () => {
    st.closedAt = Date.now();
  });

  if (shell) shell.onShow(onShow);
  loadSettings().then(() => {
    st.mode = st.settings.default_mode || 'ask';
    paintMode();
    paintChips();
    inEl.focus();
  });
  window.__askPanel = { state: st, send, onShow }; /* read by the headless check, which has no Electron */
}

boot();
