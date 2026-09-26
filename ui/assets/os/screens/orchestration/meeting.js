/* Renders one meeting (GET /api/office_log?meeting=<run>) as a conversation.
   Every string in it came from a model or a tool, so it only goes in through
   the html template, which escapes it. */

import { html } from '../../kit/html.js';
import { clock } from '../../kit/format.js';

export const LINE_CAP = 300;
const KIND_WORD = { task: 'task', says: 'says', thinks: 'thinks', uses: 'uses', gets: 'result', done: 'done', brain: 'model' };

export function meetingHtml(lines, truncated) {
  if (!lines.length) return html`<div class="muted">This run left no recorded lines yet.</div>`;
  const shown = lines.slice(0, LINE_CAP);
  return html`<div class="mt-lines">${shown.map((l) => html`<div class="mt-line" data-kind="${l.kind}">
      <span class="mt-t">${clock(l.ts)}</span><span class="mt-a">${l.agent}</span><span class="mt-k">${KIND_WORD[l.kind] || l.kind}</span><span class="mt-x">${String(l.text || '').slice(0, 1500)}</span></div>`)}
    ${lines.length > LINE_CAP || truncated ? html`<div class="muted">Showing the first ${LINE_CAP} lines.</div>` : ''}</div>`;
}
