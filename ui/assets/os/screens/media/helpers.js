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
