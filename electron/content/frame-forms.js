// Dourmouse native shell -- login and address form helper for pages in the browser pane (Phase B2).
//
// Registered by main.js as a session preload for the pane partition only. It runs in the
// page's frame in an ISOLATED world: the page's own scripts cannot see its variables, cannot
// reach `ipcRenderer`, and cannot call anything here. It makes no network request and writes
// nothing to disk. All it does:
//
//   1. Reports, without any value, whether the page has a login form or address fields
//      ({ pw, user, addr }), so the console can offer a Fill button.
//   2. Notices a real (trusted) login submission and sends the username and password to the
//      main process, once, to ask the owner whether to save it. The main process decides
//      everything: which origin it came from is taken from the frame's real address, not from
//      anything this script says.
//   3. Reports a real click on a login or address field, so the shell can show a native menu
//      of saved logins at the pointer.
//   4. When the main process sends a fill message, puts the values into the fields. It fills
//      only the top page, only if the page is still at the origin the message names, and it
//      NEVER submits anything.
//
// Frames other than the top page do nothing at all.

"use strict";

const { ipcRenderer } = require("electron");

if (process.isMainFrame) {
  const ADDRESS_TOKENS = {
    name: ["name", "given-name", "family-name", "additional-name", "cc-name"],
    email: ["email"],
    phone: ["tel", "tel-national", "tel-local"],
    line1: ["address-line1", "street-address"],
    line2: ["address-line2"],
    city: ["address-level2"],
    state: ["address-level1"],
    zip: ["postal-code"],
    country: ["country", "country-name"],
  };
  const NAME_HINTS = {
    name: /^(full.?name|name|first.?name|last.?name|given.?name|family.?name)$/i,
    email: /e.?mail/i,
    phone: /(phone|mobile|tel)/i,
    line1: /(address.?1|address.?line.?1|street|addr(ess)?$)/i,
    line2: /(address.?2|address.?line.?2|apt|suite|unit)/i,
    city: /^(city|town|locality)$/i,
    state: /^(state|province|region)$/i,
    zip: /(zip|postal|post.?code)/i,
    country: /^country/i,
  };
  const SUBMITTISH = /(sign.?in|log.?in|submit|continue|next|register|sign.?up|create|enter)/i;
  const TEXTISH = new Set(["", "text", "email", "tel", "search", "url"]);

  const isVisible = (el) => {
    if (!el || !el.isConnected) return false;
    if (el.type === "hidden") return false;
    const style = getComputedStyle(el);
    if (style.visibility === "hidden" || style.display === "none") return false;
    return el.getClientRects().length > 0;
  };
  const editable = (el) => !el.disabled && !el.readOnly;
  const inputs = () => Array.from(document.querySelectorAll("input")).filter((el) => el instanceof HTMLInputElement);
  const passwordFields = (scope) =>
    Array.from((scope || document).querySelectorAll('input[type="password"]')).filter((el) => isVisible(el) && editable(el));

  // The text field a person would call "username": the closest visible text-like input that
  // comes BEFORE the password field, in the same form when there is one.
  function usernameFieldFor(pw) {
    const scope = pw.form || document;
    const all = Array.from(scope.querySelectorAll("input")).filter(
      (el) => el instanceof HTMLInputElement && TEXTISH.has((el.getAttribute("type") || "").toLowerCase()) && isVisible(el) && editable(el),
    );
    const before = all.filter((el) => el.compareDocumentPosition(pw) & Node.DOCUMENT_POSITION_FOLLOWING);
    if (!before.length) return null;
    const tagged = before.filter((el) => /(^|\s)(username|email)(\s|$)/i.test(el.autocomplete || ""));
    const pool = tagged.length ? tagged : before;
    return pool[pool.length - 1];
  }

  // A page with a username field and no password field yet (the first step of a two-step
  // sign-in) still gets a fill for the username.
  function loneUsernameField() {
    const all = inputs().filter((el) => isVisible(el) && editable(el));
    return (
      all.find((el) => /(^|\s)(username|email)(\s|$)/i.test(el.autocomplete || "") && TEXTISH.has((el.getAttribute("type") || "").toLowerCase())) || null
    );
  }

  function addressKind(el) {
    if (!(el instanceof HTMLInputElement) || !isVisible(el) || !editable(el)) return "";
    const type = (el.getAttribute("type") || "").toLowerCase();
    if (!TEXTISH.has(type) || type === "search") return "";
    const tokens = (el.autocomplete || "").toLowerCase().split(/\s+/);
    for (const kind of Object.keys(ADDRESS_TOKENS)) {
      if (ADDRESS_TOKENS[kind].some((t) => tokens.includes(t))) return kind;
    }
    const label = el.name || el.id || "";
    if (label) {
      for (const kind of Object.keys(NAME_HINTS)) {
        if (NAME_HINTS[kind].test(label)) return kind;
      }
    }
    return "";
  }
  const addressFields = () => inputs().filter((el) => addressKind(el));

  // -------------------------- 1. what the page has -------------------------- //
  let lastReport = "";
  let reportTimer = 0;
  function report() {
    reportTimer = 0;
    const pws = passwordFields();
    const state = {
      pw: pws.length > 0,
      user: pws.length > 0 ? Boolean(usernameFieldFor(pws[0])) : Boolean(loneUsernameField()),
      addr: addressFields().length >= 2,
    };
    const key = `${state.pw}|${state.user}|${state.addr}`;
    if (key === lastReport) return;
    lastReport = key;
    ipcRenderer.send("dm:forms", state);
  }
  function scheduleReport() {
    if (reportTimer) return;
    reportTimer = setTimeout(report, 350);
  }
  document.addEventListener("DOMContentLoaded", scheduleReport, true);
  window.addEventListener("load", scheduleReport, true);
  document.addEventListener("focusin", scheduleReport, true);
  try {
    new MutationObserver(scheduleReport).observe(document, { childList: true, subtree: true, attributes: true, attributeFilter: ["type", "style", "class", "hidden"] });
  } catch {
    /* the document is not ready yet; DOMContentLoaded reports instead */
  }

  // -------------------------- 2. a real login -------------------------- //
  let lastSent = "";
  let lastSentAt = 0;
  function snapshot(scope) {
    const pws = passwordFields(scope).filter((el) => el.value);
    if (!pws.length) return null;
    const userField = usernameFieldFor(pws[0]);
    // With several password fields (sign-up with a confirmation, change password) the last
    // one holds the password that will be kept.
    return { username: userField ? userField.value : "", password: pws[pws.length - 1].value };
  }
  function sendLogin(scope) {
    const snap = snapshot(scope);
    if (!snap) return;
    const key = `${snap.username}\u0000${snap.password}`;
    const t = Date.now();
    if (key === lastSent && t - lastSentAt < 2500) return;
    lastSent = key;
    lastSentAt = t;
    ipcRenderer.send("dm:pw-submit", snap);
  }
  // Only events the browser made from real input count. A page that fires a fake submit or
  // click is not a person logging in.
  document.addEventListener("submit", (e) => {
    if (!e.isTrusted) return;
    const form = e.target instanceof HTMLFormElement ? e.target : null;
    sendLogin(form || document);
  }, true);
  document.addEventListener("click", (e) => {
    if (!e.isTrusted || !(e.target instanceof Element)) return;
    const el = e.target.closest('button, input[type="submit"], a, [role="button"]');
    if (el) {
      // A form's own submit button already fires "submit". This catches sign-in screens that
      // never use a form: only a control that SAYS it signs in counts, not a "show password" eye.
      const label = `${el.getAttribute("aria-label") || ""} ${el.value || ""} ${el.textContent || ""}`.slice(0, 80);
      if (!el.closest("form") && SUBMITTISH.test(label)) sendLogin(document);
    }
  }, true);
  document.addEventListener("keydown", (e) => {
    if (!e.isTrusted || e.key !== "Enter" || !(e.target instanceof HTMLInputElement)) return;
    if (!e.target.closest("form")) sendLogin(document);
  }, true);

  // -------------------------- 3. a click on a field -------------------------- //
  let lastClickAt = 0;
  document.addEventListener("click", (e) => {
    if (!e.isTrusted || !(e.target instanceof HTMLInputElement)) return;
    const t = Date.now();
    if (t - lastClickAt < 600) return;
    const el = e.target;
    let kind = "";
    if ((el.getAttribute("type") || "").toLowerCase() === "password" && isVisible(el)) kind = "password";
    else if (addressKind(el)) kind = "address";
    else {
      const pws = passwordFields();
      const lone = loneUsernameField();
      if ((pws.length && usernameFieldFor(pws[0]) === el) || lone === el) kind = "username";
    }
    if (!kind) return;
    lastClickAt = t;
    ipcRenderer.send("dm:field-click", { kind });
  }, true);

  // -------------------------- 4. a fill from the shell -------------------------- //
  function setValue(el, value) {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }
  ipcRenderer.on("dm:fill-login", (_event, msg) => {
    if (!msg || typeof msg !== "object" || msg.origin !== location.origin) return;
    const username = typeof msg.username === "string" ? msg.username : "";
    const password = typeof msg.password === "string" ? msg.password : "";
    const pws = passwordFields();
    if (pws.length) {
      const pw = pws.find((el) => !el.value) || pws[0];
      const user = usernameFieldFor(pw);
      if (user && username) setValue(user, username);
      if (password) setValue(pw, password);
    } else {
      const lone = loneUsernameField();
      if (lone && username) setValue(lone, username);
    }
  });
  ipcRenderer.on("dm:fill-address", (_event, msg) => {
    if (!msg || typeof msg !== "object" || msg.origin !== location.origin || !msg.fields || typeof msg.fields !== "object") return;
    const f = msg.fields;
    for (const el of addressFields()) {
      if (el.value) continue; // never overwrite what is already typed
      const kind = addressKind(el);
      const tokens = (el.autocomplete || "").toLowerCase().split(/\s+/);
      let value = "";
      if (kind === "name") {
        const parts = String(f.name || "").split(/\s+/).filter(Boolean);
        if (tokens.includes("given-name")) value = parts[0] || "";
        else if (tokens.includes("family-name")) value = parts.length > 1 ? parts[parts.length - 1] : "";
        else if (/first/i.test(el.name || el.id || "")) value = parts[0] || "";
        else if (/last|family/i.test(el.name || el.id || "")) value = parts.length > 1 ? parts[parts.length - 1] : "";
        else value = f.name || "";
      } else if (kind) {
        value = f[kind] || "";
        if (kind === "line1" && tokens.includes("street-address") && f.line2) value = `${value}, ${f.line2}`;
      }
      if (value) setValue(el, String(value));
    }
  });
}
