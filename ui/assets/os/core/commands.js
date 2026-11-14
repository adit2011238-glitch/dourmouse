/* The shell's commands, by id (T6 milestone 1, finding #194). The real menu bar
   (electron/appmenu.js) sends these ids, the keymap runs them from the keyboard and the launcher
   from a click, so one command does one thing whichever way it is asked for. A test checks this
   list and the menu's list are the same. Pure: no DOM. */

export const PLACE_SLUGS = ['home', 'projects', 'office', 'research', 'library', 'browser', 'security', 'settings'];

export const COMMAND_IDS = [
  'settings', 'new', 'search', 'sidebar', 'inspector', 'alerts', 'refresh', 'spec', 'shortcuts',
  ...PLACE_SLUGS.map((s) => 'place:' + s),
];

/* actions: { id: fn } for the plain commands plus place(slug). Returns run(id) -> true when it ran. */
export function createCommands(actions) {
  const missing = COMMAND_IDS.filter((id) => !id.startsWith('place:') && typeof actions[id] !== 'function');
  if (missing.length) throw new Error('commands with no action: ' + missing.join(', '));
  return {
    ids: COMMAND_IDS.slice(),
    run(id) {
      const key = String(id || '');
      if (!COMMAND_IDS.includes(key)) return false;
      if (key.startsWith('place:')) {
        actions.place(key.slice(6));
        return true;
      }
      actions[key]();
      return true;
    },
  };
}
