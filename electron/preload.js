// Dourmouse native shell -- preload script.
//
// Keeps the EXACT `window.pywebview.api.*` global shape ui/*.html already
// calls today (verified by grepping every ui/*.html for real call sites
// before writing this -- open_agent [index.html], open_all_hands
// [index.html, all_hands.html], open_external [setup.html, login.html] are
// the only three with a real caller; open_map/navigate/window_state/
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
