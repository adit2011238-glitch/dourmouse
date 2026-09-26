/* The approval card: the human gate for a tool that needs confirmation. A port
   of console.html addApproval (2799-2842), including the email-send variant
   that offers only CANCEL (the send is completed by typing "send it").

   The prompt text goes in with textContent: it comes from a model and a tool,
   and this page can approve real actions, so it is never parsed as markup.
   Focus goes to the card itself, not to APPROVE: a stray Enter while typing
   must never approve anything. */

function button(label, cls) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'os-btn ' + (cls || '');
  b.textContent = label;
  return b;
}

/* entry: an approvals.js entry {id, prompt, email, autonomous, state, busy, error}
   onDecide(approved): Promise<boolean>, true when the server recorded it. */
export function approvalCard(entry, onDecide) {
  const box = document.createElement('div');
  box.className = 'approve';
  box.setAttribute('role', 'group');
  box.setAttribute('aria-label', 'Approval required');
  box.tabIndex = -1;
  box.dataset.approvalId = entry.id;

  const title = document.createElement('div');
  title.className = 't';
  const prompt = document.createElement('div');
  prompt.className = 'p';
  prompt.textContent = entry.prompt || 'This action needs your approval.';
  const hint = document.createElement('div');
  hint.className = 'hint';
  const err = document.createElement('div');
  err.className = 'aerr';
  err.setAttribute('role', 'alert');
  const row = document.createElement('div');
  row.className = 'r';
  const yes = entry.email ? null : button('APPROVE', 'os-btn--primary');
  const no = button(entry.email ? 'CANCEL' : 'DECLINE', '');
  if (yes) row.append(yes);
  row.append(no);
  box.append(title, prompt, hint, err, row);

  function paint() {
    box.dataset.state = entry.state;
    box.classList.toggle('done', entry.state !== 'pending');
    if (entry.state === 'approved') title.textContent = 'APPROVED';
    else if (entry.state === 'declined') title.textContent = entry.email ? 'CANCELLED' : 'DECLINED';
    else title.textContent = entry.email ? 'SEND THIS EMAIL?' : 'APPROVAL REQUIRED';
    hint.textContent = entry.email
      ? 'Type "send it" in the box below to send, or cancel.'
      : entry.autonomous && entry.state === 'pending'
        ? 'Approving this lets the project keep going automatically.'
        : '';
    err.textContent = entry.error || '';
    row.hidden = entry.state !== 'pending';
    const off = Boolean(entry.busy);
    if (yes) yes.disabled = off;
    no.disabled = off;
  }
  const decide = (ok) => onDecide(ok);
  if (yes) yes.addEventListener('click', () => decide(true));
  no.addEventListener('click', () => decide(false));
  paint();
  return { el: box, paint, focus: () => box.focus({ preventScroll: false }) };
}
