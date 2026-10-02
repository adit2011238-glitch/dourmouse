/* BROWSER (Phase B2): the bars and panels for site permissions, saved passwords and address
   autofill, drawn in the console window.

   What this file may and may not do:
   - It talks to the Electron shell only through window.dourmouseShell.pane.privacy, whose
     handlers answer the console window alone. It never calls a server route for any of this,
     and the server has no copy of any of it.
   - A prompt, a Save bar and a Fill bar are drawn ABOVE the page area (the native page view is
     composited above this page's DOM, so nothing can be drawn over it). The page area shrinks
     to make room, and the screen's observers move the view.
   - The only call that returns a password is reveal. It runs after a confirmation card here AND
     a native dialog the shell opens, and the password is shown for ten seconds, then cleared. */

import { states } from '../../kit/states.js';
import { confirmHere } from '../../kit/confirm-card.js';
import { isAbort } from '../../core/api.js';
import { ago } from '../../kit/format.js';
import {
  privacyModel, barsKey, waitingLine, saveLine, groupSites, passwordRows, neverRows, profileRows,
  addressFromForm, vaultLine, ADDRESS_FIELDS, siteHost, clickDelayLeft,
} from './privacy-model.js';
import { drmLine } from './manage-model.js';

export const REVEAL_MS = 10000;

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

/* One row above the page: a sentence on the left, its buttons on the right. */
function bar(kind, label) {
  const b = el('div', 'cb-ask');
  b.dataset.kind = kind;
  b.setAttribute('role', 'group');
  b.setAttribute('aria-label', label);
  b.append(el('span', 'ic'), el('span', 'tx'), el('span', 'ac'));
  b.querySelector('.ic').setAttribute('aria-hidden', 'true');
  return b;
}

/* ctx: the screen context. note(text, tone): the screen's own message line.
   The returned object is what index.js wires in. */
export function createPrivacy({ ctx, privacy, note }) {
  let model = privacyModel(null);
  let lastKey = '';
  let noticeHidden = '';
  /* when each permission and Save prompt first appeared, for the click-ambush guard */
  const firstSeen = new Map();
  const guardTimers = new Set();
  const root = el('div', 'cb-privacy');
  root.hidden = true;
  const disposers = new Set();

  const fail = (err, what) => {
    if (isAbort(err)) return;
    note(what + ': ' + (err && err.message ? err.message : String(err)), 'error');
  };
  const call = async (fn, what) => {
    try {
      const r = await fn();
      if (r && r.ok === false) {
        note(what + ': ' + (r.error || 'no reason given'), 'error');
        return r;
      }
      return r;
    } catch (err) {
      fail(err, what);
      return null;
    }
  };

  /* ---------------- the bars above the page ---------------- */
  function permBar(m) {
    const b = bar('perm', 'Site permission request');
    b.querySelector('.tx').textContent = m.perm.text;
    const more = waitingLine(m);
    if (more) b.querySelector('.tx').append(el('span', 'more', ' (' + more + ')'));
    const acts = b.querySelector('.ac');
    acts.append(
      btn('Allow', 'cb-go', 'Allow this site and remember it. You can undo it in Site settings.', () => answerPerm('allow')),
      btn('Allow this time', '', 'Allow it for this tab until you leave the site or close the tab. Nothing is remembered.', () => answerPerm('once')),
      btn('Block', '', 'Refuse and remember it, so this site does not ask again. You can change it in Site settings.', () => answerPerm('block')),
      btn('Dismiss', '', 'Closes this request and tells the page no, without remembering it. The same site is not asked again for a minute.', () => answerPerm('dismiss')),
    );
    return b;
  }

  function saveBar(m) {
    const b = bar('save', 'Save password');
    b.querySelector('.tx').textContent = saveLine(m.save);
    b.querySelector('.ac').append(
      btn(m.save.update ? 'Update' : 'Save', 'cb-go', 'Encrypts it with this Mac\'s Keychain key and keeps it on this Mac only.', () => answerSave('save')),
      btn('Never for this site', '', 'Stops asking to save passwords on this site. You can undo it in the Passwords panel.', () => answerSave('never')),
      btn('Not now', '', 'Forgets this login without saving it.', () => answerSave('dismiss')),
    );
    return b;
  }

  function fillBar(m) {
    const b = bar('fill', 'Saved login');
    const tx = b.querySelector('.tx');
    tx.textContent = 'Saved login for ' + m.fill.host;
    let pick = null;
    if (m.fill.entries.length > 1) {
      pick = el('select', 'cb-pick-user');
      pick.setAttribute('aria-label', 'Which saved login');
      m.fill.entries.forEach((e) => {
        const o = el('option', '', e.username || '(no username)');
        o.value = e.id;
        pick.append(o);
      });
      tx.append(' ', pick);
    } else {
      tx.append(el('span', 'more', ' (' + (m.fill.entries[0].username || 'no username') + ')'));
    }
    b.querySelector('.ac').append(
      btn('Fill', 'cb-go', 'Types the saved username and password into this page. It never presses Sign in for you, and it only fills a page at exactly this address.', () => {
        call(() => privacy.pwFill(pick ? pick.value : m.fill.entries[0].id), 'The login was not filled');
      }),
    );
    return b;
  }

  function addressBar(m) {
    const b = bar('addr', 'Saved address');
    const tx = b.querySelector('.tx');
    tx.textContent = 'Fill in your address on this page';
    let pick = null;
    if (m.fillAddress.profiles.length > 1) {
      pick = el('select', 'cb-pick-user');
      pick.setAttribute('aria-label', 'Which saved address');
      m.fillAddress.profiles.forEach((p) => {
        const o = el('option', '', p.label);
        o.value = p.id;
        pick.append(o);
      });
      tx.append(' ', pick);
    }
    b.querySelector('.ac').append(
      btn('Fill address', '', 'Fills the empty address fields on this page from the address you saved. It never overwrites what you typed.', () => {
        call(() => privacy.addrFill(pick ? pick.value : m.fillAddress.profiles[0].id), 'The address was not filled');
      }),
    );
    return b;
  }

  function noticeBar(m) {
    const b = bar('note', 'Notice');
    b.querySelector('.tx').textContent = m.notice;
    b.querySelector('.ac').append(btn('Dismiss', '', 'Hides this message.', () => {
      noticeHidden = m.notice;
      lastKey = '';
      paintBars();
    }));
    return b;
  }

  function btn(label, cls, spec, onClick) {
    const b = el('button', 'cb-ask-btn' + (cls ? ' ' + cls : ''), label);
    b.type = 'button';
    if (spec) b.dataset.spec = spec;
    b.addEventListener('click', onClick);
    return b;
  }

  /* The click-ambush guard: the buttons of a permission or Save prompt cannot be pressed (by the
     pointer or the keyboard) until CLICK_DELAY_MS after the prompt first appeared, so a click
     that was meant for the page a moment ago cannot answer it. A rebuild of the same prompt does
     not restart the wait. */
  function guardBar(b, id) {
    const first = firstSeen.has(id) ? firstSeen.get(id) : Date.now();
    firstSeen.set(id, first);
    const left = clickDelayLeft(first, Date.now());
    if (left <= 0) return b;
    const buttons = Array.from(b.querySelectorAll('.cb-ask-btn'));
    buttons.forEach((x) => { x.disabled = true; });
    b.dataset.armed = '0';
    const t = setTimeout(() => {
      guardTimers.delete(t);
      buttons.forEach((x) => { x.disabled = false; });
      b.dataset.armed = '1';
    }, left);
    guardTimers.add(t);
    return b;
  }

  function answerPerm(decision) {
    if (!model.perm) return;
    const id = model.perm.id;
    call(() => privacy.permAnswer(id, decision), 'The answer was not delivered');
  }

  function answerSave(answer) {
    if (!model.save) return;
    const id = model.save.id;
    call(() => privacy.pwSaveAnswer(id, answer), 'The password was not handled');
  }

  function paintBars() {
    const key = barsKey(model, noticeHidden);
    if (key === lastKey) return;
    lastKey = key;
    const focused = root.contains(document.activeElement) ? document.activeElement : null;
    const keep = focused && focused.dataset.spec ? focused.textContent : '';
    const bars = [];
    guardTimers.forEach((t) => clearTimeout(t));
    guardTimers.clear();
    const live = new Set();
    if (model.perm) {
      live.add('perm:' + model.perm.id);
      bars.push(guardBar(permBar(model), 'perm:' + model.perm.id));
    }
    if (model.save) {
      live.add('save:' + model.save.id);
      bars.push(guardBar(saveBar(model), 'save:' + model.save.id));
    }
    for (const id of Array.from(firstSeen.keys())) if (!live.has(id)) firstSeen.delete(id);
    if (model.fill) bars.push(fillBar(model));
    if (model.fillAddress) bars.push(addressBar(model));
    if (model.notice && model.notice !== noticeHidden) bars.push(noticeBar(model));
    root.replaceChildren(...bars);
    root.hidden = bars.length === 0;
    if (keep) {
      const again = Array.from(root.querySelectorAll('button')).find((x) => x.textContent === keep);
      if (again) again.focus();
    }
  }

  function update(raw) {
    model = privacyModel(raw);
    if (!model.notice) noticeHidden = '';
    paintBars();
    return model;
  }

  /* ---------------- Site settings panel ---------------- */
  function sitesPanel(panelRoot, { head, button, close }) {
    const body = el('div', 'bw-pb');
    const confirm = el('div', 'bw-pc');
    const drmEl = el('p', 'bw-pnote bw-drm');
    drmEl.dataset.role = 'drm-status';
    let alive = true;
    const clearAll = button('RESET ALL', () => {
      confirmHere(
        confirm,
        'Reset every site permission? All remembered Allow and Block answers are removed, and sites will ask again.',
        () => privacy.sitesClear(),
        { onDone: (ok) => { confirm.replaceChildren(); if (ok) load(); } },
      );
    }, 'Forgets every remembered Allow and Block answer after you confirm. Sites ask again the next time.');
    panelRoot.dataset.panel = 'sites';
    panelRoot.replaceChildren(
      head('Site settings', clearAll, close),
      el('p', 'bw-pnote', 'Sites can ask to use your camera, microphone, location, notifications, the clipboard, and full screen. You answer in the bar above the page, and the answer is kept here. Screen sharing, MIDI, USB and the like are never allowed. The camera and microphone also obey the privacy kill switch and macOS.'),
      drmEl,
      confirm,
      body,
    );

    /* What the engine really says about Widevine (stock Electron has none; see B3_DRM_PLAN.md).
       Read when the panel opens and written as the engine answered, including "not available". */
    drmEl.textContent = 'Checking protected video (DRM) support.';
    if (typeof privacy.drm === 'function') {
      Promise.resolve(privacy.drm()).then((r) => {
        if (alive) drmEl.textContent = drmLine(r);
      }).catch((err) => {
        if (alive && !isAbort(err)) drmEl.textContent = 'DRM status could not be read.';
      });
    } else {
      drmEl.textContent = 'DRM status is not available in this older shell.';
    }

    async function load() {
      states.loading(body, 'Reading site settings');
      try {
        const d = await privacy.sites();
        if (!alive) return;
        if (d && d.ok === false) throw new Error(d.error || 'no reason given');
        const groups = groupSites(d.sites);
        if (!groups.length) {
          states.empty(body, 'No site has a saved permission yet.', { hint: 'When a page asks, the bar above it offers Allow, Allow this time and Block.' });
          return;
        }
        states.populated(body, groups.map((g) => siteGroup(g, load)));
      } catch (err) {
        if (!alive || isAbort(err)) return;
        states.error(body, err, { retry: () => load(), title: 'Could not read the site settings' });
      }
    }
    load();
    return { dispose: () => { alive = false; } };
  }

  function siteGroup(g, reload) {
    const box = el('div', 'bw-site');
    const top = el('div', 'bw-sitehead');
    top.append(el('span', 'bw-sitename', g.host));
    const forget = el('button', 'os-btn', 'FORGET SITE');
    forget.type = 'button';
    forget.dataset.spec = 'Removes every saved answer for this site, so it asks again.';
    forget.addEventListener('click', async () => {
      await call(() => privacy.siteForget(g.origin), 'The site was not forgotten');
      reload();
    });
    top.append(forget);
    box.append(top);
    g.items.forEach((it) => {
      const row = el('div', 'bw-siterow');
      row.append(el('span', 'bw-perm', it.label));
      const sel = el('select', 'bw-permsel');
      sel.setAttribute('aria-label', it.label + ' for ' + g.host);
      [['allow', 'Allow'], ['block', 'Block'], ['reset', 'Ask again']].forEach(([v, t]) => {
        const o = el('option', '', t);
        o.value = v;
        if (v === it.decision) o.selected = true;
        sel.append(o);
      });
      sel.addEventListener('change', async () => {
        await call(() => privacy.siteSet(g.origin, it.permission, sel.value), 'The permission was not changed');
        reload();
      });
      row.append(sel);
      if (it.at) row.append(el('span', 'bw-when', ago(it.at)));
      box.append(row);
    });
    return box;
  }

  /* ---------------- Passwords and autofill panel ---------------- */
  function passwordsPanel(panelRoot, { head, button, close }) {
    let alive = true;
    const timers = new Set();
    const listBody = el('div', 'bw-pb');
    const addrBody = el('div', 'bw-pb');
    const confirm = el('div', 'bw-pc');
    const vaultNote = el('p', 'bw-pnote', 'Checking this Mac\'s secure storage.');
    panelRoot.dataset.panel = 'passwords';
    panelRoot.replaceChildren(
      head('Passwords and autofill', close),
      vaultNote,
      confirm,
      el('h4', 'bw-day', 'Saved passwords'),
      listBody,
      el('h4', 'bw-day', 'Addresses'),
      addrBody,
    );

    async function loadPasswords() {
      states.loading(listBody, 'Reading saved passwords');
      try {
        const d = await privacy.pwList();
        if (!alive) return;
        if (d && d.ok === false) throw new Error(d.error || 'no reason given');
        vaultNote.textContent = vaultLine({ available: d.available === true });
        const rows = passwordRows(d.entries);
        const never = neverRows(d.never);
        const nodes = [];
        if (!rows.length) nodes.push(el('p', 'bw-pnote', 'No saved passwords yet. When you sign in to a site, the bar above the page offers to save it.'));
        rows.forEach((r) => nodes.push(passwordRow(r)));
        if (never.length) {
          nodes.push(el('h4', 'bw-day', 'Never saved for these sites'));
          never.forEach((o) => nodes.push(neverRow(o)));
        }
        states.populated(listBody, nodes);
      } catch (err) {
        if (!alive || isAbort(err)) return;
        states.error(listBody, err, { retry: () => loadPasswords(), title: 'Could not read the saved passwords' });
      }
    }

    function passwordRow(r) {
      const row = el('div', 'bw-pwrow');
      const info = el('div', 'bw-pwinfo');
      info.append(el('span', 'bw-pwhost', r.host), el('span', 'bw-pwuser', r.readable ? r.username || '(no username)' : '(cannot be read)'));
      const shown = el('code', 'bw-pwshown');
      shown.hidden = true;
      const acts = el('div', 'ac');
      const slot = el('div', 'bw-pc');
      const show = el('button', 'os-btn', 'SHOW');
      show.type = 'button';
      show.disabled = !r.readable;
      show.dataset.spec = 'Shows this password on screen for ten seconds, after you confirm here and in a macOS dialog. The AI cannot press that dialog.';
      show.addEventListener('click', () => {
        confirmHere(
          slot,
          'Show the saved password for ' + (r.username || 'this login') + ' on ' + r.host + '? A macOS dialog asks once more. It stays on screen for ten seconds.',
          async () => {
            const res = await privacy.pwReveal(r.id);
            if (!res || res.ok === false) throw new Error(res && res.error ? res.error : 'no answer');
            shown.textContent = res.password;
            shown.hidden = false;
            const t = setTimeout(() => {
              shown.textContent = '';
              shown.hidden = true;
              timers.delete(t);
            }, REVEAL_MS);
            timers.add(t);
          },
          { onDone: () => slot.replaceChildren() },
        );
      });
      const del = el('button', 'os-btn', 'DELETE');
      del.type = 'button';
      del.dataset.spec = 'Removes this saved login from this Mac after you confirm.';
      del.addEventListener('click', () => {
        confirmHere(
          slot,
          'Delete the saved password for ' + (r.username || 'this login') + ' on ' + r.host + '? This cannot be undone.',
          () => privacy.pwDelete(r.id),
          { onDone: (ok) => { slot.replaceChildren(); if (ok) { loadPasswords(); } } },
        );
      });
      acts.append(show, del);
      row.append(info, shown, acts, slot);
      return row;
    }

    function neverRow(origin) {
      const row = el('div', 'bw-pwrow');
      row.append(el('span', 'bw-pwhost', siteHost(origin)));
      const undo = el('button', 'os-btn', 'ASK AGAIN');
      undo.type = 'button';
      undo.dataset.spec = 'Lets Dourmouse offer to save passwords on this site again.';
      undo.addEventListener('click', async () => {
        await call(() => privacy.pwNeverRemove(origin), 'That was not changed');
        loadPasswords();
      });
      row.append(undo);
      return row;
    }

    async function loadAddresses() {
      states.loading(addrBody, 'Reading addresses');
      try {
        const d = await privacy.addrList();
        if (!alive) return;
        if (d && d.ok === false) throw new Error(d.error || 'no reason given');
        const rows = profileRows(d.profiles);
        const nodes = [el('p', 'bw-pnote', 'Names, addresses and phone numbers you type here are encrypted the same way and filled only when you press Fill address, into empty fields.')];
        rows.forEach((p) => nodes.push(profileRow(p)));
        nodes.push(profileForm(null));
        states.populated(addrBody, nodes);
      } catch (err) {
        if (!alive || isAbort(err)) return;
        states.error(addrBody, err, { retry: () => loadAddresses(), title: 'Could not read the addresses' });
      }
    }

    function profileRow(p) {
      const row = el('div', 'bw-pwrow');
      const info = el('div', 'bw-pwinfo');
      info.append(el('span', 'bw-pwhost', p.label || 'Address'), el('span', 'bw-pwuser', [p.name, p.line1, p.city].filter(Boolean).join(', ')));
      const acts = el('div', 'ac');
      const slot = el('div', 'bw-pc');
      const edit = el('button', 'os-btn', 'EDIT');
      edit.type = 'button';
      edit.addEventListener('click', () => slot.replaceChildren(profileForm(p)));
      const del = el('button', 'os-btn', 'DELETE');
      del.type = 'button';
      del.addEventListener('click', () => {
        confirmHere(slot, 'Delete the address "' + (p.label || 'Address') + '"? This cannot be undone.', () => privacy.addrDelete(p.id), {
          onDone: (ok) => { slot.replaceChildren(); if (ok) loadAddresses(); },
        });
      });
      acts.append(edit, del);
      row.append(info, acts, slot);
      return row;
    }

    function profileForm(existing) {
      const form = el('form', 'bw-addrform');
      form.noValidate = true;
      const inputs = {};
      ADDRESS_FIELDS.forEach((f) => {
        const lab = el('label', 'bw-af');
        lab.append(el('span', '', f.label));
        const inp = el('input');
        inp.type = 'text';
        inp.autocomplete = 'off';
        inp.maxLength = 200;
        if (f.hint) inp.placeholder = f.hint;
        inp.value = existing ? existing[f.key] || '' : '';
        inputs[f.key] = inp;
        lab.append(inp);
        form.append(lab);
      });
      const msg = el('div', 'bw-pnote');
      const save = el('button', 'os-btn', existing ? 'SAVE CHANGES' : 'ADD ADDRESS');
      save.type = 'submit';
      form.append(msg, save);
      form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const values = {};
        ADDRESS_FIELDS.forEach((f) => { values[f.key] = inputs[f.key].value; });
        const v = addressFromForm(values);
        if (!v.ok) {
          msg.textContent = v.reason;
          return;
        }
        const r = await call(() => privacy.addrSave(v.profile, existing ? existing.id : undefined), 'The address was not saved');
        if (r && r.ok) loadAddresses();
      });
      return form;
    }

    loadPasswords();
    loadAddresses();
    return {
      dispose: () => {
        alive = false;
        timers.forEach((t) => clearTimeout(t));
        timers.clear();
        /* a password that was on screen goes with the panel */
        panelRoot.querySelectorAll('.bw-pwshown').forEach((n) => { n.textContent = ''; });
      },
    };
  }

  function dispose() {
    disposers.forEach((fn) => fn());
    disposers.clear();
    guardTimers.forEach((t) => clearTimeout(t));
    guardTimers.clear();
  }

  return {
    root,
    update,
    model: () => model,
    panels: { sites: sitesPanel, passwords: passwordsPanel },
    dispose,
  };
}
