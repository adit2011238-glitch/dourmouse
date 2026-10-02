/* BROWSER (Phase B3): the Extensions panel and the Profiles and import panel.

   What this file may and may not do:
   - It talks to the Electron shell only through window.dourmouseShell.pane.manage, whose handlers
     answer the console window alone. It never calls a server route to add, enable, remove,
     switch or import anything.
   - It never types or holds a path. An extension folder, a Chrome profile folder and a password
     CSV are all chosen in a NATIVE macOS dialog that the shell opens, and each of those actions
     ends in a native confirmation that names what will happen. A script driving this page cannot
     press either.
   - Nothing here shows a password. An import returns counts, and this file only turns counts
     into words. */

import { states } from '../../kit/states.js';
import { confirmHere } from '../../kit/confirm-card.js';
import { isAbort } from '../../core/api.js';
import { ago } from '../../kit/format.js';
import { extensionRows, extensionState, profileList, profileNameProblem, importLines } from './manage-model.js';

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

/* ctx: the screen context. manage: pane.manage. note(text, tone): the screen's own message line.
   onChange(): called after a profile switch or an import, so the screen can re-read the bookmarks. */
export function createManage({ manage, note, onChange }) {
  const fail = (err, what) => {
    if (isAbort(err)) return;
    note(what + ': ' + (err && err.message ? err.message : String(err)), 'error');
  };
  const call = async (fn, what) => {
    try {
      const r = await fn();
      if (r && r.ok === false && !r.cancelled) note(what + ': ' + (r.error || 'no reason given'), 'error');
      return r;
    } catch (err) {
      fail(err, what);
      return null;
    }
  };
  /* a run() for confirmHere: a refusal from the shell becomes the card's own error line */
  const orThrow = (r) => {
    if (!r || r.ok === false) throw new Error(r && r.error ? r.error : 'no answer');
    return r;
  };

  /* ---------------- Extensions ---------------- */
  function extensionsPanel(panelRoot, { head, button, close }) {
    let alive = true;
    const body = el('div', 'bw-pb');
    const slot = el('div', 'bw-pc');
    const noteEl = el('p', 'bw-pnote', 'Extensions are loaded with Electron\'s own extension support.');
    const add = button('ADD EXTENSION', async () => {
      add.disabled = true;
      const r = await call(() => manage.extensions.add(), 'The extension was not added');
      add.disabled = false;
      if (r && r.ok) note('Added "' + r.extension.name + '".', 'info');
      if (alive) load();
    }, 'Opens a macOS folder picker, then a macOS confirmation that lists the extension\'s name and everything it asks for. Nothing is added until you confirm there. The folder is copied into Dourmouse\'s own folder and only the copy runs.');
    panelRoot.dataset.panel = 'extensions';
    panelRoot.replaceChildren(head('Extensions', add, close), noteEl, slot, body);

    async function load() {
      states.loading(body, 'Reading extensions');
      try {
        const d = await manage.extensions.list();
        if (!alive) return;
        if (d && d.ok === false) throw new Error(d.error || 'no reason given');
        noteEl.textContent = (d && d.note) || noteEl.textContent;
        add.disabled = d && d.supported === false;
        const rows = extensionRows(d);
        if (d && d.supported === false) {
          states.empty(body, 'This build of Electron has no extension support.', { hint: 'Extensions need the Electron shell, version 44 or newer.' });
          return;
        }
        if (!rows.length) {
          states.empty(body, 'No extension is installed.', { hint: 'Add an unpacked extension folder. Packed .crx files and the Chrome Web Store are not supported.' });
          return;
        }
        states.populated(body, rows.map(extRow));
      } catch (err) {
        if (!alive || isAbort(err)) return;
        states.error(body, err, { retry: () => load(), title: 'Could not read the extensions' });
      }
    }

    function extRow(r) {
      const row = el('div', 'bw-pwrow bw-ext');
      row.dataset.risk = r.risk;
      row.dataset.extId = r.id;
      const info = el('div', 'bw-pwinfo');
      info.append(el('span', 'bw-pwhost', r.name + (r.version ? ' ' + r.version : '')), el('span', 'bw-pwuser', extensionState(r) + (r.addedAt ? ', added ' + ago(r.addedAt) : '')));
      if (r.error) info.append(el('span', 'bw-exterr', r.error));
      if (r.summary.length) {
        const more = el('details', 'bw-extmore');
        more.append(el('summary', '', r.risk === 'high' ? 'Wide access: what it can reach' : 'What it can reach'));
        const ul = el('ul');
        r.summary.forEach((l) => ul.append(el('li', '', l)));
        more.append(ul);
        info.append(more);
      }
      const acts = el('div', 'ac');
      const rowSlot = el('div', 'bw-pc');
      const toggle = el('button', 'os-btn', r.enabled ? 'DISABLE' : 'ENABLE');
      toggle.type = 'button';
      toggle.dataset.spec = r.enabled
        ? 'Turns this extension off and unloads it. Its files stay, so it can be turned on again.'
        : 'Turns this extension on again. A macOS confirmation lists what it can reach first.';
      toggle.addEventListener('click', async () => {
        toggle.disabled = true;
        await call(() => (r.enabled ? manage.extensions.disable(r.id) : manage.extensions.enable(r.id)), 'The extension was not changed');
        if (alive) load();
      });
      const del = el('button', 'os-btn', 'REMOVE');
      del.type = 'button';
      del.dataset.spec = 'Unloads this extension and deletes its copy from this Mac after you confirm.';
      del.addEventListener('click', () => {
        confirmHere(rowSlot, 'Remove the extension "' + r.name + '"? Its copy is deleted from this Mac.', async () => orThrow(await manage.extensions.remove(r.id)), {
          onDone: (ok) => { rowSlot.replaceChildren(); if (ok && alive) load(); },
        });
      });
      acts.append(toggle, del);
      row.append(info, acts, rowSlot);
      return row;
    }

    load();
    return { dispose: () => { alive = false; } };
  }

  /* ---------------- Profiles and import ---------------- */
  function profilesPanel(panelRoot, { head, button, close }) {
    let alive = true;
    const listBody = el('div', 'bw-pb');
    const importBody = el('div', 'bw-pb');
    const slot = el('div', 'bw-pc');
    panelRoot.dataset.panel = 'profiles';
    panelRoot.replaceChildren(
      head('Profiles and import', close),
      el('p', 'bw-pnote', 'A profile has its own cookies and logins, history, bookmarks, site permissions and saved passwords. Switching closes the open tabs and opens the other profile. The AI browser tools follow the profile on screen.'),
      slot,
      el('h4', 'bw-day', 'Profiles'),
      listBody,
      el('h4', 'bw-day', 'Import from Chrome'),
      importBody,
    );

    async function loadProfiles() {
      states.loading(listBody, 'Reading profiles');
      try {
        const d = await manage.profiles.list();
        if (!alive) return;
        if (d && d.ok === false) throw new Error(d.error || 'no reason given');
        const list = profileList(d);
        const nodes = list.rows.map((p) => profileRow(p, list));
        nodes.push(createForm(list));
        states.populated(listBody, nodes);
      } catch (err) {
        if (!alive || isAbort(err)) return;
        states.error(listBody, err, { retry: () => loadProfiles(), title: 'Could not read the profiles' });
      }
    }

    function profileRow(p) {
      const row = el('div', 'bw-pwrow');
      row.dataset.profile = p.name;
      const info = el('div', 'bw-pwinfo');
      info.append(el('span', 'bw-pwhost', p.isDefault ? 'default' : p.name), el('span', 'bw-pwuser', p.active ? 'In use now' : p.isDefault ? 'The normal browser' : 'Not in use'));
      const acts = el('div', 'ac');
      const rowSlot = el('div', 'bw-pc');
      if (!p.active) {
        const sw = el('button', 'os-btn', 'SWITCH');
        sw.type = 'button';
        sw.dataset.spec = 'Closes the open tabs and opens this profile, with its own logins, history and bookmarks.';
        sw.addEventListener('click', () => {
          confirmHere(rowSlot, 'Switch to the profile "' + p.name + '"? The tabs that are open now are closed.', async () => orThrow(await manage.profiles.switchTo(p.name)), {
            onDone: (ok) => { rowSlot.replaceChildren(); if (ok) { if (onChange) onChange(); if (alive) loadProfiles(); } },
          });
        });
        acts.append(sw);
        if (!p.isDefault) {
          const del = el('button', 'os-btn', 'REMOVE');
          del.type = 'button';
          del.dataset.spec = 'Deletes this profile and everything in it, after a macOS confirmation.';
          del.addEventListener('click', async () => {
            del.disabled = true;
            const r = await call(() => manage.profiles.remove(p.name), 'The profile was not removed');
            del.disabled = false;
            if (r && r.ok && alive) loadProfiles();
          });
          acts.append(del);
        }
      }
      row.append(info, acts, rowSlot);
      return row;
    }

    function createForm(list) {
      const form = el('form', 'bw-addrform bw-profform');
      form.noValidate = true;
      const lab = el('label', 'bw-af');
      lab.append(el('span', '', 'New profile name'));
      const inp = el('input');
      inp.type = 'text';
      inp.autocomplete = 'off';
      inp.maxLength = 24;
      inp.placeholder = 'work';
      lab.append(inp);
      const msg = el('div', 'bw-pnote');
      const go = el('button', 'os-btn', 'CREATE PROFILE');
      go.type = 'submit';
      form.append(lab, msg, go);
      form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const problem = profileNameProblem(inp.value, list.rows);
        if (problem) {
          msg.textContent = problem;
          return;
        }
        const r = await call(() => manage.profiles.create(inp.value), 'The profile was not created');
        if (r && r.ok && alive) loadProfiles();
      });
      return form;
    }

    /* ---- import ---- */
    const resultEl = el('div', 'bw-importresult');
    resultEl.setAttribute('role', 'status');
    resultEl.hidden = true;

    function showResult(r, what) {
      if (!r) return;
      if (r.cancelled) {
        resultEl.hidden = true;
        return;
      }
      resultEl.replaceChildren();
      if (r.ok === false) {
        resultEl.append(el('p', 'bw-pnote', what + ': ' + (r.error || 'no reason given')));
      } else {
        resultEl.append(el('h4', 'bw-day', 'Imported into the profile "' + (r.profile || 'default') + '"'));
        importLines(r).forEach((l) => resultEl.append(el('p', 'bw-pnote', l)));
      }
      resultEl.hidden = false;
    }

    const wantBm = el('input');
    wantBm.type = 'checkbox';
    wantBm.checked = true;
    const wantHi = el('input');
    wantHi.type = 'checkbox';
    wantHi.checked = true;
    const chk = (input, text) => {
      const l = el('label', 'bw-chk');
      l.append(input, el('span', '', text));
      return l;
    };
    const chromeBtn = el('button', 'os-btn', 'IMPORT BOOKMARKS AND HISTORY');
    chromeBtn.type = 'button';
    chromeBtn.dataset.spec = 'Opens a macOS folder picker for a Chrome profile folder, then a macOS confirmation with the counts. Only its Bookmarks and History files are read, as copies. Passwords, cookies and the rest are never touched, and Chrome is not changed.';
    chromeBtn.addEventListener('click', async () => {
      chromeBtn.disabled = true;
      const r = await call(() => manage.importFrom.chrome({ bookmarks: wantBm.checked, history: wantHi.checked }), 'The import did not run');
      chromeBtn.disabled = false;
      showResult(r, 'Nothing was imported');
      if (r && r.ok && onChange) onChange();
    });
    const pwBtn = el('button', 'os-btn', 'IMPORT PASSWORDS FROM A CSV');
    pwBtn.type = 'button';
    pwBtn.dataset.spec = 'Opens a macOS file picker for the CSV you exported from Chrome, then a macOS confirmation with the count. The passwords are encrypted into this Mac\'s Keychain-protected vault. The file is not copied or changed, and Dourmouse never reads Chrome\'s own password store.';
    pwBtn.addEventListener('click', async () => {
      pwBtn.disabled = true;
      const r = await call(() => manage.importFrom.passwords(), 'The import did not run');
      pwBtn.disabled = false;
      showResult(r, 'Nothing was imported');
    });
    importBody.append(
      el('p', 'bw-pnote', 'Import is read-only and explicit. You pick the Chrome profile folder (for example Default inside Chrome\'s own folder) or the exported CSV in a macOS dialog, and a macOS confirmation shows what will be imported. Chrome\'s own password database and its Keychain key are never read, and neither are its cookies. To get a CSV, export your passwords from Chrome (Settings, Passwords) and delete the file after the import.'),
      chk(wantBm, 'Bookmarks'),
      chk(wantHi, 'History'),
      chromeBtn,
      el('div', 'bw-gap'),
      pwBtn,
      resultEl,
    );

    loadProfiles();
    return { dispose: () => { alive = false; } };
  }

  return { panels: { extensions: extensionsPanel, profiles: profilesPanel } };
}
