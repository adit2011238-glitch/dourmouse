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
  },
});
