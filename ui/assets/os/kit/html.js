/* Escaping tagged template. Everything that came from the server, an email, a
   web page, a news feed, a file name or a model reply goes through esc().
   The console's own esc() covers five characters; the mockup's covered two,
   which is unsafe inside an attribute. Always quote attribute values. */

const MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

export function esc(value) {
  if (value === null || value === undefined) return '';
  return String(value).replace(/[&<>"']/g, (c) => MAP[c]);
}

/* A string that is already safe markup. Only html`` and raw() make one. */
export class SafeHtml {
  constructor(text) {
    this.text = text;
  }
  toString() {
    return this.text;
  }
}

/* Trusted, static markup only (an icon path table, a fixed snippet). */
export function raw(text) {
  return new SafeHtml(String(text));
}

function part(value) {
  if (value instanceof SafeHtml) return value.text;
  if (Array.isArray(value)) return value.map(part).join('');
  if (value === null || value === undefined || value === false) return '';
  return esc(value);
}

export function html(strings, ...values) {
  let out = strings[0];
  for (let i = 0; i < values.length; i += 1) {
    out += part(values[i]) + strings[i + 1];
  }
  return new SafeHtml(out);
}

/* The only place markup becomes DOM. Screens call these, they never touch
   innerHTML themselves (checked by test_os_screen_contract.py). */
export function toFragment(safe) {
  const tpl = document.createElement('template');
  tpl.innerHTML = safe instanceof SafeHtml ? safe.text : esc(safe);
  return tpl.content;
}

export function setHtml(el, safe) {
  el.replaceChildren(toFragment(safe));
  return el;
}

export function appendHtml(el, safe) {
  el.append(toFragment(safe));
  return el;
}

/* One element from a snippet with a single root. */
export function toElement(safe) {
  const frag = toFragment(safe);
  return frag.firstElementChild;
}
