/* A small, safe markdown renderer for model replies. The text is escaped
   FIRST and only then turned into a fixed set of tags, so a reply can never
   inject markup. Links open through the host (data-ext), never target=_blank:
   the Electron main window has no window-open handler (pitfall 5). */

import { esc, raw } from './html.js';

function inline(escaped) {
  let out = escaped;
  /* Every quantifier is bounded: an unbounded [^\]\n]+ rescans to the end of
     the line from each '[' and is quadratic on a long line of brackets. */
  out = out.replace(/`([^`\n]{1,500})`/g, (_m, code) => '<code>' + code + '</code>');
  out = out.replace(/\*\*([^*\n]{1,500})\*\*/g, '<b>$1</b>');
  out = out.replace(/\*(?=\S)([^*\n]{1,300}?)(?<=\S)\*/g, '<i>$1</i>');
  out = out.replace(/\[([^\]\n]{1,200})\]\((https?:\/\/[^\s)]{1,2000})\)/g, (_m, label, url) => {
    return '<a href="' + url + '" data-ext="1" rel="noopener noreferrer">' + label + '</a>';
  });
  return out;
}

const MAX_COLS = 20;
const MAX_ROWS = 300;
const MAX_ROW_CHARS = 4000;

function splitRow(line) {
  let t = line.trim();
  if (t.startsWith('|')) t = t.slice(1);
  if (t.endsWith('|')) t = t.slice(0, -1);
  return t.split('|', MAX_COLS).map((c) => c.trim());
}

/* a pipe row: bounded length so a 200 KB line of pipes is never split */
function isRow(line) {
  return line.length <= MAX_ROW_CHARS && line.indexOf('|') !== -1;
}

function isDivider(line) {
  if (line.length > MAX_ROW_CHARS || line.indexOf('-') === -1) return false;
  return splitRow(line).every((c) => /^:?-{1,40}:?$/.test(c));
}

function align(cell) {
  const l = cell.startsWith(':');
  const r = cell.endsWith(':');
  return l && r ? 'center' : r ? 'right' : '';
}

export function md(source) {
  const text = String(source ?? '').replace(/\r\n/g, '\n');
  const out = [];
  const fence = /```([A-Za-z0-9_+-]*)\n([\s\S]*?)(?:```|$)/g;
  let last = 0;
  let m;
  const blocks = (chunk) => {
    const lines = chunk.split('\n');
    let para = [];
    let list = null;
    let quote = null;
    const flushPara = () => {
      if (para.length) {
        out.push('<p>' + para.map((l) => inline(esc(l))).join('<br>') + '</p>');
        para = [];
      }
    };
    const flushList = () => {
      if (list) {
        out.push('<' + list.tag + '>' + list.items.map((i) => '<li>' + inline(esc(i)) + '</li>').join('') + '</' + list.tag + '>');
        list = null;
      }
    };
    const flushQuote = () => {
      if (quote) {
        out.push('<blockquote>' + quote.map((l) => inline(esc(l))).join('<br>') + '</blockquote>');
        quote = null;
      }
    };
    const flushAll = () => {
      flushPara();
      flushList();
      flushQuote();
    };
    for (let i = 0; i < lines.length; i += 1) {
      const line = lines[i];
      const ul = /^\s{0,6}[-*]\s+(.{0,4000})$/.exec(line);
      const ol = /^\s{0,6}\d{1,4}[.)]\s+(.{0,4000})$/.exec(line);
      const h = /^(#{1,3})\s+(.{0,500})$/.exec(line);
      const q = /^\s{0,3}>\s?(.{0,4000})$/.exec(line);
      if (isRow(line) && i + 1 < lines.length && isDivider(lines[i + 1])) {
        flushAll();
        const head = splitRow(line);
        const aligns = splitRow(lines[i + 1]).map(align);
        const rows = [];
        let j = i + 2;
        while (j < lines.length && rows.length < MAX_ROWS && lines[j].trim() && isRow(lines[j])) {
          rows.push(splitRow(lines[j]));
          j += 1;
        }
        const cell = (tag, c, k) => '<' + tag + (aligns[k] ? ' class="md-al-' + aligns[k] + '"' : '') + '>' + inline(esc(c || '')) + '</' + tag + '>';
        out.push('<div class="md-tablewrap"><table class="md-table"><thead><tr>' + head.map((c, k) => cell('th', c, k)).join('') + '</tr></thead><tbody>' +
          rows.map((r) => '<tr>' + head.map((_c, k) => cell('td', r[k], k)).join('') + '</tr>').join('') + '</tbody></table></div>');
        i = j - 1;
      } else if (q) {
        flushPara();
        flushList();
        if (!quote) quote = [];
        quote.push(q[1]);
      } else if (ul || ol) {
        flushPara();
        flushQuote();
        const tag = ul ? 'ul' : 'ol';
        if (list && list.tag !== tag) flushList();
        if (!list) list = { tag, items: [] };
        list.items.push((ul || ol)[1]);
      } else if (h) {
        flushAll();
        out.push('<div class="md-h md-h' + h[1].length + '">' + inline(esc(h[2])) + '</div>');
      } else if (/^\s{0,3}(?:-{3,40}|_{3,40}|\*{3,40})\s*$/.test(line)) {
        flushAll();
        out.push('<hr>');
      } else if (!line.trim()) {
        flushAll();
      } else {
        flushList();
        flushQuote();
        para.push(line);
      }
    }
    flushAll();
  };
  while ((m = fence.exec(text)) !== null) {
    blocks(text.slice(last, m.index));
    const lang = m[1] ? '<span class="md-lang">' + esc(m[1]) + '</span>' : '<span class="md-lang">code</span>';
    out.push('<div class="md-codeblock"><div class="md-codebar">' + lang + '<button type="button" class="md-copy" data-md-copy="1" aria-label="Copy this code block">COPY</button></div><pre class="md-code"><code>' + esc(m[2].replace(/\n$/, '')) + '</code></pre></div>');
    last = m.index + m[0].length;
    if (m[0].length === 0) fence.lastIndex += 1;
  }
  blocks(text.slice(last));
  return raw(out.join(''));
}

/* One delegated click handler for the COPY button on every code block inside
   `container`. notify(level, title, detail) reports the result. Returns the
   remover. The clipboard write is the only side effect. */
export function wireCodeCopy(container, notify) {
  const onClick = (e) => {
    const b = e.target && e.target.closest ? e.target.closest('button[data-md-copy]') : null;
    if (!b || !container.contains(b)) return;
    const code = b.closest('.md-codeblock');
    const text = code ? (code.querySelector('code') || {}).textContent || '' : '';
    const write = navigator.clipboard && navigator.clipboard.writeText ? navigator.clipboard.writeText(text) : Promise.reject(new Error('This browser does not allow clipboard access here.'));
    write.then(
      () => {
        b.textContent = 'COPIED';
        setTimeout(() => { b.textContent = 'COPY'; }, 1600);
        notify('ok', 'Copied', 'The code block is on the clipboard.');
      },
      (err) => notify('warn', 'Could not copy', err && err.message),
    );
  };
  container.addEventListener('click', onClick);
  return () => container.removeEventListener('click', onClick);
}
