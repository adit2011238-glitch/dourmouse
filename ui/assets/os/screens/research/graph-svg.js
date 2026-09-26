/* The evidence graph as SVG. Nodes and edges come from GET /api/os/research/graph;
   every label goes through html``, so a stored claim cannot break out of the markup. */

import { html, raw } from '../../kit/html.js';
import { layoutGraph, edgeTone, nodeTone, shorten, NODE_W, NODE_H } from './helpers.js';

export function graphSvg(data, label) {
  const g = layoutGraph(data.nodes, data.edges, data.root && data.root.id);
  const lines = g.lines.map((l) => html`<path d="M${l.x1} ${l.y1} L${l.x2} ${l.y2}" stroke="${raw(edgeTone(l.relation))}" stroke-width="1.1" opacity=".6" fill="none"/><text x="${(l.x1 + l.x2) / 2}" y="${(l.y1 + l.y2) / 2 - 2}" text-anchor="middle" fill="${raw(edgeTone(l.relation))}" font-size="8" font-family="var(--dm-font-mono)">${l.relation.replace(/_/g, ' ')}</text>`.text).join('');
  const nodes = g.nodes.map((n) => html`<g data-node="${n.id}"><title>${n.type}: ${n.label}</title><rect x="${n.x - NODE_W / 2}" y="${n.y - NODE_H / 2}" width="${NODE_W}" height="${NODE_H}" rx="6" fill="rgba(255,255,255,.06)" stroke="${raw(nodeTone(n.type))}" stroke-width="1.2"/><text x="${n.x}" y="${n.y + 3.5}" text-anchor="middle" fill="var(--dm-fg)" font-size="8.5" font-family="var(--dm-font-mono)">${shorten(n.label, 17)}</text></g>`.text).join('');
  return html`<svg viewBox="0 0 ${g.width} ${g.height}" class="rgraph" role="img" aria-label="${label}" style="width:100%;height:auto">${raw(lines)}${raw(nodes)}</svg>`;
}
