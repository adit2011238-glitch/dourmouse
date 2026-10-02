/* Pure helpers for MEDIA. No DOM at import time, so node can test them. */

/* 68.4 -> "1:08". Anything that is not a finite, non-negative number gives '' so
   the screen never shows a made-up time. */
export function clock(sec) {
  if (typeof sec !== 'number' || !Number.isFinite(sec) || sec < 0) return '';
  const s = Math.floor(sec);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  const two = (n) => String(n).padStart(2, '0');
  return h ? h + ':' + two(m) + ':' + two(r) : m + ':' + two(r);
}

export function sizeLabel(bytes) {
  if (typeof bytes !== 'number' || !Number.isFinite(bytes) || bytes < 0) return '';
  if (bytes < 1024) return bytes + ' B';
  const units = ['KB', 'MB', 'GB', 'TB'];
  let v = bytes / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return (v >= 100 ? v.toFixed(0) : v.toFixed(1)) + ' ' + units[i];
}

/* The info line: the file name, then only the facts that were really read.
   `facts` = { duration, width, height, video:[codec], audio:[codec], size }.
   The media element's own numbers win over the probe's. */
export function infoLine(row, facts = {}) {
  if (!row) return '';
  const parts = [row.name];
  const dur = clock(facts.duration);
  if (dur) parts.push(dur);
  const codec = (facts.video && facts.video[0]) || (facts.audio && facts.audio[0]) || '';
  if (codec) parts.push(String(codec).toUpperCase());
  if (facts.width > 0 && facts.height > 0) parts.push(facts.width + 'x' + facts.height);
  const size = sizeLabel(row.size);
  if (size) parts.push(size);
  return parts.join(' · ');
}

/* Fraction 0..1 for the scrubber fill; 0 when the duration is unknown. */
export function fraction(cur, dur) {
  if (!(dur > 0) || !(cur >= 0)) return 0;
  return Math.min(1, cur / dur);
}

/* MediaError.code -> plain words. */
export function mediaErrorText(code) {
  return ({
    1: 'Playback was stopped.',
    2: 'The network failed while reading the file.',
    3: 'The file was found but this window could not decode it.',
    4: 'This window cannot play that file or the server could not serve it.',
  })[code] || 'This window could not play the file.';
}

/* The text of the formats cards, from the server's own lists. */
export function formatText(list) {
  return Array.isArray(list) && list.length ? list.join(' ') : 'none';
}

/* Converting progress from /api/files/media-status. */
export function convertLabel(job) {
  if (!job) return 'Preparing the file for playback.';
  const pct = typeof job.progress === 'number' && Number.isFinite(job.progress) ? Math.round(job.progress * 100) : null;
  const how = job.route === 'transcode' ? 'Converting' : job.route === 'remux' ? 'Repackaging' : 'Preparing';
  return how + ' the file for playback' + (pct === null ? '.' : ': ' + pct + '%.');
}

export const CONVERT_POLL_MS = 1500;
export const CONVERT_POLL_MAX = 400; /* about ten minutes, then it stops asking */

/* ---------------- queue ---------------- */

/* The index of the queue row `step` away from `path` (+1 next, -1 previous),
   or -1 when there is none. No wrap-around: the end of the queue is the end.
   A file that is not in the queue has no previous, and its next is the first
   queued row, so "next" always means something while a queue exists. */
export function neighbour(rows, path, step) {
  if (!Array.isArray(rows) || !rows.length) return -1;
  const at = rows.findIndex((r) => r && r.path === path);
  if (at < 0) return step > 0 ? 0 : -1;
  const to = at + step;
  return to >= 0 && to < rows.length ? to : -1;
}

/* Where a row at `index` lands when moved one place up (-1) or down (+1); -1
   when it is already at that end. */
export function reorderTarget(index, dir, length) {
  const to = index + dir;
  return index >= 0 && index < length && to >= 0 && to < length ? to : -1;
}

/* A position label such as "2 of 5", or '' when the file is not queued. */
export function queueLabel(rows, path) {
  const at = Array.isArray(rows) ? rows.findIndex((r) => r && r.path === path) : -1;
  return at < 0 ? '' : at + 1 + ' of ' + rows.length;
}

/* ---------------- transport ---------------- */

/* currentTime + delta clamped into [0, duration]; the duration may be unknown. */
export function seekTarget(cur, delta, dur) {
  const base = typeof cur === 'number' && Number.isFinite(cur) ? cur : 0;
  const top = typeof dur === 'number' && Number.isFinite(dur) && dur > 0 ? dur : Infinity;
  return Math.max(0, Math.min(top, base + delta));
}

/* What a `player_control` event asks of this screen. The server sends
   {type, action: play|pause|seek, seconds?, path}. Returns
   { action, seconds, reopen } or null when the event is not one this screen
   can act on. `reopen` is true when the event names a file other than the one
   open (`aliases` maps the path the server names to the resolved path this
   screen opened, so a symlink or ~ spelling does not reload the file and lose
   the position on every event). */
export function controlPlan(evt, currentPath, aliases = {}) {
  if (!evt || typeof evt !== 'object') return null;
  const action = String(evt.action || '').toLowerCase();
  if (action !== 'play' && action !== 'pause' && action !== 'seek') return null;
  let seconds = null;
  if (action === 'seek') {
    seconds = Number(evt.seconds);
    if (!Number.isFinite(seconds) || seconds < 0) return null;
  }
  const named = typeof evt.path === 'string' ? evt.path : '';
  const resolved = named ? aliases[named] || named : '';
  const reopen = Boolean(named) && resolved !== currentPath;
  return { action, seconds, reopen, path: named };
}

/* ---------------- PDF highlights ---------------- */

export const HL_COLORS = ['yellow', 'green', 'blue', 'pink'];

/* Two client-pixel points and the page's bounding box -> a rectangle in
   fractions of the page, clipped to the page, or null when it is too small to
   be a selection (a click, not a drag) or the box has no size. */
export function rectFromDrag(a, b, box) {
  if (!a || !b || !box || !(box.width > 0) || !(box.height > 0)) return null;
  const clip = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const x1 = clip((Math.min(a.x, b.x) - box.left) / box.width, 0, 1);
  const x2 = clip((Math.max(a.x, b.x) - box.left) / box.width, 0, 1);
  const y1 = clip((Math.min(a.y, b.y) - box.top) / box.height, 0, 1);
  const y2 = clip((Math.max(a.y, b.y) - box.top) / box.height, 0, 1);
  const rect = { x: x1, y: y1, w: x2 - x1, h: y2 - y1 };
  return rect.w < 0.005 || rect.h < 0.003 ? null : rect;
}

export function pageClamp(page, count) {
  const n = Math.trunc(Number(page));
  if (!Number.isFinite(n) || !(count > 0)) return 0;
  return Math.max(0, Math.min(count - 1, n));
}

export function highlightsOnPage(list, page) {
  return (Array.isArray(list) ? list : []).filter((h) => h && h.page === page);
}

/* ---------------- YouTube and Spotify ---------------- */

const WEB_HOSTS = {
  youtube: ['youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be'],
  spotify: ['open.spotify.com'],
};

/* `text` is a pasted link or a search; `service` is 'youtube' or 'spotify'.
   Returns { ok, url, how } or { ok: false, error }. Only https links to the
   service's own hosts are accepted, so this field cannot be used to open
   anything else; a plain phrase becomes that service's own search page. */
export function parseWebLink(text, service) {
  const q = String(text || '').trim();
  const hosts = WEB_HOSTS[service];
  if (!hosts) return { ok: false, error: 'Pick YouTube or Spotify.' };
  if (!q) {
    const home = service === 'youtube' ? 'https://www.youtube.com/' : 'https://open.spotify.com/';
    return { ok: true, url: home, how: 'home' };
  }
  if (/^[a-z][a-z0-9+.-]*:/i.test(q) || q.startsWith('//')) {
    let u;
    try {
      u = new URL(q);
    } catch (_err) {
      return { ok: false, error: 'That is not a usable link.' };
    }
    if (u.protocol !== 'https:') return { ok: false, error: 'Only https links are opened.' };
    if (!hosts.includes(u.hostname.toLowerCase())) {
      return { ok: false, error: 'That link is not on ' + (service === 'youtube' ? 'YouTube' : 'Spotify') + '. Use the link of the service you picked.' };
    }
    return { ok: true, url: u.href, how: 'link' };
  }
  const enc = encodeURIComponent(q);
  return {
    ok: true,
    url: service === 'youtube' ? 'https://www.youtube.com/results?search_query=' + enc : 'https://open.spotify.com/search/' + enc,
    how: 'search',
  };
}
