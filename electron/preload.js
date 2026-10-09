// Dourmouse native shell -- preload script.
//
// Keeps the EXACT `window.pywebview.api.*` global shape ui/*.html already
// calls today (verified by grepping every ui/*.html for real call sites
// before writing this -- open_agent [index.html], open_all_hands
// [index.html, all_hands.html], open_external [setup.html, login.html] were
// the only three with a real caller then; open_study and open_project
// [console.html] were added after; open_map/navigate/window_state/
// set_window_state/screen_size/list_running_apps/split_with_app/
// split_in_window have none and are deliberately NOT exposed here -- see
// the plan's "don't port dead code by default" note). This means every
// existing page that already feature-detects
//   typeof window.pywebview !== "undefined" && window.pywebview.api && ...
// needs ZERO changes for this shell swap -- confirmed live in the Electron
// migration spike before this file was written for real.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("pywebview", {
  api: {
    open_agent: (name) => ipcRenderer.invoke("bridge:open_agent", name),
    open_all_hands: (runId, goal) => ipcRenderer.invoke("bridge:open_all_hands", runId, goal),
    open_external: (url) => ipcRenderer.invoke("bridge:open_external", url),
    // ui/console.html feature-detects these two (STUDY, PROJECTS): without them it falls back to
    // window.open, which the shell refuses for every URL (finding A-2).
    open_study: () => ipcRenderer.invoke("bridge:open_study"),
    open_project: (tabId, name) => ipcRenderer.invoke("bridge:open_project", tabId, name),
  },
});

// Finding #116 (OS-3): the console's browser pane drives the real
// BrowserView through these, instead of an iframe plus a rewriting proxy.
contextBridge.exposeInMainWorld("dourmouseShell", {
  pane: {
    navigate: (url) => ipcRenderer.invoke("pane:navigate", url),
    nav: (what) => ipcRenderer.invoke("pane:nav", what),
    bounds: (rect) => ipcRenderer.invoke("pane:bounds", rect),
    show: () => ipcRenderer.invoke("pane:show"),
    hide: () => ipcRenderer.invoke("pane:hide"),
    state: () => ipcRenderer.invoke("pane:state"),
    onState: (cb) => ipcRenderer.on("pane:state", (_evt, s) => cb(s)),
    // Phase B1: tabs, find, zoom, print, downloads. Every one is a plain request to
    // main.js, which validates it; the page never gets a handle to the pane. The two
    // subscriptions below return the function that removes them, so a screen can
    // clean up on unmount (onState above cannot, which is why host.js fans it out).
    screen: (active) => ipcRenderer.invoke("pane:screen", active === true),
    newTab: (url) => ipcRenderer.invoke("pane:tab-new", url),
    closeTab: (id) => ipcRenderer.invoke("pane:tab-close", id),
    selectTab: (id) => ipcRenderer.invoke("pane:tab-select", id),
    reopenTab: () => ipcRenderer.invoke("pane:tab-reopen"),
    find: (text, opts) => ipcRenderer.invoke("pane:find", text, opts),
    findStop: () => ipcRenderer.invoke("pane:find-stop"),
    focusPage: () => ipcRenderer.invoke("pane:focus-page"),
    zoom: (action) => ipcRenderer.invoke("pane:zoom", action),
    print: (opts) => ipcRenderer.invoke("pane:print", opts),
    downloads: () => ipcRenderer.invoke("pane:downloads"),
    downloadAction: (id, action) => ipcRenderer.invoke("pane:download-action", id, action),
    clearDownloads: () => ipcRenderer.invoke("pane:downloads-clear"),
    onDownloads: (cb) => {
      const h = (_evt, list) => cb(list);
      ipcRenderer.on("pane:downloads", h);
      return () => ipcRenderer.removeListener("pane:downloads", h);
    },
    onCommand: (cb) => {
      const h = (_evt, cmd) => cb(cmd);
      ipcRenderer.on("pane:command", h);
      return () => ipcRenderer.removeListener("pane:command", h);
    },
    // Phase C2: who has the pane, the owner or the model. Stop cancels what the model is doing;
    // Take control also keeps it from starting anything until Release. main.js answers only the
    // console window's top frame; the pane bridge has no route for any of the three.
    control: {
      state: () => ipcRenderer.invoke("control:state"),
      stop: () => ipcRenderer.invoke("control:stop"),
      take: () => ipcRenderer.invoke("control:take"),
      release: () => ipcRenderer.invoke("control:release"),
      onUpdate: (cb) => {
        const h = (_evt, s) => cb(s);
        ipcRenderer.on("pane:control", h);
        return () => ipcRenderer.removeListener("pane:control", h);
      },
    },
    // Phase B2: site permissions, saved passwords and address autofill. Each call is a request
    // the main process validates and answers only for the console window (it checks the sender).
    // Nothing here hands the console a password except `reveal`, which main.js puts behind a
    // native confirmation dialog.
    privacy: {
      state: () => ipcRenderer.invoke("pane:privacy"),
      onUpdate: (cb) => {
        const h = (_evt, s) => cb(s);
        ipcRenderer.on("pane:privacy", h);
        return () => ipcRenderer.removeListener("pane:privacy", h);
      },
      permAnswer: (id, decision) => ipcRenderer.invoke("pane:perm-answer", id, decision),
      sites: () => ipcRenderer.invoke("site:perms"),
      siteSet: (origin, permission, decision) => ipcRenderer.invoke("site:perm-set", origin, permission, decision),
      siteForget: (origin) => ipcRenderer.invoke("site:perm-forget", origin),
      sitesClear: () => ipcRenderer.invoke("site:perms-clear"),
      pwList: () => ipcRenderer.invoke("pw:list"),
      pwDelete: (id) => ipcRenderer.invoke("pw:delete", id),
      pwNeverRemove: (origin) => ipcRenderer.invoke("pw:never-remove", origin),
      pwSaveAnswer: (id, answer) => ipcRenderer.invoke("pw:save-answer", id, answer),
      pwFill: (entryId) => ipcRenderer.invoke("pw:fill", entryId),
      pwReveal: (id) => ipcRenderer.invoke("pw:reveal", id),
      addrList: () => ipcRenderer.invoke("addr:list"),
      addrSave: (profile, id) => ipcRenderer.invoke("addr:save", profile, id),
      addrDelete: (id) => ipcRenderer.invoke("addr:delete", id),
      addrFill: (id) => ipcRenderer.invoke("addr:fill", id),
      // Phase B3: what the engine says about Widevine (non-secret, shown in Site settings).
      drm: () => ipcRenderer.invoke("drm:status"),
    },
    // Phase B3: extensions, profiles and import from Chrome. Each call is a request the main process
    // validates and answers only for the console window. The ones that add code, import data or
    // delete a profile end in a NATIVE macOS dialog that a script driving the console cannot press;
    // none of them takes a path from the page (the folders and files are chosen in the native dialog).
    manage: {
      extensions: {
        list: () => ipcRenderer.invoke("ext:list"),
        add: () => ipcRenderer.invoke("ext:add"),
        enable: (id) => ipcRenderer.invoke("ext:enable", id),
        disable: (id) => ipcRenderer.invoke("ext:disable", id),
        remove: (id) => ipcRenderer.invoke("ext:remove", id),
      },
      profiles: {
        list: () => ipcRenderer.invoke("profile:list"),
        switchTo: (name) => ipcRenderer.invoke("profile:switch", name),
        create: (name) => ipcRenderer.invoke("profile:create", name),
        remove: (name) => ipcRenderer.invoke("profile:remove", name),
      },
      importFrom: {
        chrome: (want) => ipcRenderer.invoke("import:chrome", want),
        passwords: () => ipcRenderer.invoke("import:passwords"),
      },
    },
  },
});

// W1R-5: the console is hidden, not destroyed, when the red button is pressed (main.js). A page that was
// capturing would keep the microphone or camera open with no window to show it. This runs in the page's
// own world before any page script: it records every capture stream and speech recognizer the page opens
// and offers window.__dmStopMedia(), which main.js calls when it hides the console. The page is told first
// (a "dourmouse:console-hiding" event) so a screen can discard a half-made recording instead of acting on
// it; whatever is still live afterwards is stopped here. Returns how many captures it stopped.
try {
  contextBridge.executeInMainWorld({
    func: () => {
      if (window.__dmStopMedia) return;
      const tracks = new Set();
      const recognizers = new Set();
      const md = navigator.mediaDevices;
      if (md) {
        for (const name of ["getUserMedia", "getDisplayMedia"]) {
          const original = md[name];
          if (typeof original !== "function") continue;
          md[name] = function (...args) {
            return original.apply(this, args).then((stream) => {
              for (const t of stream.getTracks()) {
                tracks.add(t);
                t.addEventListener("ended", () => tracks.delete(t));
              }
              return stream;
            });
          };
        }
      }
      for (const ctorName of ["SpeechRecognition", "webkitSpeechRecognition"]) {
        const ctor = window[ctorName];
        if (!ctor || !ctor.prototype || typeof ctor.prototype.start !== "function") continue;
        const start = ctor.prototype.start;
        ctor.prototype.start = function (...args) {
          recognizers.add(this);
          this.addEventListener("end", () => recognizers.delete(this));
          return start.apply(this, args);
        };
      }
      Object.defineProperty(window, "__dmStopMedia", {
        value: () => {
          try {
            window.dispatchEvent(new CustomEvent("dourmouse:console-hiding"));
          } catch (_e) { /* a screen's handler failing must not keep the microphone on */ }
          let stopped = 0;
          for (const t of [...tracks]) {
            if (t.readyState === "live") { t.stop(); stopped += 1; }
          }
          tracks.clear();
          for (const r of [...recognizers]) {
            try { r.abort(); stopped += 1; } catch (_e) { /* already stopped */ }
          }
          recognizers.clear();
          return stopped;
        },
        enumerable: false,
      });
    },
  });
} catch (exc) {
  // main.js then finds no __dmStopMedia and a hidden console keeps whatever it was capturing.
  console.warn("preload: could not install the capture tracker:", exc && exc.message ? exc.message : exc);
}
