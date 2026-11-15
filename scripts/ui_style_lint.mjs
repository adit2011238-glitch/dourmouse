// The shell's style check (T6 milestone 1, finding #194). Fails on the things the T6 principles
// and the owner's AI-slop rules ban, in the shell's own files (the screens are restyled in T6
// milestone 2 and join the check then):
//   CSS:  gradients, text-shadow, uppercase, letter-spaced labels, backdrop blur, decorative shadows,
//         a font-family outside the tokens, colour literals outside tokens.css, removed focus outlines
//   all:  em dashes, emoji
// Usage: node scripts/ui_style_lint.mjs [--json]   (exit 1 when anything is found)

import { readFileSync, readdirSync } from "node:fs";
import { join, relative, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const OS = join(ROOT, "ui", "assets", "os");

export const CSS_FILES = ["tokens.css", "shell.css", "palette.css"].map((f) => join(OS, f));
export const TEXT_FILES = [
  join(ROOT, "ui", "shell.html"),
  join(OS, "boot.js"),
  ...["chrome", "core"].flatMap((d) => readdirSync(join(OS, d)).filter((f) => f.endsWith(".js")).map((f) => join(OS, d, f))),
  join(ROOT, "electron", "appearance.js"),
  join(ROOT, "electron", "appmenu.js"),
];

const EMOJI = /[\u{1F300}-\u{1FAFF}\u{2600}-\u{26FF}\u{2700}-\u{27BF}\u{1F000}-\u{1F2FF}]/u;
const COLOR = /#[0-9a-f]{3,8}\b|\brgba?\(|\bhsla?\(|\bhwb\(|\boklch\(/i;
/* a shadow is allowed only as the token or as a 0.5px hairline drawn in a token colour */
const SHADOW_OK = /^\s*(none|var\(--tk-shadow-float\)|0 0 0 0\.5px var\(--tk-[a-z0-9-]+\))\s*$/;

function stripComments(css) {
  return css.replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, " "));
}

export function lintCss(text, file) {
  const out = [];
  const isTokens = file.endsWith("tokens.css");
  const lines = stripComments(text).split("\n");
  lines.forEach((line, i) => {
    const at = relative(ROOT, file) + ":" + (i + 1);
    const add = (rule) => out.push({ at, rule, line: line.trim().slice(0, 160) });
    if (/gradient\(/i.test(line)) add("gradient");
    if (/text-shadow\s*:/i.test(line)) add("text-shadow");
    if (/text-transform\s*:\s*uppercase/i.test(line)) add("uppercase");
    const ls = /letter-spacing\s*:\s*([^;]+)/i.exec(line);
    if (ls && !/^\s*(0|normal)\s*$/.test(ls[1])) add("letter-spacing");
    /* the one blur the owner approved (2026-11-14): the sidebar over the owner's own wallpaper photo */
    const wallpaperBlur = /\[data-wallpaper="on"\] #sidebar/.test(line);
    if (/backdrop-filter\s*:/i.test(line) && !/:\s*none/.test(line) && !wallpaperBlur) add("backdrop blur");
    if (/(^|[^-])filter\s*:\s*[^;]*blur\(/i.test(line)) add("blur");
    const sh = /(^|[^-])box-shadow\s*:\s*([^;]+)/i.exec(line);
    if (sh && !SHADOW_OK.test(sh[2])) add("decorative shadow");
    const ff = /font-family\s*:\s*([^;]+)/i.exec(line);
    if (ff && !isTokens && !/^\s*(var\(--tk-font(-mono)?\)|inherit)\s*$/.test(ff[1])) add("font-family outside the tokens");
    if (!isTokens && COLOR.test(line)) add("colour literal outside tokens.css");
    if (/outline\s*:\s*(none|0)\b/i.test(line) && !/#stagetitle:focus\b/.test(line) && !/\.pal-field input/.test(line)) add("focus outline removed");
    if (/\u2014/.test(line)) add("em dash");
  });
  return out;
}

export function lintText(text, file) {
  const out = [];
  text.split("\n").forEach((line, i) => {
    const at = relative(ROOT, file) + ":" + (i + 1);
    if (/\u2014/.test(line)) out.push({ at, rule: "em dash", line: line.trim().slice(0, 160) });
    if (EMOJI.test(line)) out.push({ at, rule: "emoji", line: line.trim().slice(0, 160) });
  });
  return out;
}

export function lintAll() {
  return [
    ...CSS_FILES.flatMap((f) => lintCss(readFileSync(f, "utf8"), f)),
    ...TEXT_FILES.flatMap((f) => lintText(readFileSync(f, "utf8"), f)),
  ];
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  const found = lintAll();
  if (process.argv.includes("--json")) console.log(JSON.stringify({ files: CSS_FILES.length + TEXT_FILES.length, found }));
  else if (found.length) found.forEach((f) => console.log(f.at + "  " + f.rule + "  " + f.line));
  else console.log("ui style lint: " + (CSS_FILES.length + TEXT_FILES.length) + " files, nothing found");
  process.exit(found.length ? 1 : 0);
}
