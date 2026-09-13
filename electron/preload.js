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
