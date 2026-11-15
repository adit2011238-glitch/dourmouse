/* Pure helpers for COMMAND CENTER. Nothing here touches the DOM, so node
   can test it in isolation (same split as orchestration/helpers.js and
   security/helpers.js).

   Source of the agent lanes: the "agent_trace" event type (finding #186,
   dourmouse/dispatch.py's _broadcast_live_trace / _emit_event) -- a FULL,
   untruncated copy of every dispatch transcript entry, tagged with the
   real agent and call_id, broadcast to every /api/events client whether
   or not that run's own chat tab is open. This is deliberately a SEPARATE
   feed from "agent_activity" (status only: idle/computing/auth) and from
   the per-request SSE stream a single chat tab gets -- see dispatch.py's
   own docstring on _broadcast_live_trace for why a background goal or
   routine would otherwise be invisible until it finished. */

export const LANE_CAP = 8; // oldest finished lane dropped beyond this
export const LANE_TEXT_CAP = 20000; // characters kept per lane before trimming the oldest text

/** One lane = one call_id's whole live transcript, newest text at the end. */
function newLane(agent, call_id) {
  return { agent: agent || 'orchestrator', call_id: call_id || '', text: '', lines: [], done: false, startedAt: Date.now(), lastAt: Date.now() };
}

function describe(entry) {
  const t = entry.type;
  if (t === 'thinking_delta') return { kind: 'thinking', text: entry.text || '' };
  if (t === 'assistant_delta') return { kind: 'answer', text: entry.text || '' };
  if (t === 'assistant_text') return { kind: 'answer', text: entry.text || '', final: true };
  if (t === 'tool_use') return { kind: 'tool', text: 'CALL ' + (entry.name || '?') + '(' + String(entry.raw_arguments || entry.arguments || '').slice(0, 2000) + ')' };
  if (t === 'tool_result') return { kind: 'result', text: 'RESULT ' + (entry.name || '?') + ': ' + String(entry.text || '').slice(0, 4000) };
  if (t === 'plan') return { kind: 'plan', text: 'PLAN: ' + (entry.steps || []).map((s) => (s.subagent || '?') + (s.task ? ' -- ' + s.task : '')).join(' | ') };
  if (t === 'confirmation_requested') return { kind: 'gate', text: 'ASKED: ' + (entry.summary || entry.text || 'confirmation required') };
  if (t === 'confirmation_resolved') return { kind: 'gate', text: (entry.approved ? 'APPROVED' : 'DECLINED') + (entry.summary ? ': ' + entry.summary : '') };
  if (t === 'stopped_by_user') return { kind: 'status', text: 'STOPPED by the owner' };
  if (t === 'budget_exhausted') return { kind: 'status', text: 'TOOL BUDGET exhausted' };
  if (t === 'grounded_reminder') return { kind: 'status', text: 'GROUNDED MODE: no tool call used despite real tools being available' };
  if (t === 'brain' || t === 'brain_thinking') return { kind: 'status', text: (entry.model ? 'model: ' + entry.model : '') };
  if (t === 'function') return { kind: 'tool', text: String(entry.text || entry.name || '') };
  return { kind: 'other', text: JSON.stringify(entry).slice(0, 500) };
}

/** Applies one "agent_trace" event's inner `entry` to the lanes map, keyed by call_id.
    An entry without a call id is ignored (CC-1). */
export function applyTrace(lanes, order, entry) {
  /* CC-1: a lane is a real run. An entry with no call id belongs to no run, so it opens nothing. */
  if (!entry || !entry.call_id) return null;
  const key = entry.call_id;
  let lane = lanes[key];
  if (!lane) {
    lane = newLane(entry.agent, entry.call_id);
    lanes[key] = lane;
    order.unshift(key);
  }
  const d = describe(entry);
  lane.lastAt = Date.now();
  if (d.text) {
    lane.lines.push({ kind: d.kind, text: d.text, at: lane.lastAt });
    if (lane.lines.length > 4000) lane.lines.splice(0, lane.lines.length - 4000);
  }
  if (d.final || entry.type === 'stopped_by_user') lane.done = true;
  /* bring the just-touched lane to the front -- after this, `key` is always
     order[0], so the eviction loop below (which only ever pops the END)
     can never pop the lane just touched, and always shrinks. */
  const i = order.indexOf(key);
  if (i > 0) {
    order.splice(i, 1);
    order.unshift(key);
  }
  while (order.length > LANE_CAP) {
    const dropped = order.pop();
    delete lanes[dropped];
  }
  return key;
}

export function laneSummary(lane) {
  const n = lane.lines.length;
  const kinds = {};
  for (const l of lane.lines) kinds[l.kind] = (kinds[l.kind] || 0) + 1;
  return { n, kinds };
}

/** Opens a lane for a run the server reports as live right now (GET /api/os/office/activity). */
export function seedLane(lanes, order, lane) {
  if (!lane || !lane.call_id || lanes[lane.call_id]) return null;
  const made = newLane(lane.agent, lane.call_id);
  made.waiting = lane.status === 'auth';
  made.tool = lane.tool || '';
  lanes[lane.call_id] = made;
  order.push(lane.call_id);
  while (order.length > LANE_CAP) delete lanes[order.pop()];
  return lane.call_id;
}

/** Model-free feed polls: skill -> { tool, at }. They are not runs and never open a lane. */
export function applyFeeds(feeds, evt) {
  const agents = evt && evt.agents && typeof evt.agents === 'object' ? evt.agents : {};
  for (const [skill, a] of Object.entries(agents)) {
    if (a && a.status === 'live' && a.last) feeds[skill] = { tool: a.last.tool, at: a.last.at };
    else if (a && a.status && a.status !== 'live') delete feeds[skill];
  }
  return feeds;
}

export function seedFeeds(list) {
  const out = {};
  for (const f of Array.isArray(list) ? list : []) if (f && f.skill) out[f.skill] = { tool: f.tool, at: f.at };
  return out;
}

function minutes(at, now) {
  const ms = typeof at === 'number' ? at : Date.parse(at);
  if (!Number.isFinite(ms)) return '';
  const m = Math.max(0, Math.round((now - ms) / 60000));
  return m < 1 ? 'just now' : m + ' min ago';
}

/** One plain line: "Feeds: news polled 2 min ago, mail polled just now. No model runs for these." */
export function feedsLine(feeds, now = Date.now()) {
  const rows = Object.entries(feeds || {}).map(([skill, f]) => ({ skill, when: minutes(f.at, now), at: Date.parse(f.at) || 0 }));
  if (!rows.length) return '';
  rows.sort((a, b) => b.at - a.at);
  return 'Feeds: ' + rows.map((r) => r.skill + ' polled ' + (r.when || 'recently')).join(', ') + '. No model runs for these.';
}
