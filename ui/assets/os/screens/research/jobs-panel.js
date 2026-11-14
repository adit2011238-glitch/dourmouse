/* RESEARCH JOBS (PL-10 T1): background research runs and the owner's two decisions.

   A job runs in the goals engine, not in this thread: context brief, paper search
   (OpenAlex, Semantic Scholar, arXiv), one graded card per paper, research questions,
   answer rounds, then the answers with citations and references. It stops twice for
   the owner: to check the brief (CONTINUE) and to approve the questions (APPROVE).
   Nothing here approves anything by itself. Every text from the server is placed with
   textContent, never as HTML. Read from GET /api/os/research/jobs and /job?id=. */

import { states } from '../../kit/states.js';
import { confirmHere } from '../../kit/confirm-card.js';
import { isAbort } from '../../core/api.js';
import { safeFilename, shorten } from './helpers.js';

const STATUS_WORD = {
  EXECUTING: 'running', READY: 'running', WAITING_FOR_APPROVAL: 'needs you', COMPLETED: 'done',
  BLOCKED: 'blocked', CANCELLED: 'cancelled', FAILED: 'failed', PAUSED: 'paused', CREATED: 'starting',
};

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
}

function button(label, spec, onClick, primary) {
  const b = el('button', 'os-btn' + (primary ? ' os-btn--primary' : ''), label);
  b.type = 'button';
  b.dataset.spec = spec;
  b.addEventListener('click', onClick);
  return b;
}

export function mountJobsPanel(listEl, detailEl, confirmEl, ctx) {
  const st = { jobs: null, sel: '', job: null, busy: false };
  const guard = () => ctx.signal.aborted;

  async function post(path, body, okTitle) {
    if (st.busy) return false;
    st.busy = true;
    try {
      await ctx.api.post(path, body);
      if (okTitle) ctx.notify({ level: 'ok', title: okTitle, ttl: 2500 });
      await load();
      return true;
    } catch (err) {
      if (!isAbort(err)) ctx.notify({ level: 'error', title: 'Not done', detail: err && err.message });
      return false;
    } finally {
      st.busy = false;
    }
  }

  /* ---------------- start ---------------- */
  function startForm() {
    const form = el('form', 'card rs-new');
    const label = el('label', 'lbl', 'New research job');
    label.setAttribute('for', 'rsJobTopic');
    const input = el('input');
    input.id = 'rsJobTopic';
    input.type = 'text';
    input.maxLength = 300;
    input.placeholder = 'Topic or question to research';
    input.dataset.spec = 'What the job researches. It is searched in OpenAlex, Semantic Scholar and arXiv.';
    const style = el('select');
    style.setAttribute('aria-label', 'Citation style');
    style.dataset.spec = 'Citation style of the answers and the reference list.';
    [['apa', 'APA'], ['mla', 'MLA']].forEach(([v, t]) => {
      const o = el('option', '', t);
      o.value = v;
      style.append(o);
    });
    const pauseWrap = el('label', 'muted rs-jobpause');
    const pause = el('input');
    pause.type = 'checkbox';
    pause.checked = true;
    pause.dataset.spec = 'When ticked, the job stops after the context brief so you can check and correct it.';
    pauseWrap.append(pause, document.createTextNode(' Stop so I can check the brief'));
    const go = el('button', 'os-btn os-btn--primary', 'CONTINUE');
    go.type = 'submit';
    go.dataset.spec = 'Shows what will start and waits for your approval before anything starts.';
    const cancel = button('CANCEL', 'Closes this form without starting anything.', () => close());
    const row = el('div', 'rs-newrow');
    row.append(input, style, go, cancel);
    form.append(label, row, pauseWrap);
    const off = ctx.keys.pushEsc(() => close());
    const close = () => {
      off();
      confirmEl.replaceChildren();
    };
    form.addEventListener('submit', (ev) => {
      ev.preventDefault();
      const topic = input.value.trim();
      if (!topic) {
        input.focus();
        return;
      }
      off();
      confirmHere(confirmEl, 'Start a background research job on "' + shorten(topic, 160) + '"? It searches OpenAlex, Semantic Scholar and arXiv, reads open-access PDFs and uses model credit. It stops ' + (pause.checked ? 'twice' : 'once') + ' for you: ' + (pause.checked ? 'to check the brief, then ' : '') + 'to approve the research questions.', async () => {
        const r = await ctx.api.post('/api/os/research/jobs/start', { topic, style: style.value, pause_after_brief: pause.checked });
        st.sel = r.id;
        await load();
      }, { onDone: () => setTimeout(() => confirmEl.replaceChildren(), 1500) });
    });
    confirmEl.replaceChildren(form);
    input.focus();
  }

  /* ---------------- list ---------------- */
  function paintList() {
    const jobs = (st.jobs || []);
    if (!jobs.length) {
      states.empty(listEl, 'No research jobs yet.', { hint: 'Press NEW RESEARCH JOB to start one in the background.' });
      return;
    }
    const wrap = el('div', 'rs-qs');
    jobs.forEach((j) => {
      const row = el('button', 'os-row rs-q');
      row.type = 'button';
      row.dataset.spec = 'Shows this job: its progress, what it needs from you, its paper cards and its answers.';
      row.setAttribute('aria-pressed', String(j.id === st.sel));
      row.append(el('span', 'rt', j.topic));
      const word = STATUS_WORD[j.status] || j.status.toLowerCase();
      row.append(el('span', 'tag' + (j.status === 'WAITING_FOR_APPROVAL' || j.status === 'BLOCKED' ? ' warn' : ''), word));
      row.append(el('span', 'muted', j.cards + (j.cards === 1 ? ' paper card' : ' paper cards')));
      row.addEventListener('click', () => {
        st.sel = j.id;
        paintList();
        loadJob();
      });
      wrap.append(row);
    });
    states.populated(listEl, wrap);
  }

  /* ---------------- one job ---------------- */
  function section(title) {
    const s = el('div', 'rs-jobsec');
    s.append(el('div', 'lbl', title));
    return s;
  }

  function briefBox(job) {
    const s = section(job.waiting_for === 'brief' ? 'Needs you: check the context brief' : 'Context brief');
    s.append(el('pre', 'rs-brief', job.brief.markdown));
    if (job.brief.rejected && job.brief.rejected.length) {
      s.append(el('div', 'muted', job.brief.rejected.length + ' brief item(s) were dropped because their quote was not in the named source.'));
    }
    if (job.waiting_for !== 'brief') return s;
    const notes = el('textarea', 'rs-notes');
    notes.rows = 3;
    notes.maxLength = 4000;
    notes.placeholder = 'Corrections (optional). They travel with the brief and override it.';
    notes.value = job.brief.owner_notes || '';
    notes.setAttribute('aria-label', 'Corrections to the brief');
    const row = el('div', 'rs-newrow');
    row.append(
      button('SAVE CORRECTIONS', 'Saves your corrections to the brief. Nothing else happens.', () => post('/api/os/research/job/brief', { id: job.id, owner_notes: notes.value }, 'Corrections saved')),
      button('CONTINUE', 'Saves your corrections, then lets the job search for and read papers.', async () => {
        if (await post('/api/os/research/job/brief', { id: job.id, owner_notes: notes.value })) post('/api/os/research/job/continue', { id: job.id }, 'Paper search started');
      }, true),
    );
    s.append(notes, row);
    return s;
  }

  function questionsBox(job) {
    const q = job.questions;
    const waiting = job.waiting_for === 'questions';
    const s = section(waiting ? 'Needs you: approve the research questions' : 'Research questions' + (q.status === 'approved' ? ' (approved)' : ''));
    if (q.note) s.append(el('div', 'muted', q.note));
    if (!waiting) {
      s.append(el('div', 'rt', q.question));
      const ul = el('ul', 'rs-subq');
      q.sub_questions.forEach((x) => ul.append(el('li', '', x.text)));
      s.append(ul);
      return s;
    }
    const qIn = el('input');
    qIn.type = 'text';
    qIn.maxLength = 400;
    qIn.value = q.question;
    qIn.setAttribute('aria-label', 'Research question');
    const goals = el('textarea', 'rs-notes');
    goals.rows = 2;
    goals.value = (q.goals || []).join('\n');
    goals.setAttribute('aria-label', 'Goals, one per line');
    const subs = el('textarea', 'rs-notes');
    subs.rows = 6;
    subs.value = q.sub_questions.map((x) => x.text).join('\n');
    subs.setAttribute('aria-label', 'Sub-questions, one per line');
    const lines = (t) => t.value.split('\n').map((x) => x.trim()).filter(Boolean);
    const payload = () => ({ id: job.id, question: qIn.value.trim(), goals: lines(goals), sub_questions: lines(subs) });
    const why = el('ul', 'rs-subq muted');
    q.sub_questions.forEach((x) => { if (x.why) why.append(el('li', '', x.text + ': ' + x.why)); });
    const row = el('div', 'rs-newrow');
    row.append(
      button('SAVE EDITS', 'Saves your version of the question, goals (one per line) and sub-questions (one per line). Nothing runs.', () => post('/api/os/research/job/questions', payload(), 'Questions saved')),
      button('APPROVE', 'Saves your version, then lets the job answer these sub-questions with cited evidence.', async () => {
        if (await post('/api/os/research/job/questions', payload())) post('/api/os/research/job/approve', { id: job.id }, 'Answer rounds started');
      }, true),
    );
    s.append(el('label', 'muted', 'Research question'), qIn, el('label', 'muted', 'Goals, one per line'), goals,
      el('label', 'muted', 'Sub-questions, one per line (1 to 6)'), subs, why, row);
    return s;
  }

  function cardsBox(job) {
    const s = section('Papers read (' + job.cards.length + ')');
    job.cards.forEach((c) => {
      const d = el('details', 'rs-card');
      const sum = el('summary');
      sum.append(el('span', 'tag', c.grade.letter + ' ' + c.grade.score), document.createTextNode(' '),
        el('span', 'rt', c.paper.title), el('span', 'muted', ' (' + (c.paper.year || 'n.d.') + ', ' + (c.read_from === 'pdf' ? 'full text' : c.read_from === 'abstract' ? 'abstract only' : 'not readable') + ')'));
      d.append(sum);
      [['Question', c.question], ['Method', c.method], ['Data', c.data], ['Bearing (assessment)', c.bearing]].forEach(([k, v]) => {
        if (v) d.append(el('div', '', k + ': ' + v));
      });
      c.findings.forEach((f) => {
        const p = el('div', 'rs-finding');
        p.append(el('div', '', f.text), el('blockquote', 'muted', '"' + f.quote + '"' + (f.location ? ' (' + f.location + ')' : '')));
        d.append(p);
      });
      c.limitations.forEach((x) => d.append(el('div', 'muted', 'Limitation: ' + x)));
      const g = el('ul', 'rs-subq muted');
      c.grade.reasons.forEach((r) => g.append(el('li', '', r)));
      d.append(el('div', 'lbl', 'Grade'), g);
      if (c.rejected.length) d.append(el('div', 'muted', c.rejected.length + ' field(s) dropped: their quote was not in the paper.'));
      if (/^https?:\/\//i.test(c.paper.url || '')) {
        const a = el('a', '', c.paper.url);
        a.href = c.paper.url;
        a.addEventListener('click', (ev) => {
          ev.preventDefault();
          ctx.host.openExternal(c.paper.url);
        });
        d.append(a);
      }
      s.append(d);
    });
    return s;
  }

  function answersBox(job) {
    const r = job.report;
    const s = section('Answers');
    s.append(el('div', 'rt', r.question));
    r.answers.forEach((a, i) => {
      const box = el('div', 'rs-answer');
      box.append(el('div', 'lbl', (i + 1) + '. ' + a.sub_question));
      if (a.sentences.length) {
        a.sentences.forEach((x) => box.append(el('p', '', x.text + (x.citation ? ' ' + x.citation : ''))));
      } else {
        box.append(el('p', 'muted', 'Not answered: the evidence found does not address this sub-question.'));
      }
      box.append(el('span', 'tag' + (a.confidence === 'low' || a.confidence === 'none' ? ' warn' : ''), 'confidence: ' + a.confidence), el('span', 'muted', ' ' + a.confidence_reason));
      (a.numbers || []).forEach((n) => {
        const stt = n.statistics || {};
        if (stt.error) return;
        box.append(el('div', 'muted', n.quantity + ': ' + n.values.map((v) => v.as_written).join(', ') + '; mean ' + Number(stt.mean).toPrecision(3) + ', 95% CI ' + Number(stt.ci95[0]).toPrecision(3) + ' to ' + Number(stt.ci95[1]).toPrecision(3) + ' (' + stt.method + ')'));
      });
      s.append(box);
    });
    const dis = section('Where sources disagree');
    if (r.disagreements.length) {
      r.disagreements.forEach((d) => dis.append(el('p', '', d.sub_question + ': "' + d.a.quote + '" (' + d.a.cite + ') versus "' + d.b.quote + '" (' + d.b.cite + '). ' + d.note)));
    } else {
      dis.append(el('p', 'muted', 'No disagreement was found among the cited findings.'));
    }
    const refs = section('References (' + (r.style === 'mla' ? 'MLA' : 'APA') + ')');
    r.references.forEach((x) => refs.append(el('p', 'rs-ref', x.replace(/\*/g, ''))));
    const how = el('div', 'muted', 'Stopped: ' + r.stop_reason + '. Rounds: ' + r.rounds + '. Papers read: ' + r.papers_read + ' (' + r.abstract_only + ' from the abstract only).');
    const exp = el('div', 'rs-newrow');
    const style = el('select');
    style.setAttribute('aria-label', 'Export citation style');
    [['apa', 'APA'], ['mla', 'MLA']].forEach(([v, t]) => {
      const o = el('option', '', t);
      o.value = v;
      if (v === r.style) o.selected = true;
      style.append(o);
    });
    exp.append(style, button('EXPORT', 'Saves the answers, disagreements, references and the quote behind every claim as a markdown file.', () => exportJob(job.id, style.value)));
    const out = el('div');
    out.append(s, dis, refs, how, exp);
    return out;
  }

  async function exportJob(id, style) {
    try {
      const r = await ctx.api.get('/api/os/research/job/export?id=' + encodeURIComponent(id) + '&format=markdown&style=' + encodeURIComponent(style));
      const blob = new Blob([r.content], { type: (r.mime || 'text/markdown') + ';charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = safeFilename(r.filename);
      a.rel = 'noopener';
      detailEl.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 4000);
      ctx.notify({ level: 'ok', title: 'Exported', detail: safeFilename(r.filename), ttl: 3000 });
    } catch (err) {
      if (!isAbort(err)) ctx.notify({ level: 'error', title: 'Could not export', detail: err && err.message });
    }
  }

  function paintJob() {
    const job = st.job;
    if (!job) {
      detailEl.replaceChildren();
      return;
    }
    const wrap = el('div', 'rs-job');
    const head = el('div', 'rs-jobhead');
    head.append(el('span', 'rt', job.topic), el('span', 'tag' + (job.waiting_for ? ' warn' : ''), STATUS_WORD[job.status] || job.status.toLowerCase()));
    wrap.append(head);
    if (job.status === 'BLOCKED' && job.blocked_reason) wrap.append(el('div', 'muted', 'Blocked: ' + job.blocked_reason));
    const steps = el('ol', 'rs-steps muted');
    job.steps.forEach((x) => steps.append(el('li', '', x.description + ': ' + x.status.toLowerCase().replace(/_/g, ' ') + (x.error && x.status !== 'COMPLETED' ? ' (' + shorten(x.error, 140) + ')' : ''))));
    wrap.append(steps);
    const sp = job.spend || {};
    wrap.append(el('div', 'muted', 'Papers read ' + (sp.papers_read || 0) + ' of at most ' + ((job.budget || {}).max_papers || '?') + '; model calls ' + (sp.model_calls || 0) + ' of at most ' + ((job.budget || {}).max_model_calls || '?') + '.'));
    if (job.brief) wrap.append(briefBox(job));
    if (job.questions) wrap.append(questionsBox(job));
    if (job.report) wrap.append(answersBox(job));
    if (job.cards.length) wrap.append(cardsBox(job));
    const log = el('details', 'rs-log');
    log.append(el('summary', 'lbl', 'Log'));
    job.log.slice().reverse().forEach((line) => log.append(el('div', 'muted', line)));
    wrap.append(log);
    detailEl.replaceChildren(wrap);
  }

  async function loadJob() {
    if (!st.sel) {
      st.job = null;
      paintJob();
      return;
    }
    try {
      const d = await ctx.api.get('/api/os/research/job?id=' + encodeURIComponent(st.sel));
      if (guard()) return;
      /* never repaint over an edit in progress (typing in the brief or question boxes) */
      const editing = detailEl.contains(document.activeElement) && /^(TEXTAREA|INPUT|SELECT)$/.test(document.activeElement.tagName);
      st.job = d;
      if (!editing) paintJob();
    } catch (err) {
      if (!isAbort(err)) states.stale(detailEl, 'Could not read this job: ' + err.message);
    }
  }

  async function load() {
    try {
      const d = await ctx.api.get('/api/os/research/jobs');
      if (guard()) return;
      st.jobs = d.jobs || [];
      if (!st.jobs.some((j) => j.id === st.sel)) st.sel = st.jobs.length ? st.jobs[0].id : '';
      paintList();
      await loadJob();
    } catch (err) {
      if (isAbort(err)) return;
      if (st.jobs) states.stale(listEl, 'Could not refresh: ' + err.message);
      else states.error(listEl, err, { title: 'Could not read the research jobs', retry: () => load() });
    }
  }

  /* a running job is re-read every 5 seconds; a job waiting for you or finished is not */
  ctx.every(5000, () => {
    if (st.job && ['EXECUTING', 'READY', 'CREATED', 'VERIFYING'].includes(st.job.status)) load();
  });
  states.loading(listEl, 'Reading research jobs');
  return { load, startForm };
}
