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
  out = out.replace(/\[([^\]\n]{1,200})\]\((https?:\/\/[^\s)]{1,2000})\)/g, (_m, label, url) => {
    return '<a href="' + url + '" data-ext="1" rel="noopener noreferrer">' + label + '</a>';
  });
  return out;
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
    for (const line of lines) {
      const ul = /^\s*[-*]\s+(.*)$/.exec(line);
      const ol = /^\s*\d+[.)]\s+(.*)$/.exec(line);
      const h = /^(#{1,3})\s+(.*)$/.exec(line);
      if (ul || ol) {
        flushPara();
        const tag = ul ? 'ul' : 'ol';
        if (list && list.tag !== tag) flushList();
        if (!list) list = { tag, items: [] };
        list.items.push((ul || ol)[1]);
      } else if (h) {
        flushPara();
        flushList();
        out.push('<div class="md-h">' + inline(esc(h[2])) + '</div>');
      } else if (!line.trim()) {
        flushPara();
        flushList();
      } else {
        flushList();
        para.push(line);
      }
    }
    flushPara();
    flushList();
  };
  while ((m = fence.exec(text)) !== null) {
    blocks(text.slice(last, m.index));
    const lang = m[1] ? '<span class="md-lang">' + esc(m[1]) + '</span>' : '';
    out.push('<pre class="md-code">' + lang + '<code>' + esc(m[2].replace(/\n$/, '')) + '</code></pre>');
    last = m.index + m[0].length;
    if (m[0].length === 0) fence.lastIndex += 1;
  }
  blocks(text.slice(last));
  return raw(out.join(''));
}
