/* Pure helpers for SETTINGS (no DOM at import time, so node can test them). */

export const SECTION_TITLE = { security: 'Security', agents: 'Agents' };
export const SHELL_LABEL = { auto: 'AUTO', electron: 'ELECTRON', pywebview: 'PYWEBVIEW' };
export const SHELL_WORD = { auto: 'auto', electron: 'Electron', pywebview: 'pywebview' };

/* the background switches, grouped by their section, in the server's order */
export function groupFeatures(items) {
  const groups = [];
  (Array.isArray(items) ? items : []).forEach((f) => {
    if (!f || typeof f.key !== 'string') return;
    let g = groups.find((x) => x.section === f.section);
    if (!g) {
      g = { section: f.section, title: SECTION_TITLE[f.section] || String(f.section || 'Other'), switches: [], folders: [] };
      groups.push(g);
    }
    (f.kind === 'paths' ? g.folders : g.switches).push(f);
  });
  return groups;
}

export function differsFromDefault(f) {
  if (f.kind === 'paths') return Array.isArray(f.value) && f.value.length > 0;
  return typeof f.value === 'boolean' && f.value !== f.default;
}

export function featurePrompt(f, next) {
  const when = 'It is saved to your config file and takes effect the next time Dourmouse starts; nothing running now changes.';
  if (f.section === 'security' && next === false) {
    return 'Turn OFF "' + f.label + '"? ' + f.help + ' Off means this protection stops at the next launch. ' + when;
  }
  return 'Turn ' + (next ? 'ON' : 'OFF') + ' "' + f.label + '"? ' + f.help + ' ' + when;
}

export function foldersPrompt(f, lines) {
  const list = lines.filter(Boolean);
  const what = list.length
    ? 'only these ' + list.length + (list.length === 1 ? ' folder' : ' folders') + ': ' + list.join(', ')
    : 'Documents, Desktop and Downloads (the default)';
  return 'Let the file librarian read ' + what + '? It indexes file names and text from there so you and other agents can find things. It is saved to your config file and takes effect the next time Dourmouse starts.';
}

export function parseFolders(text) {
  return String(text || '').split('\n').map((s) => s.trim()).filter(Boolean).slice(0, 50);
}

export function togglePrompt(t, next) {
  if (t.id === 'auto_approve' && next) {
    return 'Turn ON auto approve? From now on every gated tool runs without asking you: the model can send mail, run commands, change files and control apps with no confirmation card. This applies at once, to every tab, and stays on until you turn it off here. Nothing else is switched by this.';
  }
  if (t.id === 'auto_approve') {
    return 'Turn OFF auto approve? Confirmation cards come back for every gated tool, at once, in every tab.';
  }
  return 'Turn ' + (next ? 'ON' : 'OFF') + ' "' + t.label + '"? ' + t.help + ' It is saved to your config file and applies at once.';
}

export function shellPrompt(value, shell) {
  const running = SHELL_WORD[value] || value;
  let effective = value;
  if (value === 'auto') effective = shell && shell.electron_available ? 'electron' : 'pywebview';
  return 'Set the native shell to ' + running + '? ' +
    (value === 'auto' ? 'That means Electron when it is installed and pywebview otherwise, so the next launch uses ' + (SHELL_WORD[effective] || effective) + '. ' : '') +
    'It is saved to your config file and read only when Dourmouse next starts; the window you are in now does not change.';
}

export function resetPrompt(items) {
  const list = Array.isArray(items) ? items : [];
  const changed = list.filter(differsFromDefault).map((f) => f.label);
  return 'Reset the ' + list.length + ' background switches to their defaults? ' +
    (changed.length ? 'These differ now and will change back: ' + changed.join(', ') + '. ' : 'All of them already match their defaults, so this only clears the saved copies. ') +
    'It does not touch API keys, the orchestrator model, auto approve, the shell choice or any other setting. It takes effect the next time Dourmouse starts.';
}

export function shellChoices(shell) {
  const s = shell || {};
  return ['auto', 'electron', 'pywebview'].map((value) => ({
    value,
    label: SHELL_LABEL[value],
    selected: s.requested === value,
    disabled: value === 'electron' && s.electron_available === false,
    reason: value === 'electron' && s.electron_available === false
      ? 'Electron is not installed in this checkout (electron/node_modules is missing). Run npm install in the electron folder.'
      : '',
  }));
}

export function keyTag(source) {
  if (source === 'saved') return { tone: 'ok', word: 'key set', where: 'saved in your config file' };
  if (source === 'environment') return { tone: 'ok', word: 'key set', where: 'from the environment' };
  if (source === 'none') return { tone: 'warn', word: 'not set', where: '' };
  return { tone: 'warn', word: 'unknown', where: '' };
}

export function tokenTag(access) {
  const a = access || {};
  if (a.token_gate) return { tone: 'ok', word: 'set', note: 'Every request must carry it.' };
  if (a.loopback) return { tone: '', word: 'not set', note: 'Not needed while the server only listens on this Mac.' };
  return { tone: 'bad', word: 'not set', note: 'The server is reachable from other machines with no token.' };
}

export function bindLine(access) {
  const a = access || {};
  const host = String(a.host || '');
  return (host.includes(':') ? '[' + host + ']' : host) + ':' + a.port;
}

export function orchestratorSource(source) {
  return ({
    persisted: 'saved in Settings',
    env_override: 'set by an environment variable',
    default_nvidia: 'default (NVIDIA key present)',
    default_active_backend: 'default of the active backend',
  })[source] || '';
}

export function localModelRow(models) {
  if (models && models.local_active) {
    return { tone: 'bad', word: 'in use', text: 'The active backend is a local server (' + models.base_url + '). The rule is large cloud models only.' };
  }
  return { tone: 'warn', word: 'policy', text: 'Owner rule: large cloud models only, because small and local ones are slow and unreliable at calling tools. Nothing in the code blocks a local model, so this is a rule for you, not a lock.' };
}

export function backendRow(b) {
  return {
    name: String(b && b.name || ''),
    tone: b && b.configured ? 'ok' : 'warn',
    word: b && b.configured ? 'ready' : 'not ready',
    model: b && b.model ? String(b.model) : '',
    detail: b && b.detail ? String(b.detail) : '',
  };
}

export function osReducedMotion(win) {
  try {
    const mm = win && win.matchMedia;
    if (typeof mm !== 'function') return null;
    return Boolean(win.matchMedia('(prefers-reduced-motion: reduce)').matches);
  } catch (_err) {
    return null;
  }
}

/* ---------------- PRIV-5: privacy ---------------- */
export const CONSENT_CHOICES = [
  { id: 'ask', label: 'ASK', spec: 'Ask once, the first time a chat needs it, and remember your answer.' },
  { id: 'allow', label: 'ALLOW', spec: 'Send it to the cloud model when a chat needs it, with identifiers swapped for placeholders.' },
  { id: 'deny', label: 'NEVER', spec: 'Never send it. A chat that needs it is told it was not allowed.' },
];

export function consentPrompt(source, next) {
  const what = source.label + ' (' + source.what + ')';
  if (next === 'allow') {
    return 'Let Dourmouse send your ' + what + ' to the cloud model when a chat needs it? Card and bank numbers, ID numbers, phone numbers, street addresses and other people\'s email addresses are swapped for placeholders first; names and other text are not, and neither is anything you type or attach yourself. It is shared with cloud models, not only one. You can change this here at any time.';
  }
  if (next === 'deny') {
    return 'Never send your ' + what + ' to a cloud model? A chat that needs it is told it was not allowed, and the agent says so.';
  }
  return 'Forget the saved answer for ' + source.label + '? The next chat that needs it asks you once.';
}

export function privateModePrompt(next) {
  return next
    ? 'Turn Private mode ON for every chat? Mail, memory, documents, finance, contacts, files outside the workspace and screen content are then never sent to a cloud model by Dourmouse\'s tools, the tools that read them are hidden, and earlier results from them are removed from each chat\'s context. Web search and everything public still work. What you type or attach yourself is still sent.'
    : 'Turn Private mode OFF for every chat? Sensitive sources are then sent only where you allowed them below. A chat you made private in its own toolbar stays private.';
}

export function providerPrompt(p, next) {
  return next
    ? 'Mark ' + p.label + ' (' + p.company + ') as checked? Tick this only after you looked at that account\'s data retention and model training settings yourself. Dourmouse cannot check them; this records that you did, with the date.'
    : 'Clear the check mark for ' + p.label + '?';
}

export function providerTag(p) {
  if (!p.configured) return { tone: '', word: 'not set up' };
  return p.verified ? { tone: 'ok', word: 'checked' } : { tone: 'warn', word: 'not checked' };
}
