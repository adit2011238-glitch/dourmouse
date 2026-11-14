// The real macOS menu bar (T6 milestone 1, finding #194). Before this there was no application
// menu at all, so Electron's default one showed, and the page painted a fake menu bar of its own.
//
// Every item that is not a plain macOS role sends one command id to the console window
// ("shell:command"). ui/assets/os/core/commands.js maps the same ids to what they do, and a test
// checks the two lists agree. Keyboard shortcuts typed in the page are handled by the page's
// keymap first (it calls preventDefault, so the menu does not fire a second time); the menu
// catches the same keys when focus is somewhere the page cannot see, such as the browser pane.

"use strict";

const PLACES = [
  ["home", "Home"],
  ["projects", "Projects"],
  ["office", "Office"],
  ["research", "Research"],
  ["library", "Library"],
  ["browser", "Browser"],
  ["security", "Security"],
  ["settings", "Settings"],
];

// Every command id the menu can send. Kept as data so the test can compare it with the page.
const COMMAND_IDS = [
  "settings", "new", "search", "sidebar", "inspector", "alerts", "refresh", "spec", "shortcuts",
  ...PLACES.map(([slug]) => "place:" + slug),
];

function buildTemplate({ appName = "Dourmouse", send, state = {} }) {
  const cmd = (label, id, accelerator, extra = {}) => ({ label, accelerator, click: () => send(id), ...extra });
  return [
    {
      label: appName,
      submenu: [
        { role: "about", label: "About " + appName },
        { type: "separator" },
        cmd("Settings...", "settings", "Command+,"),
        { type: "separator" },
        { role: "services" },
        { type: "separator" },
        { role: "hide", label: "Hide " + appName },
        { role: "hideOthers" },
        { role: "unhide" },
        { type: "separator" },
        { role: "quit", label: "Quit " + appName },
      ],
    },
    {
      label: "File",
      submenu: [
        cmd("New Thread", "new", "Command+N"),
        cmd("Search", "search", "Command+K"),
        { type: "separator" },
        { role: "close" },
      ],
    },
    { role: "editMenu" },
    {
      label: "View",
      submenu: [
        cmd(state.sidebar === false ? "Show Sidebar" : "Hide Sidebar", "sidebar", "Control+Command+S"),
        cmd(state.inspector ? "Hide Inspector" : "Show Inspector", "inspector", "Alt+Command+I"),
        cmd("Show Alerts", "alerts", "Alt+Command+A"),
        { type: "separator" },
        cmd("Refresh This View", "refresh", "Command+R"),
        { type: "separator" },
        { role: "togglefullscreen" },
        { type: "separator" },
        {
          label: "Developer",
          submenu: [cmd("Label Every Control", "spec", "Alt+A")],
        },
      ],
    },
    {
      label: "Go",
      submenu: PLACES.map(([slug, label], i) => cmd(label, "place:" + slug, "Command+" + (i + 1))),
    },
    { role: "windowMenu" },
    {
      role: "help",
      submenu: [cmd("Keyboard Shortcuts", "shortcuts", "Command+/")],
    },
  ];
}

// Installs the menu and returns { update(state) } to relabel Show/Hide items.
function installAppMenu({ Menu, appName, send }) {
  let state = { sidebar: true, inspector: false };
  const apply = () => Menu.setApplicationMenu(Menu.buildFromTemplate(buildTemplate({ appName, send, state })));
  apply();
  return {
    update(next) {
      if (!next || typeof next !== "object") return;
      const clean = { sidebar: next.sidebar !== false, inspector: next.inspector === true };
      if (clean.sidebar === state.sidebar && clean.inspector === state.inspector) return;
      state = clean;
      apply();
    },
  };
}

module.exports = { buildTemplate, installAppMenu, COMMAND_IDS, PLACES };
