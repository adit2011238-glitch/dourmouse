// Dourmouse native shell -- pure security decisions (findings S34, S35).
//
// CommonJS with no Electron imports on purpose, so plain node can test every
// rule (dourmouse/tests/test_electron_hardening.py). main.js wires these into
// the real session and window handlers.

const DEFAULT_PORT = 8765;

function appPort(port) {
  if (port !== undefined && port !== null) return String(port);
  return String(parseInt(process.env.DOURMOUSE_UI_PORT || String(DEFAULT_PORT), 10));
}

// True only for the app's own origin: http on 127.0.0.1 or localhost at the
// server port. Parsed with URL, so "http://127.0.0.1:8765@evil.example" and
// "http://127.0.0.1:8765.evil.example" are judged by their real host.
function isAppOrigin(url, port) {
  let parsed;
  try {
    parsed = new URL(String(url));
  } catch {
    return false;
  }
  if (parsed.protocol !== "http:") return false;
  if (parsed.hostname !== "127.0.0.1" && parsed.hostname !== "localhost") return false;
  return parsed.port === appPort(port);
}

// The only permissions the app's own pages use: the microphone and camera
// (push-to-talk, voice page, hand-tracking; all started by an explicit user
// action) and clipboard writes (the COPY buttons). Web Notifications are not
// used (alerts go through the main process), and geolocation, clipboard reads,
// screen capture and the rest are never needed.
// "fullscreen" is the media player's fullscreen button (finding #139).
const APP_PERMISSIONS = new Set(["media", "clipboard-sanitized-write", "fullscreen"]);

// Deny by default. A remote page (anything in the browser pane) is never
// granted anything, whatever it asks for.
function permissionAllowed(permission, origin, port) {
  return APP_PERMISSIONS.has(permission) && isAppOrigin(origin, port);
}

// Main and task windows may only ever show the app itself.
function navigationAllowed(url, port) {
  return isAppOrigin(url, port);
}

// Anything else is handed to the OS browser, and only for plain web links:
// never file:, javascript:, data: or a custom scheme handler.
function externalUrlAllowed(url) {
  try {
    const protocol = new URL(String(url)).protocol;
    return protocol === "http:" || protocol === "https:";
  } catch {
    return false;
  }
}

module.exports = { isAppOrigin, permissionAllowed, navigationAllowed, externalUrlAllowed, APP_PERMISSIONS };
