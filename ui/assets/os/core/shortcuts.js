/* The shell's keyboard shortcuts, written once. boot.js binds them, the
   launcher shows them on the right of each row, and the shortcut help panel
   (Command slash) lists them, so the three can never disagree. Pure: no DOM. */

const CMD = '⌘';

/* screens: the registry's SCREENS array, in sidebar order. Command 1 to 9 go to the first nine. */
export function shortcutList(screens = []) {
  const list = [
    { id: 'launcher', combo: 'Meta+k', keys: CMD + 'K', label: 'Open the launcher', group: 'General' },
    { id: 'settings', combo: 'Meta+,', keys: CMD + ',', label: 'Open SETTINGS', group: 'General', screen: 'SETTINGS' },
    { id: 'new', combo: 'Meta+n', keys: CMD + 'N', label: 'New conversation on HOME', group: 'General' },
    { id: 'help', combo: 'Meta+/', keys: CMD + '/', label: 'Show this list of shortcuts', group: 'General' },
    { id: 'sidebar', combo: 'Meta+\\', keys: CMD + '\\', label: 'Hide or show the sidebar', group: 'General' },
    { id: 'spec', combo: 'Alt+a', keys: 'Alt A', label: 'Label every control (SPEC overlay)', group: 'General', bound: false },
    { id: 'esc', combo: 'Escape', keys: 'Esc', label: 'Close the newest open panel, form or dialog', group: 'General', bound: false },
  ];
  screens.slice(0, 9).forEach((s, i) => {
    list.push({ id: 'screen' + (i + 1), combo: 'Meta+' + (i + 1), keys: CMD + (i + 1), label: 'Go to ' + s.id, group: 'Screens', screen: s.id });
  });
  list.push({ id: 'browser-open', combo: '', keys: 'Alt L', label: 'BROWSER: focus the address bar', group: 'On a screen', bound: false });
  return list;
}

/* the shortcut shown next to a screen or action in the launcher, by screen id or shortcut id */
export function keysFor(list, key) {
  const hit = list.find((s) => s.screen === key || s.id === key);
  return hit ? hit.keys : '';
}

/* the shortcuts boot.js must bind (a documentation-only row has bound: false) */
export function bindable(list) {
  return list.filter((s) => s.bound !== false && s.combo && s.id !== 'launcher');
}
