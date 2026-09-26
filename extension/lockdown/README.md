# Dourmouse Lockdown browser extension

Blocks web addresses with a path (for example `reddit.com/r/all`) while a
Dourmouse lockdown is on. Whole website names are blocked system-wide through
the hosts file; a path cannot be, so this extension covers it inside the
browser.

It asks Dourmouse on this Mac for its rules (`http://127.0.0.1:8765/api/security/lockdown/rules`)
when the browser starts and every 15 seconds, and installs them as Chrome
declarativeNetRequest rules. It asks for three permissions (declarativeNetRequest,
alarms, storage) and may talk only to `127.0.0.1` and `localhost`. It has no
content scripts and loads no remote code.

## Install (Chrome, Chromium, Brave, Edge, Arc)

1. Open `chrome://extensions` (Brave: `brave://extensions`, Edge: `edge://extensions`).
2. Turn on **Developer mode** (top right).
3. Click **Load unpacked** and choose this folder, `extension/lockdown`.
4. Click the puzzle icon in the toolbar and **pin** "Dourmouse Lockdown".
5. For private windows: open the extension's **Details** and turn on **Allow in Incognito**.

## Add a URL and check that it works

1. In Dourmouse add a URL to the lockdown list (the console's lockdown card, kind "url", or ask the assistant to add `example.com/blocked-page`).
2. Start the lockdown.
3. Within 15 seconds, open `https://example.com/blocked-page` in the browser. Chrome shows "This site has been blocked".
4. Stop the lockdown. Within 15 seconds the page opens again.

The toolbar icon shows no badge when all is well. A red `!` badge means the
extension cannot reach Dourmouse (the app is closed, or the port differs). The
extension then KEEPS the last rules, so closing Dourmouse does not end the
block. Hover the icon for the reason.

If Dourmouse runs on another port, open `chrome://extensions`, click the
"service worker" link under this extension, and in its console run
`chrome.storage.local.set({ port: 9000 })`.

## What a rule covers

An entry matches the host, its subdomains, and any address whose path starts
with the entry: `reddit.com/r/all` blocks `www.reddit.com/r/all` and
`old.reddit.com/r/all/top` and also `reddit.com/r/allthethings`. The query and
fragment are ignored. Only pages and frames are blocked, not other requests.

## Honest limits

- Only browsers that have this extension installed are covered. Safari, Firefox and any other browser are NOT protected by it.
- A private window is covered only if "Allow in Incognito" is on for the extension.
- The extension can be disabled or removed in `chrome://extensions`. It does not stop the person who owns the browser from doing that.
- Whole-name blocking through the hosts file still applies system-wide, in every browser, whether or not this extension is installed.
- Chrome may stretch the 15 second poll to 30 seconds for a packed extension. Changes reach the browser within that time, not instantly.
- The extension was checked with unit tests against a stubbed browser API. It could not be loaded into Chrome from a script, because Chrome blocks that; the steps above are the manual check.

## Tests

`node test_background.mjs` (no dependencies) runs the worker against stubbed
`chrome.*` and `fetch`. The Python suite runs it too.
