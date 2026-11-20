// The Ask panel's window rules (plan 4.9, PL-04). Pure CommonJS with no Electron import, so plain node tests
// every decision (dourmouse/tests/test_ask_panel_electron.py). main.js owns the window and the shortcut.

"use strict";

const policy = require("./policy");

const WIDTH = 640;
const HEIGHT_MIN = 112;
const HEIGHT_MAX = 560;
const TOP_FRACTION = 0.2; // the input sits about a fifth of the way down, like a launcher

// Only the app's own /ask page may drive the panel's IPC.
function isAskPage(url, port) {
  if (!policy.isAppOrigin(url, port)) return false;
  try {
    const p = new URL(String(url)).pathname;
    return p === "/ask" || p === "/ask.html";
  } catch {
    return false;
  }
}

function clampHeight(h) {
  const n = Number(h);
  if (!Number.isFinite(n)) return HEIGHT_MIN;
  return Math.max(HEIGHT_MIN, Math.min(HEIGHT_MAX, Math.round(n)));
}

// Where the panel goes on the display the pointer is on: centred, high on the screen, inside its work area.
function panelBounds(workArea, height = HEIGHT_MIN) {
  const h = clampHeight(height);
  const w = Math.min(workArea.width, WIDTH, Math.max(320, workArea.width - 40));
  const x = Math.round(workArea.x + (workArea.width - w) / 2);
  const y = Math.round(workArea.y + Math.min(workArea.height * TOP_FRACTION, Math.max(0, workArea.height - h - 20)));
  return { x, y, width: w, height: h };
}

// The one place the panel may send the owner in the main window: a hash of the shell's own screens.
const TARGET_RE = /^#\/(home|projects|office|research|library|browser|security|settings)(\/[A-Za-z0-9_.:-]{1,80}){0,3}$/;
function openTargetAllowed(hash) {
  return typeof hash === "string" && hash.length <= 200 && TARGET_RE.test(hash);
}

// Clipboard text offered as context: plain text only, capped, never an empty string.
const CLIP_MAX = 8000;
function clipboardContext(text) {
  if (typeof text !== "string") return null;
  const t = text.replace(/\u0000/g, "").trim();
  if (!t) return null;
  return t.length > CLIP_MAX ? { text: t.slice(0, CLIP_MAX), cut: true } : { text: t, cut: false };
}

// What the settings change needs from the shortcut: register, unregister, or nothing.
function shortcutPlan(prev, next) {
  const was = prev && prev.enabled ? prev.hotkey : "";
  const now = next && next.enabled ? next.hotkey : "";
  if (was === now) return { unregister: "", register: "" };
  return { unregister: was, register: now };
}

module.exports = { WIDTH, HEIGHT_MIN, HEIGHT_MAX, isAskPage, clampHeight, panelBounds, openTargetAllowed, clipboardContext, shortcutPlan };
