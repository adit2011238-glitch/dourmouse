/* The generic flowchart builder behind the mockup's rflow() and sflow()
   (os_mockup.html:369-446). Boxes are data, so a screen passes REAL counts in
   the sub line and never edits SVG by hand. Every string goes through html``,
   so a value from the server cannot break out of the markup.

   column: one box per row, arrows between them, an optional bracket down the
           left edge and a legend on the right (SECURITY).
   snake : boxes fill rows left to right then right to left, with an optional
           dashed backward edge from the last box to the first (RESEARCH). */

import { html, raw } from './html.js';

const TONES = {
  det: ['rgba(52,211,153,.10)', 'var(--dm-ok)'],
  ai: ['color-mix(in srgb, var(--os-warn) 13%, transparent)', 'var(--os-warn)'],
  ui: ['rgba(255,255,255,.05)', 'var(--os-edge-hi)'],
  act: ['rgba(251,113,133,.12)', '#fb7185'],
  built: ['rgba(52,211,153,.10)', 'var(--dm-ok)'],
  todo: ['rgba(255,255,255,.025)', 'var(--os-edge-hi)'],
};

const FONT = 'font-family="var(--dm-font-mono)"';

export function flowSvg({ layout = 'column', boxes, bracket = null, legend = null, back = null, idPrefix = 'fl', label = '' }) {
  return layout === 'snake' ? snake({ boxes, back, idPrefix, label }) : column({ boxes, bracket, legend, idPrefix, label });
}

/* S2: the drawing never shrinks below its own size (so no text renders under 11px); a narrow stage scrolls it sideways */
function scroll(svg) {
  return html`<div class="flowscroll">${svg}</div>`;
}

function marker(id, color) {
  return '<marker id="' + id + '" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><path d="M0 0L7 3.5L0 7z" fill="' + color + '"/></marker>';
}

function column({ boxes, bracket, legend, idPrefix, label }) {
  /* S2: every text size is 11 or more, and the box is wide enough for the longest sub line at that size */
  const bx = 84;
  const bw = 380;
  const bh = 46;
  const sy = 60;
  const y0 = 14;
  const arrow = idPrefix + '-a';
  let shapes = '';
  boxes.forEach((b, i) => {
    const y = y0 + i * sy;
    const [fill, stroke] = TONES[b.tone] || TONES.ui;
    shapes += html`<g data-box="${b.key || i}"><rect x="${bx}" y="${y}" width="${bw}" height="${bh}" rx="9" fill="${raw(fill)}" stroke="${raw(stroke)}" stroke-width="1.3"/><text x="${bx + 14}" y="${y + 19}" fill="var(--dm-fg)" font-size="12.5" ${raw(FONT)}>${b.title}</text><text x="${bx + 14}" y="${y + 37}" fill="var(--dm-fg-body)" font-size="11" ${raw(FONT)}>${b.sub || ''}</text></g>`.text;
    if (i > 0) {
      shapes += '<path d="M' + (bx + bw / 2) + ' ' + (y - sy + bh) + ' L' + (bx + bw / 2) + ' ' + y + '" stroke="var(--os-edge-hi)" stroke-width="1.3" marker-end="url(#' + arrow + ')"/>';
    }
  });
  let extra = '';
  if (bracket) {
    const b0 = y0 + bracket.from * sy;
    const b1 = y0 + bracket.to * sy + bh;
    const mid = (b0 + b1) / 2;
    extra += '<path d="M72 ' + b0 + ' L60 ' + b0 + ' L60 ' + b1 + ' L72 ' + b1 + '" fill="none" stroke="var(--dm-ok)" stroke-width="1.3"/>';
    extra += html`<text x="42" y="${mid}" fill="var(--dm-ok)" font-size="11" ${raw(FONT)} transform="rotate(-90 42 ${mid})" text-anchor="middle">${bracket.label}</text>`.text;
  }
  if (legend) {
    const lx = 490;
    extra += html`<text x="${lx}" y="${y0 + 10}" fill="var(--dm-fg-dim)" font-size="11" ${raw(FONT)}>${legend.title}</text>`.text;
    legend.items.forEach((it, j) => {
      extra += html`<circle cx="${lx + 6}" cy="${y0 + 28 + j * 22}" r="4.5" fill="${raw(it.color)}"/><text x="${lx + 18}" y="${y0 + 32 + j * 22}" fill="var(--dm-fg-body)" font-size="11" ${raw(FONT)}>${it.label}</text>`.text;
    });
  }
  const h = y0 + boxes.length * sy;
  return scroll(html`<svg viewBox="0 0 770 ${h}" class="flow" role="img" aria-label="${label}" style="width:100%;min-width:calc(770px * var(--os-scale, 1));height:auto;margin:6px 0"><defs>${raw(marker(arrow, 'var(--dm-fg-dim)'))}</defs>${raw(shapes)}${raw(extra)}</svg>`);
}

function snake({ boxes, back, idPrefix, label }) {
  const cols = 5;
  const bw = 150;
  const bh = 46;
  const gx = 20;
  const gy = 34;
  const sx = 170;
  const sy = 90;
  const pos = (i) => {
    const r = Math.floor(i / cols);
    let c = i % cols;
    if (r % 2 === 1) c = cols - 1 - c;
    return { x: gx + c * sx, y: gy + r * sy };
  };
  const ah = idPrefix + '-a';
  const aha = idPrefix + '-b';
  let shapes = '';
  let arrows = '';
  boxes.forEach((b, i) => {
    const p = pos(i);
    const [fill, stroke] = TONES[b.tone] || TONES.todo;
    shapes += html`<g data-box="${b.key || i}"><rect x="${p.x}" y="${p.y}" width="${bw}" height="${bh}" rx="9" fill="${raw(fill)}" stroke="${raw(stroke)}" stroke-width="1.2"/><text x="${p.x + bw / 2}" y="${p.y + bh / 2 + 4}" text-anchor="middle" fill="${b.tone === 'built' ? 'var(--dm-fg)' : 'var(--dm-fg-dim)'}" font-size="11.5" ${raw(FONT)}>${b.title}</text></g>`.text;
    if (i > 0) {
      const a = pos(i - 1);
      let d;
      if (a.y === p.y) {
        d = p.x > a.x ? 'M' + (a.x + bw) + ' ' + (a.y + bh / 2) + ' L' + p.x + ' ' + (p.y + bh / 2) : 'M' + a.x + ' ' + (a.y + bh / 2) + ' L' + (p.x + bw) + ' ' + (p.y + bh / 2);
      } else {
        d = 'M' + (a.x + bw / 2) + ' ' + (a.y + bh) + ' L' + (p.x + bw / 2) + ' ' + p.y;
      }
      arrows += '<path d="' + d + '" stroke="var(--os-edge-hi)" stroke-width="1.3" fill="none" marker-end="url(#' + ah + ')"/>';
    }
  });
  const rows = Math.ceil(boxes.length / cols);
  let h = gy + rows * sy;
  let backSvg = '';
  if (back && boxes.length > 1) {
    const last = pos(boxes.length - 1);
    const first = pos(0);
    const lane = gy + (rows - 1) * sy + bh + 22;
    const top = 12;
    const lx = 8;
    backSvg = '<path d="M' + (last.x + bw / 2) + ' ' + (last.y + bh) + ' L' + (last.x + bw / 2) + ' ' + lane + ' L' + lx + ' ' + lane + ' L' + lx + ' ' + top + ' L' + (first.x + bw / 2) + ' ' + top + ' L' + (first.x + bw / 2) + ' ' + first.y + '" fill="none" stroke="var(--dm-active)" stroke-width="1.7" stroke-dasharray="6 4" marker-end="url(#' + aha + ')"/>' +
      html`<text x="${gx + 2.4 * sx}" y="${lane + 16}" text-anchor="middle" fill="var(--dm-active)" font-size="11.5" ${raw(FONT)}>${back.label}</text>`.text;
    h = lane + 26;
  }
  return scroll(html`<svg viewBox="0 0 880 ${h}" class="flow" role="img" aria-label="${label}" style="width:100%;min-width:calc(880px * var(--os-scale, 1));height:auto;margin:8px 0"><defs>${raw(marker(ah, 'var(--dm-fg-dim)'))}${raw(marker(aha, 'var(--dm-active)'))}</defs>${raw(arrows)}${raw(backSvg)}${raw(shapes)}</svg>`);
}
