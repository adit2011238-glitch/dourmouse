// The macOS appearance bridge (T6 milestone 1, finding #194).
//
// Reads what the owner chose in System Settings (light or dark, the accent colour, increased
// contrast, reduced transparency and motion) and the dynamic system colours, and hands the page
// a plain object of checked hex strings. The page never gets a handle to systemPreferences: it
// asks with "appearance:get" and hears "appearance:changed". Every value is validated here and
// again in ui/assets/os/core/appearance.js, so a strange return from the OS can only ever drop a
// colour back to its fallback, never put arbitrary text into a CSS custom property.

"use strict";

const HEX = /^#?([0-9a-f]{6})([0-9a-f]{2})?$/i;

// Dynamic system colours the page uses (Electron's getColor names). Each maps to a token.
const SYSTEM_COLORS = {
  label: "label",
  "secondary-label": "secondaryLabel",
  "tertiary-label": "tertiaryLabel",
  "placeholder-text": "placeholder",
  separator: "separator",
  "control-background": "controlBackground",
  "window-background": "windowBackground",
  "under-page-background": "underPage",
  "selected-content-background": "selectedContent",
  "unemphasized-selected-content-background": "selectedInactive",
  link: "link",
};
const STATUS_COLORS = { green: "good", orange: "warning", red: "critical", blue: "blue" };

// "#RRGGBB" from an Electron colour string ("RRGGBBAA", "#RRGGBBAA" or "#RRGGBB"), alpha kept apart.
function normHex(value) {
  const m = HEX.exec(String(value || "").trim());
  if (!m) return null;
  const alpha = m[2] ? parseInt(m[2], 16) / 255 : 1;
  return { hex: "#" + m[1].toLowerCase(), alpha: Math.round(alpha * 1000) / 1000 };
}

function safe(fn, fallback) {
  try {
    const v = fn();
    return v === undefined ? fallback : v;
  } catch (_exc) {
    return fallback;
  }
}

// -> { source, dark, highContrast, reduceTransparency, reduceMotion, accent, colors: {token: {hex, alpha}} }
function readAppearance({ nativeTheme, systemPreferences, platform = process.platform }) {
  const out = {
    source: "system",
    dark: Boolean(safe(() => nativeTheme.shouldUseDarkColors, false)),
    highContrast: Boolean(safe(() => nativeTheme.shouldUseHighContrastColors, false)),
    reduceTransparency: Boolean(safe(() => nativeTheme.prefersReducedTransparency, false)),
    reduceMotion: false,
    accent: null,
    colors: {},
  };
  if (platform !== "darwin" || !systemPreferences) return out;
  const anim = safe(() => systemPreferences.getAnimationSettings(), null);
  if (anim && typeof anim.prefersReducedMotion === "boolean") out.reduceMotion = anim.prefersReducedMotion;
  const accent = normHex(safe(() => systemPreferences.getAccentColor(), ""));
  if (accent) out.accent = accent.hex;
  for (const [name, token] of Object.entries(SYSTEM_COLORS)) {
    const c = normHex(safe(() => systemPreferences.getColor(name), ""));
    if (c) out.colors[token] = c;
  }
  for (const [name, token] of Object.entries(STATUS_COLORS)) {
    const c = normHex(safe(() => systemPreferences.getSystemColor(name), ""));
    if (c) out.colors[token] = c;
  }
  return out;
}

// Calls send(appearance) now and again on every change, coalesced to one call per 120 ms
// (a theme switch fires nativeTheme "updated" and the colour notification together).
// Returns a function that stops watching.
function watchAppearance({ nativeTheme, systemPreferences, platform = process.platform, send }) {
  let timer = null;
  const fire = () => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      send(readAppearance({ nativeTheme, systemPreferences, platform }));
    }, 120);
  };
  const subs = [];
  nativeTheme.on("updated", fire);
  if (platform === "darwin" && systemPreferences && typeof systemPreferences.subscribeNotification === "function") {
    // The accent and highlight colour change; the dark-mode switch (nativeTheme covers it too).
    for (const n of ["AppleColorPreferencesChangedNotification", "AppleInterfaceThemeChangedNotification"]) {
      const id = safe(() => systemPreferences.subscribeNotification(n, fire), null);
      if (typeof id === "number") subs.push(id);
    }
  }
  return () => {
    if (timer) clearTimeout(timer);
    nativeTheme.removeListener("updated", fire);
    for (const id of subs) safe(() => systemPreferences.unsubscribeNotification(id), null);
  };
}

module.exports = { readAppearance, watchAppearance, normHex, SYSTEM_COLORS, STATUS_COLORS };
