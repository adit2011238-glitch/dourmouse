/* A page-level confirmation: the owner's own click, not the model's approval
   gate. Use it for a change the owner asks for from a screen (pause a goal,
   edit a routine, dismiss a lockdown). The card says exactly what will
   happen, nothing runs until APPROVE is pressed, and a failure is shown in
   the card in the server's own words. Changes the model asks for ride
   ctx.approvals instead.

     confirmHere(containerEl, 'Pause goal "X"? Its running task finishes, then nothing further starts.',
                 () => ctx.api.post('/api/os/goals/pause', { id }));

   Focus goes to the card, never to APPROVE (approval-card.js). */

import { approvalCard } from './approval-card.js';

let seq = 0;

/* run: async function, called only after APPROVE. Returns the card handle
   { el, paint, focus, entry, dismiss }. */
export function confirmHere(container, prompt, run, { onDone = null } = {}) {
  seq += 1;
  const entry = { id: 'local-' + seq, prompt, email: false, autonomous: false, state: 'pending', busy: false, error: '' };
  const card = approvalCard(entry, async (ok) => {
    if (!ok) {
      entry.state = 'declined';
      card.paint();
      if (onDone) onDone(false);
      return true;
    }
    entry.busy = true;
    entry.error = '';
    card.paint();
    try {
      await run();
      entry.state = 'approved';
    } catch (err) {
      entry.error = err && err.message ? err.message : String(err);
    }
    entry.busy = false;
    card.paint();
    if (onDone && entry.state === 'approved') onDone(true);
    return true;
  });
  container.replaceChildren(card.el);
  card.focus();
  return { ...card, entry, dismiss: () => card.el.remove() };
}
