// Startup sequence (backlog #6): a brief loading animation on every app
// open, then a real check of the three sign-in states the user asked
// about — Claude CLI, Codex CLI (/api/connections, already existed and
// already probes both honestly), and Google (/api/auth/status's real
// `me` field, not just OAuth-client config). If anything is missing, a
// popup shows the EXACT command to paste — no manual research needed.
// Injected the same guarded, file-exists-checked way as the Spotify
// widget (see dourmouse/webui.py::_serve_static).
(function () {
  "use strict";

  function el(tag, attrs, children) {
    var e = document.createElement(tag);
    for (var k in attrs || {}) e.setAttribute(k, attrs[k]);
    (children || []).forEach(function (c) {
      e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return e;
  }

  function injectStyle() {
    var css = document.createElement("style");
    css.textContent =
      "#dm-startup-overlay{position:fixed;inset:0;z-index:99999;display:flex;" +
      "align-items:center;justify-content:center;background:#F7F5F1;" +
      "transition:opacity .25s ease;font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Inter',sans-serif}" +
      "#dm-startup-overlay.dm-hidden{opacity:0;pointer-events:none}" +
      "#dm-startup-spinner{width:28px;height:28px;border-radius:50%;" +
      "border:2.5px solid #E4DFD5;border-top-color:#A8481E;animation:dm-spin .8s linear infinite}" +
      "@keyframes dm-spin{to{transform:rotate(360deg)}}" +
      "#dm-signin-popup{position:fixed;inset:0;z-index:99998;display:none;" +
      "align-items:center;justify-content:center;background:rgba(32,30,26,.35)}" +
      "#dm-signin-popup.dm-open{display:flex}" +
      "#dm-signin-card{background:#FFFFFF;border:1px solid #E4DFD5;border-radius:14px;" +
      "padding:24px;max-width:420px;width:92vw;box-shadow:0 12px 32px -8px rgba(30,24,12,.18);" +
      "font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Inter',sans-serif;color:#221F1B}" +
      "#dm-signin-card h2{font-size:16px;font-weight:600;color:#201E1A;margin-bottom:10px}" +
      "#dm-signin-card .dm-item{margin-bottom:14px}" +
      "#dm-signin-card .dm-label{font-size:13px;font-weight:600;color:#3A3733;margin-bottom:4px}" +
      "#dm-signin-card code{display:block;background:#F7F5F1;border:1px solid #E4DFD5;" +
      "border-radius:6px;padding:8px 10px;font-size:13px;font-family:ui-monospace,monospace;" +
      "cursor:pointer;user-select:all}" +
      "#dm-signin-card a.dm-google-link{display:inline-block;margin-top:2px;color:#A8481E;font-size:13px;font-weight:600}" +
      "#dm-signin-card button{margin-top:6px;border:none;background:#A8481E;color:#fff;" +
      "border-radius:8px;padding:8px 16px;font-size:13px;font-weight:600;cursor:pointer}";
    document.head.appendChild(css);
  }

  function buildOverlay() {
    var overlay = el("div", { id: "dm-startup-overlay" }, [el("div", { id: "dm-startup-spinner" })]);
    document.body.appendChild(overlay);
    return overlay;
  }

  function buildPopup(items) {
    var card = el("div", { id: "dm-signin-card" }, [
      el("h2", {}, ["Finish signing in"]),
    ]);
    items.forEach(function (it) {
      var block = el("div", { class: "dm-item" }, [el("div", { class: "dm-label" }, [it.label])]);
      if (it.command) {
        var code = el("code", { title: "Click to copy" }, [it.command]);
        code.addEventListener("click", function () {
          if (navigator.clipboard) navigator.clipboard.writeText(it.command);
        });
        block.appendChild(code);
      } else if (it.href) {
        block.appendChild(el("a", { class: "dm-google-link", href: it.href }, ["Sign in with Google →"]));
      }
      card.appendChild(block);
    });
    var dismiss = el("button", {}, ["Continue anyway"]);
    var popup = el("div", { id: "dm-signin-popup" }, [card]);
    dismiss.addEventListener("click", function () {
      popup.classList.remove("dm-open");
    });
    card.appendChild(dismiss);
    document.body.appendChild(popup);
    return popup;
  }

  function checkSignins() {
    return Promise.all([
      fetch("/api/connections").then(function (r) { return r.json(); }).catch(function () { return {}; }),
      fetch("/api/auth/status").then(function (r) { return r.json(); }).catch(function () { return {}; }),
    ]).then(function (results) {
      var conns = results[0] || {};
      var auth = results[1] || {};
      var missing = [];
      if (conns.claude && conns.claude.ok === false) {
        missing.push({
          label: "Claude Code CLI — run this, then complete /login:",
          command: "claude",
        });
      }
      if (conns.codex && conns.codex.ok === false) {
        missing.push({
          label: "Codex CLI — install + sign in:",
          command: "npm i -g @openai/codex && codex login",
        });
      }
      if (auth && auth.configured && !auth.me) {
        missing.push({ label: "Google account — not signed in:", href: "/api/auth/google/start" });
      }
      return missing;
    });
  }

  function init() {
    injectStyle();
    var overlay = buildOverlay();
    checkSignins().then(function (missing) {
      overlay.classList.add("dm-hidden");
      setTimeout(function () {
        if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
      }, 300);
      if (missing.length) {
        var popup = buildPopup(missing);
        // rAF so the popup mounts after layout, matching the overlay's
        // own fade timing rather than flashing in mid-transition.
        requestAnimationFrame(function () { popup.classList.add("dm-open"); });
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
