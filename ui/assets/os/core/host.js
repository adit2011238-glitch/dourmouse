/* Electron, pywebview or a plain browser. preload.js exposes window.pywebview
   inside Electron too, so typeof window.pywebview cannot tell them apart:
   check window.dourmouseShell first (pitfall 5).

   pane.onState calls ipcRenderer.on with no way to unsubscribe, so it is
   subscribed exactly once here and fanned out; a screen that subscribed on
   every mount would leak one listener per visit. */

export function createHost({ win = globalThis.window } = {}) {
  const w = win || {};
  const shell = w.dourmouseShell || null;
  const kind = shell ? 'electron' : w.pywebview ? 'pywebview' : 'browser';
  const paneListeners = new Set();
  let paneState = null;
  let subscribed = false;

  function subscribePane() {
    if (subscribed || !shell || !shell.pane || typeof shell.pane.onState !== 'function') return;
    subscribed = true;
    shell.pane.onState((s) => {
      paneState = s;
      paneListeners.forEach((fn) => {
        try {
          fn(s);
        } catch (err) {
          console.error(err);
        }
      });
    });
  }

  const bridge = () => (w.pywebview && w.pywebview.api) || null;

  return {
    kind,
    pane: shell ? shell.pane : null,
    paneState: () => paneState,
    onPaneState(fn) {
      subscribePane();
      paneListeners.add(fn);
      return () => paneListeners.delete(fn);
    },
    paneListenerCount: () => paneListeners.size,
    /* External links never use target=_blank: the Electron main window has no
       window-open handler, so that would spawn an unmanaged window. */
    openExternal(url) {
      const u = String(url || '');
      /* http(s), or exactly a macOS Privacy & Security pane (finding #169; the main process checks again) */
      const settingsPane = /^x-apple\.systempreferences:com\.apple\.preference\.security\?Privacy_[A-Za-z]{1,40}$/.test(u);
      if (!/^https?:\/\//i.test(u) && !settingsPane) return false;
      if (settingsPane && kind === 'browser') return false; /* a plain browser tab cannot open it */
      const b = bridge();
      if (b && typeof b.open_external === 'function') {
        b.open_external(u);
        return true;
      }
      if (kind === 'browser' && typeof w.open === 'function') {
        w.open(u, '_blank', 'noopener,noreferrer');
        return true;
      }
      return false;
    },
    openAgent(name) {
      const b = bridge();
      if (b && typeof b.open_agent === 'function') {
        b.open_agent(name);
        return true;
      }
      return false;
    },
  };
}
