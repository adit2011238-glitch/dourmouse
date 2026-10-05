// Stable element ids for the browser agent (phase C1).
//
// This file is injected by dourmouse/browser_agent.py into an ISOLATED WORLD of the page
// (CDP Page.createIsolatedWorld), never into the page's own JavaScript world. An isolated
// world shares the DOM with the page but has its own globals and its own copies of the
// built-in prototypes, so:
//   * a page script cannot read the id table, the counters or any function defined here
//     (there is no window property it could reach), and cannot forge or reassign an id;
//   * a page that patches Element.prototype or getBoundingClientRect does not change what
//     this code sees;
//   * nothing is written to the DOM: no data- attribute, no id, no class. The element to
//     id mapping lives in a Map and a WeakMap in this world only.
//
// A new document (any navigation that is not in-page) destroys the world with it, so every
// id dies on navigation. The Python side detects that, and also compares location.href.
//
// The redaction rules in secretField() are the same ones the old snapshot used (finding
// #163): password, hidden, one-time-code, card and address fields never have their value
// returned, and the same field-name patterns are still matched.
(() => {
  if (globalThis.__dmAgent) return globalThis.__dmAgent.token;

  const SELECTOR = [
    "input", "textarea", "select", "button", "a[href]", "summary",
    "[role='button']", "[role='link']", "[role='textbox']", "[role='searchbox']", "[role='combobox']",
    "[role='checkbox']", "[role='radio']", "[role='switch']", "[role='tab']", "[role='menuitem']",
    "[role='option']", "[contenteditable]:not([contenteditable='false'])",
  ].join(", ");

  const TEXT_INPUT_TYPES = new Set(["", "text", "search", "url", "tel", "email", "password", "number"]);
  const VALUE_SET_TYPES = new Set(["date", "datetime-local", "month", "time", "week", "color", "range"]);

  const state = {
    token: Math.random().toString(36).slice(2) + Date.now().toString(36),
    gen: 0,
    nextId: 1,
    byId: new Map(), // id (number) -> WeakRef(element)
    ofEl: new WeakMap(), // element -> id
    href: "",
  };

  // ---- the same redaction the previous snapshot applied (finding #163) ---------------
  function secretField(el) {
    const ac = (el.getAttribute("autocomplete") || "").toLowerCase().split(/\s+/);
    const idn = ((el.getAttribute("name") || "") + " " + (el.id || "")).toLowerCase();
    return el.type === "password" || el.type === "hidden"
      || ac.some((a) => /^(current-password|new-password|one-time-code|cc-.*|name|given-name|family-name|email|tel.*|street-address|address-line.*|postal-code|address-level.*|country.*)$/.test(a))
      || /pass|pwd|token|secret|otp|csrf|cc-?num|cvv|cvc/.test(idn);
  }

  function visible(el) {
    if (!el.isConnected) return false;
    const r = el.getBoundingClientRect();
    if (!(r.width > 0 && r.height > 0)) return false;
    if (typeof el.checkVisibility === "function") {
      return el.checkVisibility({ checkVisibilityCSS: true, visibilityProperty: true });
    }
    const cs = getComputedStyle(el);
    return cs.display !== "none" && cs.visibility !== "hidden";
  }

  function textOf(node) {
    return ((node && (node.innerText || node.textContent)) || "").trim();
  }

  function accessibleName(el) {
    const aria = el.getAttribute("aria-label");
    if (aria && aria.trim()) return aria.trim();
    const by = el.getAttribute("aria-labelledby");
    if (by) {
      const root = el.getRootNode();
      const parts = by.split(/\s+/).map((i) => textOf(root.getElementById ? root.getElementById(i) : null)).filter(Boolean);
      if (parts.length) return parts.join(" ");
    }
    if (el.labels && el.labels.length) {
      const l = Array.from(el.labels).map(textOf).filter(Boolean).join(" ");
      if (l) return l;
    }
    const ph = el.getAttribute("placeholder");
    if (ph && ph.trim()) return ph.trim();
    const tag = el.tagName.toLowerCase();
    if (tag === "input" && ["button", "submit", "reset"].includes(el.type) && el.value) return el.value;
    const txt = textOf(el);
    if (txt) return txt;
    const title = el.getAttribute("title");
    if (title && title.trim()) return title.trim();
    const img = el.querySelector && el.querySelector("img[alt]");
    if (img && img.getAttribute("alt")) return img.getAttribute("alt");
    return el.getAttribute("href") || "";
  }

  function describe(el) {
    const tag = el.tagName.toLowerCase();
    let name = accessibleName(el).replace(/\s+/g, " ").slice(0, 60);
    if (!name && (tag === "input" || tag === "textarea")) name = "<unlabeled " + tag + ">";
    if (!name && tag === "button") name = "<unlabeled button>";
    return { tag, name };
  }

  function collect(root, out) {
    for (const el of root.querySelectorAll("*")) {
      if (el.matches(SELECTOR)) out.push(el);
      if (el.shadowRoot) collect(el.shadowRoot, out);
    }
  }

  function isTextEntry(el) {
    const tag = el.tagName.toLowerCase();
    if (tag === "textarea") return true;
    if (tag === "input") return TEXT_INPUT_TYPES.has((el.type || "").toLowerCase());
    return false;
  }

  function idFor(el, fresh) {
    let id = state.ofEl.get(el);
    if (id === undefined) {
      id = state.nextId++;
      state.ofEl.set(el, id);
      state.byId.set(id, new WeakRef(el));
      fresh.push(id);
    }
    return id;
  }

  // ---- snapshot ------------------------------------------------------------------------
  function snapshot(p) {
    state.gen += 1;
    state.href = location.href;
    if (typeof p.start === "number" && p.start > state.nextId) state.nextId = p.start;
    const max = Math.max(1, Math.min(300, p.max || 60));
    const all = [];
    collect(document, all);
    const items = [];
    const fresh = [];
    let hiddenOmitted = 0;
    let total = 0;
    for (const el of all) {
      // A nested contenteditable is part of its editing host, not a second field.
      if (el.hasAttribute("contenteditable") && el.parentElement && el.parentElement.isContentEditable) continue;
      const tag = el.tagName.toLowerCase();
      const type = tag === "input" ? (el.type || "text").toLowerCase() : "";
      if (type === "hidden") {
        // Listed as before (name only, value redacted, no id: nothing can act on it).
        total += 1;
        if (items.length < max) {
          const d = describe(el);
          if (d.name) items.push({ id: 0, t: tag, type, name: d.name, val: el.value ? "[hidden]" : "", flags: [] });
        }
        continue;
      }
      if (!visible(el)) { hiddenOmitted += 1; continue; }
      const d = describe(el);
      if (!d.name) continue;
      total += 1;
      if (items.length >= max) continue;
      const secret = secretField(el);
      let val = "";
      if (tag === "select") {
        const opt = el.selectedOptions && el.selectedOptions[0];
        val = secret ? (el.value ? "[hidden]" : "") : (opt ? textOf(opt) : "");
      } else if (el.value !== undefined && el.value !== null && !(type === "checkbox" || type === "radio" || type === "button" || type === "submit" || type === "reset")) {
        val = secret ? (el.value ? "[hidden]" : "") : String(el.value);
      }
      const flags = [];
      if (el.disabled || el.getAttribute("aria-disabled") === "true") flags.push("disabled");
      if (el.readOnly) flags.push("readonly");
      if (el.required || el.getAttribute("aria-required") === "true") flags.push("required");
      if (type === "checkbox" || type === "radio") flags.push(el.checked ? "checked" : "unchecked");
      else if (el.getAttribute("aria-checked")) flags.push(el.getAttribute("aria-checked") === "true" ? "checked" : "unchecked");
      if (el.getAttribute("aria-expanded")) flags.push(el.getAttribute("aria-expanded") === "true" ? "expanded" : "collapsed");
      if (el.getAttribute("aria-selected") === "true") flags.push("selected");
      const item = { id: idFor(el, fresh), t: tag, type, name: d.name, val: val.slice(0, 40), flags };
      const role = el.getAttribute("role");
      if (role) item.role = role;
      if (tag === "select") item.options = Array.from(el.options).slice(0, 12).map((o) => textOf(o).slice(0, 30));
      items.push(item);
    }
    return {
      gen: state.gen, href: state.href, title: document.title || "", items, total, hiddenOmitted,
      nextId: state.nextId, fresh,
    };
  }

  // ---- resolving an id back to its element ------------------------------------------
  function refuse(code, id, el, extra) {
    const d = el ? describe(el) : { name: "" };
    return { ok: false, code, id, name: d.name, ...(extra || {}) };
  }

  function deepFromPoint(x, y) {
    let el = document.elementFromPoint(x, y);
    while (el && el.shadowRoot) {
      const inner = el.shadowRoot.elementFromPoint(x, y);
      if (!inner || inner === el) break;
      el = inner;
    }
    return el;
  }

  // document.activeElement stops at a shadow host; the focused control is inside it.
  function deepActive() {
    let a = document.activeElement;
    while (a && a.shadowRoot && a.shadowRoot.activeElement) a = a.shadowRoot.activeElement;
    return a;
  }

  function within(host, node) {
    for (let n = node; n; n = n.parentNode || n.host) if (n === host) return true;
    return false;
  }

  function lookup(p) {
    const id = p.id;
    if (state.gen === 0) return { err: refuse("unknown", id, null) };
    if (location.href !== state.href) return { err: refuse("navigated", id, null, { was: state.href, now: location.href, gen: state.gen }) };
    const ref = state.byId.get(id);
    if (!ref) return { err: refuse("unknown", id, null) };
    const el = ref.deref();
    if (!el || !el.isConnected) return { err: refuse("gone", id, el || null, { gen: state.gen }) };
    if (!visible(el)) return { err: refuse("hidden", id, el, { gen: state.gen }) };
    return { el };
  }

  function placeCaretAtEnd(el) {
    if (el.isContentEditable) {
      const sel = getSelection();
      sel.selectAllChildren(el);
      sel.collapseToEnd();
    } else if (isTextEntry(el)) {
      // Some input types (email, number) do not support the selection API and throw: for those
      // the caret is wherever focus left it, which is the end for a fresh focus.
      try { el.setSelectionRange(el.value.length, el.value.length); } catch (_) { /* unsupported type */ }
    }
  }

  function selectAllContent(el) {
    if (el.isContentEditable) getSelection().selectAllChildren(el);
    else el.select();
  }

  function prepare(p) {
    const found = lookup(p);
    if (found.err) return found.err;
    const el = found.el;
    const op = p.op;
    const d = describe(el);
    const base = { ok: true, id: p.id, tag: d.tag, name: d.name };
    if (op === "text") {
      const tag = d.tag;
      if (tag === "input" || tag === "textarea" || tag === "select") {
        if (secretField(el)) return { ...base, text: el.value ? "[hidden]" : "" };
        return { ...base, text: tag === "select" ? textOf(el.selectedOptions[0]) : String(el.value) };
      }
      return { ...base, text: (el.innerText || el.textContent || "").slice(0, 4000) };
    }
    if (el.disabled || el.getAttribute("aria-disabled") === "true") return refuse("disabled", p.id, el);
    el.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });

    if (op === "click") {
      const r = el.getBoundingClientRect();
      const x = Math.min(Math.max(r.left + r.width / 2, 1), innerWidth - 1);
      const y = Math.min(Math.max(r.top + r.height / 2, 1), innerHeight - 1);
      const hit = deepFromPoint(x, y);
      if (!hit || !(within(el, hit) || hit.control === el)) {
        // finding #168: the page controls the id; keep it to a short, single-line token.
        const safeId = hit && hit.id ? String(hit.id).replace(/[^A-Za-z0-9_.:-]/g, "").slice(0, 30) : "";
        const h = hit ? String(hit.tagName).toLowerCase().replace(/[^a-z0-9-]/g, "").slice(0, 20) + (safeId ? "#" + safeId : "") : "nothing";
        return refuse("obscured", p.id, el, { by: h });
      }
      return { ...base, x, y };
    }
    if (op === "focus") { el.focus(); return base; }
    if (op === "fill" || op === "type") {
      const tag = d.tag;
      const type = tag === "input" ? (el.type || "text").toLowerCase() : "";
      if (el.readOnly) return refuse("readonly", p.id, el);
      if (tag === "input" && VALUE_SET_TYPES.has(type)) {
        if (op === "type") return refuse("wrongtype", p.id, el, { detail: "a " + type + " input takes a value, use browser_fill" });
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
        setter.call(el, String(p.value));
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
        return { ...base, done: true };
      }
      const roleOnly = !isTextEntry(el) && !el.isContentEditable;
      const editable = !roleOnly || el.getAttribute("role") === "textbox"
        || el.getAttribute("role") === "searchbox" || el.getAttribute("role") === "combobox";
      if (!editable) return refuse("wrongtype", p.id, el, { detail: "it is not a text field (a " + (type ? "input " + type : tag) + ")" });
      // Phase C2: an editor surface (a role=textbox that is neither a field nor contenteditable, such
      // as a canvas editor fed by a hidden text frame) has no value to fill or select: it takes typed
      // text after a click, and only browser_type does that.
      if (roleOnly && op === "fill") return refuse("wrongtype", p.id, el, { detail: "it is an editor surface with no value to fill (use browser_type, which clicks into it and types)" });
      if (roleOnly && p.clear) return refuse("wrongtype", p.id, el, { detail: "an editor surface cannot be cleared from here (select its text with keys first)" });
      const multiline = multilineOf(el);
      if (roleOnly) {
        el.focus();
        const a = deepActive();
        if (within(el, a)) return { ...base, focused: true, multiline };
        if (a && (a.tagName === "IFRAME" || a.tagName === "FRAME")) return { ...base, focused: true, frame: true, multiline: true };
        const r = el.getBoundingClientRect();
        // Not focusable by script: the editor wants a click where the text goes. The end of the
        // surface is the safest place for a caret: the agent appends, it does not insert mid-text.
        return { ...base, focused: false, multiline, clickAt: {
          x: Math.min(Math.max(r.left + r.width / 2, 1), innerWidth - 1),
          y: Math.min(Math.max(r.bottom - Math.min(8, r.height / 2), 1), innerHeight - 1),
        } };
      }
      el.focus();
      if (op === "fill" || p.clear) selectAllContent(el);
      else placeCaretAtEnd(el);
      return { ...base, focused: within(el, deepActive()), multiline };
    }
    if (op === "select") {
      if (d.tag !== "select") return refuse("wrongtype", p.id, el, { detail: "it is not a <select> (a " + d.tag + ")" });
      const want = String(p.value);
      const opts = Array.from(el.options);
      let o = opts.find((x) => x.value === want) || opts.find((x) => textOf(x) === want)
        || opts.find((x) => textOf(x).toLowerCase() === want.toLowerCase());
      if (!o) return refuse("nooption", p.id, el, { options: opts.slice(0, 20).map((x) => textOf(x).slice(0, 40)) });
      if (o.disabled) return refuse("disabled", p.id, el, { detail: "that option is disabled" });
      el.value = o.value;
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return { ...base, chose: textOf(o) };
    }
    return { ok: false, code: "badop", id: p.id, name: "" };
  }

  // Phase C2: whether a line break typed into this element stays a line break (an input method's
  // text never presses Enter, so it cannot submit anything either way).
  function multilineOf(el) {
    return el.tagName === "TEXTAREA" || el.isContentEditable || el.getAttribute("aria-multiline") === "true";
  }

  // What has the keyboard. Phase C2: an editor such as Google Docs takes its typing through a
  // hidden iframe, so a focused frame is followed one level in when it is same-origin.
  function focusState() {
    const a = deepActive();
    if (!a || a === document.body) return { has: false };
    const tag = a.tagName.toLowerCase();
    if (tag === "iframe" || tag === "frame") {
      let doc = null;
      try { doc = a.contentDocument; } catch (_) { doc = null; }
      if (!doc) return { has: true, tag, frame: true, inner: "", editable: true, multiline: true }; // cross-origin: cannot look in
      let i = doc.activeElement;
      while (i && i.shadowRoot && i.shadowRoot.activeElement) i = i.shadowRoot.activeElement;
      const designMode = String(doc.designMode).toLowerCase() === "on";
      if (!i || i === doc.body) {
        const ce = Boolean(doc.body && doc.body.isContentEditable) || designMode;
        return { has: true, tag, frame: true, inner: ce ? "contenteditable body" : "", editable: ce, multiline: ce };
      }
      const itag = i.tagName.toLowerCase();
      const ok = isTextEntry(i) || i.isContentEditable || designMode;
      return { has: true, tag, frame: true, inner: i.isContentEditable ? "contenteditable " + itag : itag, editable: ok, multiline: ok && (multilineOf(i) || designMode) };
    }
    return { has: true, tag, editable: isTextEntry(a) || a.isContentEditable, multiline: multilineOf(a) };
  }

  // Is element `id` (still) the one with the keyboard?
  function focusCheck(p) {
    const found = lookup(p);
    if (found.err) return found.err;
    return { ok: within(found.el, deepActive()) };
  }

  const api = {
    token: state.token,
    run(op, p) {
      if (op === "snapshot") return snapshot(p || {});
      if (op === "prepare") return prepare(p || {});
      if (op === "focusState") return focusState();
      if (op === "focusCheck") return focusCheck(p || {});
      if (op === "ping") return { gen: state.gen, href: state.href, now: location.href };
      throw new Error("unknown op " + op);
    },
  };
  Object.defineProperty(globalThis, "__dmAgent", { value: api, enumerable: false, configurable: false, writable: false });
  return state.token;
})()
