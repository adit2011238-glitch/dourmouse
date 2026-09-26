/* NEWS: pure helpers, no DOM at import time so node can test them. */

export const FEED_CAP = 60; /* newest headlines kept on screen; the server keeps 200 */

const CHANNEL_WORD = { news: 'news', disasters: 'disaster', conflict_events: 'conflict' };

export function channelWord(channel) {
  const c = String(channel || '');
  return CHANNEL_WORD[c] || c.replace(/_/g, ' ') || 'feed';
}

/* Only http and https links are ever offered as something to open. */
export function safeLink(link) {
  const u = String(link || '').trim();
  return /^https?:\/\/[^\s/][^\s]{0,1999}$/i.test(u) ? u : '';
}

export function itemKey(item) {
  return [item && item.channel, item && item.title, item && item.link].map((x) => String(x || '')).join('|');
}

export function isImportant(item) {
  return Boolean(item && item.important);
}

export function itemAt(item) {
  return item && (item.at || item.published || item.time) ? item.at || item.published || item.time : '';
}

/* One plain line about the watcher, from GET /api/news status. */
export function watcherLine(resp) {
  if (!resp || resp.running === false || !resp.status) {
    return { tone: 'off', text: 'The news watcher is not running in this server, so no headlines are being collected.' };
  }
  const s = resp.status;
  const every = Number(s.poll_interval_seconds);
  const cadence = Number.isFinite(every) && every > 0 ? 'Polls every ' + Math.round(every) + ' seconds.' : '';
  if (s.last_poll_ok === false) {
    return { tone: 'bad', text: 'The last poll failed' + (s.last_error ? ': ' + String(s.last_error).slice(0, 200) : '.') + ' ' + cadence };
  }
  if (s.last_poll_ok === null || s.last_poll_ok === undefined) {
    return { tone: 'wait', text: 'The watcher has started and has not finished its first poll. ' + cadence };
  }
  return { tone: 'ok', text: 'The last poll succeeded. ' + cadence + ' Sources: Google News RSS, GDACS disaster alerts and GDELT conflict events.' };
}

/* What TO RESEARCH will do, said before it does it. */
export function researchPrompt(item) {
  const t = String((item && item.title) || '').slice(0, 200);
  return 'Start a research record for "' + t + '" with this link as its first source? This writes one row in the local research database. It does not fetch the link and does not call a model; you run the model stages from RESEARCH.';
}
