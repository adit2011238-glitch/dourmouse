/* TerminalWidget: a tool's output as a terminal block. Nothing is invented: the
   command line is the tool call's real arguments, the body is its real result,
   and the status word is "running", "returned" or "failed" (a tool result carries
   no exit code, so none is shown). textContent only. */

import { stepExit } from './helpers.js';

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

const OUT_CAP = 6000;

export function TerminalWidget(step) {
  const status = stepExit(step);
  const box = el('div', 'tw');
  box.dataset.status = status;
  const head = el('div', 'tw-head');
  head.append(el('span', 'tw-name', step.name), el('span', 'tw-status tw-' + status, status));
  const cmd = el('pre', 'tw-cmd');
  cmd.append(el('span', 'tw-prompt', '$ '), document.createTextNode(String(step.args || '').slice(0, 1500)));
  const out = el('pre', 'tw-out');
  out.tabIndex = 0;
  out.setAttribute('aria-label', step.name + ' output');
  const res = String(step.result || '');
  out.textContent = step.running && !res ? 'Waiting for output...' : res.slice(0, OUT_CAP) + (res.length > OUT_CAP ? '\n... ' + (res.length - OUT_CAP) + ' more characters not shown' : '');
  box.append(head, cmd, out);
  return box;
}
