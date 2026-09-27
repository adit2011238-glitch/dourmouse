/* Stage action helpers. The stage reuses one button per key (an action's id, or
   its label when it has none) so a state change never drops keyboard focus. Two
   actions with the same key in one call would silently become ONE button: the
   later one wins and the earlier one never renders (this hid RESEARCH's NEW
   QUESTION behind NEW THREAD). So a duplicate key is refused loudly. */

export function actionKey(a) {
  return String((a && (a.id || a.label)) || '');
}

export function assertUniqueActions(actions) {
  const seen = new Set();
  (actions || []).forEach((a) => {
    const key = actionKey(a);
    if (!key) throw new Error('A stage action needs an id or a label.');
    if (seen.has(key)) throw new Error('Two stage actions share the id "' + key + '": one of them would never be drawn.');
    seen.add(key);
  });
}

/* The same list with every repeated or missing key made unique, so no button is
   dropped. The stage calls it only after assertUniqueActions has complained: the
   console names the bug and the screen keeps working. */
export function dedupeActions(actions) {
  const used = new Set();
  return (actions || []).map((a) => {
    const own = actionKey(a);
    let key = own || 'action';
    const base = key;
    let n = 1;
    while (used.has(key)) {
      n += 1;
      key = base + '~' + n;
    }
    used.add(key);
    return own && key === own ? a : { label: key, ...(a || {}), id: key };
  });
}
