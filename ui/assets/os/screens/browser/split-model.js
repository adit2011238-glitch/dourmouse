/* BROWSER split view: the layout math and the column's fixed words. Pure: no DOM, no storage, so node tests it.

   The screen is split into the real browser on one side (about 80 percent) and a chat column on the other
   (about 20 percent). The native Electron view is composited ABOVE this page's DOM, so the one rule that
   matters is paneClip(): the rectangle the native view may cover is the page's own region and never the
   column. The widths below only decide how wide each side is; the DOM is read for the actual rectangles. */

import { hostOf } from './helpers.js';

export const DEFAULT_RATIO = 0.2; /* the column's share of the screen width */
export const MIN_CHAT_PX = 280; /* never narrower than this ... */
export const MAX_RATIO = 0.45; /* ... and never wider than this share */
export const MIN_RATIO = 0.05;
export const OVERLAY_BELOW = 900; /* under this screen width the column is an overlay, not a side */
export const RAIL_PX = 34; /* the slim button strip left behind when the column is hidden */
export const DIVIDER_PX = 8;
const OVERLAY_MIN = 240;
const OVERLAY_MAX = 380;
const STEP = 20;
const BIG_STEP = 100;

const finite = (n) => typeof n === 'number' && Number.isFinite(n);

/* ---------------- what is remembered ---------------- */
export function parseStoredRatio(v) {
  if (v === null || v === undefined || v === '') return DEFAULT_RATIO;
  const n = Number(v);
  if (!Number.isFinite(n) || n <= 0) return DEFAULT_RATIO;
  return Math.min(MAX_RATIO, Math.max(MIN_RATIO, n));
}

export function parseStoredSide(v) {
  return v === 'left' ? 'left' : 'right';
}

export function parseStoredFlag(v) {
  return v === '1';
}

/* ---------------- width of the column ---------------- */
export function clampChatPx(px, total) {
  const t = finite(total) && total > 0 ? total : 0;
  const max = Math.floor(t * MAX_RATIO);
  const min = Math.min(MIN_CHAT_PX, max);
  if (!finite(px)) return min;
  return Math.max(min, Math.min(max, Math.round(px)));
}

export function chatPxFor(ratio, total) {
  const t = finite(total) && total > 0 ? total : 0;
  return clampChatPx(Math.round(t * (finite(ratio) ? ratio : DEFAULT_RATIO)), t);
}

export function ratioFromPx(px, total) {
  if (!finite(total) || total <= 0) return DEFAULT_RATIO;
  return Math.round((clampChatPx(px, total) / total) * 10000) / 10000;
}

/* ---------------- the layout ---------------- */
function overlayPx(total) {
  const room = Math.max(0, total - RAIL_PX);
  return Math.min(room, Math.max(OVERLAY_MIN, Math.min(OVERLAY_MAX, total - RAIL_PX - 16)));
}

/* state: { total, ratio, collapsed, overlayOpen }
   mode: 'split' (the column beside the page), 'rail' (hidden, a slim strip remains), 'overlay-closed',
   'overlay-open' (under OVERLAY_BELOW the column floats over the page and the strip is its button) */
export function layoutFor(state) {
  const total = finite(state && state.total) && state.total > 0 ? Math.floor(state.total) : 0;
  if (total < OVERLAY_BELOW) {
    const open = total > 0 && Boolean(state && state.overlayOpen);
    return {
      mode: open ? 'overlay-open' : 'overlay-closed',
      chatPx: open ? overlayPx(total) : 0,
      railPx: RAIL_PX,
      dividerPx: 0,
      leftPx: Math.max(0, total - RAIL_PX),
    };
  }
  if (state.collapsed) {
    return { mode: 'rail', chatPx: 0, railPx: RAIL_PX, dividerPx: 0, leftPx: total - RAIL_PX };
  }
  const chatPx = chatPxFor(state.ratio, total);
  return { mode: 'split', chatPx, railPx: 0, dividerPx: DIVIDER_PX, leftPx: total - chatPx - DIVIDER_PX };
}

/* the button that hides or shows the column: it collapses beside the page and floats the overlay under 900 */
export function toggled(state) {
  const total = finite(state && state.total) ? state.total : 0;
  if (total < OVERLAY_BELOW) return { collapsed: Boolean(state.collapsed), overlayOpen: !state.overlayOpen };
  return { collapsed: !state.collapsed, overlayOpen: false };
}

/* something needs the owner (an approval): the column must be visible. null when it already is.
   The saved choice is not rewritten by the caller, so the column goes back to how the owner left it. */
export function forcedOpen(state) {
  const total = finite(state && state.total) ? state.total : 0;
  if (total < OVERLAY_BELOW) return state.overlayOpen ? null : { collapsed: Boolean(state.collapsed), overlayOpen: true };
  return state.collapsed ? { collapsed: false, overlayOpen: false } : null;
}

/* ---------------- the divider ---------------- */
/* pointer: clientX. The divider sits between the page and the column, so half of it belongs to each. */
export function dragPx({ pointer, rectLeft, rectRight, total, side }) {
  const half = DIVIDER_PX / 2;
  const px = side === 'left' ? pointer - rectLeft - half : rectRight - pointer - half;
  return clampChatPx(px, total);
}

/* an arrow key moves the divider the way it points; where the column is, that grows or shrinks it */
export function keyPx({ key, px, total, side, shift }) {
  if (key !== 'ArrowLeft' && key !== 'ArrowRight') return null;
  const step = shift ? BIG_STEP : STEP;
  const towardsLeft = key === 'ArrowLeft';
  const grow = side === 'right' ? towardsLeft : !towardsLeft;
  return clampChatPx(px + (grow ? step : -step), total);
}

/* ---------------- what the native view may cover ---------------- */
/* base: the visible scroll area of the screen. left: the page's own region. overlay: the floating column when
   it is open. Rectangles are { left, top, right, bottom } in viewport pixels. The result is the clip handed to
   viewBounds(); null when the page's region is not laid out, which keeps the view hidden. */
export function paneClip({ base, left, overlay, side }) {
  if (!left) return null;
  const b = base || { left: -Infinity, top: -Infinity, right: Infinity, bottom: Infinity };
  const clip = {
    left: Math.max(b.left, left.left),
    top: Math.max(b.top, left.top),
    right: Math.min(b.right, left.right),
    bottom: Math.min(b.bottom, left.bottom),
  };
  if (overlay) {
    if (side === 'left') clip.left = Math.max(clip.left, overlay.right);
    else clip.right = Math.min(clip.right, overlay.left);
  }
  return clip;
}

/* ---------------- the column's own words ---------------- */
export const STARTERS = [
  {
    label: 'Summarise this page',
    send: 'Summarise the page I have open in the browser in a few short points. Do not click or change anything.',
    spec: 'Asks the AI to read the page in your active tab and summarise it. It does not click or type anything.',
  },
  {
    label: 'Find the price',
    send: 'Find the price on the page I have open in the browser and tell me what it is, with the name of the item. Do not click or change anything.',
    spec: 'Asks the AI to read the page in your active tab and tell you the price. It does not click or type anything.',
  },
  {
    label: "Fill this form (I'll confirm)",
    send: 'Fill in the form on the page I have open in the browser using what you know about me. Ask me before you submit it or click anything that sends or buys something.',
    spec: 'Asks the AI to fill the form in your active tab. It asks you before it submits anything or clicks something risky.',
  },
];

/* what the top of the column says about the tab the AI will work in.
   v: the screen's current page { url, title, loading }. shared: false when this view is not the AI's browser. */
export function contextLine(v, shared) {
  const url = v && typeof v.url === 'string' ? v.url : '';
  const title = v && typeof v.title === 'string' ? v.title.trim() : '';
  const host = hostOf(url);
  if (!url) return { title: 'No page open', host: '', kind: 'none', shared: Boolean(shared) };
  if (v.loading && !title) return { title: host || url, host, kind: 'loading', shared: Boolean(shared) };
  return { title: title || host || url, host, kind: 'page', shared: Boolean(shared) };
}
