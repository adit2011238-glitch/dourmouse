/* A page the server asked the browser pane to open while BROWSER was not the
   current screen (the browser_pane_open event: NEWS sends it, and so do the
   agent's browser tools). The shell keeps the newest request in one slot and
   BROWSER opens it the next time it mounts. One slot, never a list: only the
   newest request matters, and nothing here grows. */

let pending = null;

export function putPaneRequest(url) {
  pending = typeof url === 'string' && url.trim() ? url.trim().slice(0, 2000) : null;
}

/* Returns the waiting address once, then forgets it. */
export function takePaneRequest() {
  const url = pending;
  pending = null;
  return url;
}

export function hasPaneRequest() {
  return pending !== null;
}
