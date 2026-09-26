/* The 18 screens, written once. A screen builder never edits this file:
   each loader points at ../screens/<slug>/index.js, and a folder that does not
   exist yet fails to import, which the router renders as an honest "not built
   yet" state. Order is the mockup's (os_mockup.html ORDER). */

const S = (id, thread, sub, load) => ({ id, slug: id.toLowerCase(), label: id, icon: id, thread, sub, load });

export const SCREENS = [
  S('HOME', true, 'central agent dispatch', () => import('../screens/home/index.js')),
  S('COMMS', true, 'real gmail inbox', () => import('../screens/comms/index.js')),
  S('RESEARCH', true, 'evidence pipeline', () => import('../screens/research/index.js')),
  S('BROWSER', false, 'embedded browser', () => import('../screens/browser/index.js')),
  S('MEDIA', true, 'player and library', () => import('../screens/media/index.js')),
  S('CODE', true, 'diff and run', () => import('../screens/code/index.js')),
  S('PROJECTS', false, 'isolated workspaces', () => import('../screens/projects/index.js')),
  S('WIKI', false, 'device wiki', () => import('../screens/wiki/index.js')),
  S('GOALS', false, 'long-running goals', () => import('../screens/goals/index.js')),
  S('TIMETABLE', false, 'schedules', () => import('../screens/timetable/index.js')),
  S('ORCHESTRATION', false, 'live fan-out', () => import('../screens/orchestration/index.js')),
  S('AGENTSMITH', false, 'self-extension proposals', () => import('../screens/agentsmith/index.js')),
  S('VOICE', false, 'voice commands', () => import('../screens/voice/index.js')),
  S('ATLAS', false, 'world monitor', () => import('../screens/atlas/index.js')),
  S('NEWS', true, 'live headlines', () => import('../screens/news/index.js')),
  S('SECURITY', false, 'defensive posture', () => import('../screens/security/index.js')),
  S('SETTINGS', false, 'preferences', () => import('../screens/settings/index.js')),
  S('OFFICE', false, 'agents at work', () => import('../screens/office/index.js')),
];

export const IDS = SCREENS.map((s) => s.id);
export const THREAD_SCREENS = SCREENS.filter((s) => s.thread).map((s) => s.id);

/* The dock: icon-only surfaces worth one click from anywhere. */
export const DOCK = [
  ['HOME', 'Console'],
  ['OFFICE', 'Agents'],
  ['RESEARCH', 'Research'],
  ['SECURITY', 'Security'],
  ['BROWSER', 'Browser'],
  ['MEDIA', 'Media'],
  ['WIKI', 'Wiki'],
  ['GOALS', 'Goals'],
  ['SETTINGS', 'Settings'],
];

/* Owner decision: these three stay in the classic console. The console has no
   hash router (checked: no location.hash use in ui/console.html), so a link
   can only land on its HOME; the user picks the screen from its own sidebar. */
export const CONSOLE_LINKS = [
  ['VISION', 'Vision'],
  ['GLOBE', 'Globe'],
  ['DESIGN3D', 'Design 3D'],
];

export function byId(id) {
  return SCREENS.find((s) => s.id === id) || null;
}

export function bySlug(slug) {
  const s = String(slug || '').toLowerCase();
  return SCREENS.find((x) => x.slug === s) || null;
}
