/* The shell's keyboard shortcuts, written once. boot.js binds them, the launcher shows them on the
   right of each row, and Help, Keyboard Shortcuts lists them, so the three can never disagree.
   electron/appmenu.js puts the same keys on the real menu bar (a test compares the two).
   Pure: no DOM. */

const CMD = '⌘';
const OPT = '⌥';
const CTRL = '⌃';

/* places: the registry's PLACES array, in sidebar order. Command 1 to 8 go to them (T6). */
export function shortcutList(places = []) {
  const list = [
    { id: 'search', combo: 'Meta+k', keys: CMD + 'K', label: 'Search', group: 'General' },
    { id: 'settings', combo: 'Meta+,', keys: CMD + ',', label: 'Settings', group: 'General', place: 'SETTINGS' },
    { id: 'new', combo: 'Meta+n', keys: CMD + 'N', label: 'New thread', group: 'General' },
    { id: 'shortcuts', combo: 'Meta+/', keys: CMD + '/', label: 'Keyboard shortcuts', group: 'General' },
    { id: 'sidebar', combo: 'Ctrl+Meta+s', keys: CTRL + CMD + 'S', label: 'Show or hide the sidebar', group: 'View' },
    { id: 'inspector', combo: 'Alt+Meta+i', keys: OPT + CMD + 'I', label: 'Show or hide the inspector', group: 'View' },
    { id: 'alerts', combo: 'Alt+Meta+a', keys: OPT + CMD + 'A', label: 'Show alerts in the inspector', group: 'View' },
    { id: 'refresh', combo: 'Meta+r', keys: CMD + 'R', label: 'Refresh this view', group: 'View' },
    { id: 'undo', combo: 'Meta+z', keys: CMD + 'Z', label: 'Undo the action a notice is offering to undo', group: 'General' },
    { id: 'spec', combo: 'Alt+a', keys: OPT + 'A', label: 'Label every control (Developer)', group: 'View', bound: false },
    { id: 'esc', combo: 'Escape', keys: 'Esc', label: 'Close the sheet, launcher or form in front', group: 'General', bound: false },
  ];
  places.slice(0, 8).forEach((p, i) => {
    list.push({ id: 'place:' + p.slug, combo: 'Meta+' + (i + 1), keys: CMD + (i + 1), label: p.label, group: 'Go', place: p.id });
  });
  list.push({ id: 'browser-open', combo: '', keys: OPT + 'L', label: 'Browser: focus the address bar', group: 'In a view', bound: false });
  return list;
}

/* the shortcut shown next to a place or action in the launcher, by place id, screen id or shortcut id */
export function keysFor(list, key) {
  const hit = list.find((s) => s.place === key || s.id === key);
  return hit ? hit.keys : '';
}

/* the shortcuts boot.js must bind (a documentation-only row has bound: false) */
export function bindable(list) {
  return list.filter((s) => s.bound !== false && s.combo);
}
