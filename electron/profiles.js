// Dourmouse native shell -- browser profiles (Phase B3).
//
// CommonJS with no Electron imports, like policy.js, so plain node can test every rule.
// main.js owns the Electron sessions and the disk; this file owns the decisions: what a
// profile may be called, where its files and its cookie jar live, and what the list of
// profiles looks like after an add, a remove or a switch.
//
// A profile is a name plus two things that belong to it alone:
//   * a persistent Chromium partition (its own cookie jar, local storage and cache), and
//   * its own history, bookmarks, site permissions, saved passwords, addresses and zoom.
// The "default" profile is not special code: it simply keeps the paths and the partition
// that existed before profiles did, so nothing that already exists moves.

const DEFAULT_PROFILE = "default";
const DEFAULT_PARTITION = "persist:dourmouse-browser";
const MAX_PROFILES = 8;
const NAME_RE = /^[a-z0-9][a-z0-9_-]{0,23}$/;

// Lower case letters, digits, "_" and "-", at most 24, starting with a letter or a digit.
// This is the only gate between a caller-supplied name and a folder name and a partition
// name, so it is strict on purpose: no dots, no slashes, no spaces, no unicode.
function cleanName(name) {
  return String(name === undefined || name === null ? "" : name).trim().toLowerCase();
}

function isValidName(name) {
  return NAME_RE.test(String(name));
}

// "default" is valid as a lookup, never as a name to create.
function isKnownName(registry, name) {
  return name === DEFAULT_PROFILE || registry.names.includes(name);
}

function partitionFor(name) {
  if (name === DEFAULT_PROFILE) return DEFAULT_PARTITION;
  if (!isValidName(name)) throw new Error("not a profile name");
  return `${DEFAULT_PARTITION}-${name}`;
}

// The folder a profile keeps its files in. The default profile keeps the browser folder
// itself (today's paths); a named one gets browser/profiles/<name>/.
function dirFor(path, browserDir, name) {
  if (name === DEFAULT_PROFILE) return browserDir;
  if (!isValidName(name)) throw new Error("not a profile name");
  return path.join(browserDir, "profiles", name);
}

// A file the owner or another program edited: keep only what this code could have written.
function sanitizeRegistry(raw) {
  const names = [];
  const src = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
  for (const n of Array.isArray(src.names) ? src.names : []) {
    if (typeof n === "string" && isValidName(n) && n !== DEFAULT_PROFILE && !names.includes(n) && names.length < MAX_PROFILES) names.push(n);
  }
  const active = typeof src.active === "string" && (src.active === DEFAULT_PROFILE || names.includes(src.active)) ? src.active : DEFAULT_PROFILE;
  return { active, names };
}

function addProfile(registry, rawName) {
  const name = cleanName(rawName);
  if (!isValidName(name)) return { registry, ok: false, error: "A profile name uses lower case letters, digits, - and _ (up to 24 characters)." };
  if (name === DEFAULT_PROFILE) return { registry, ok: false, error: "That name is already used by the default profile." };
  if (registry.names.includes(name)) return { registry, ok: false, error: "A profile with that name already exists." };
  if (registry.names.length >= MAX_PROFILES) return { registry, ok: false, error: `At most ${MAX_PROFILES} extra profiles.` };
  return { registry: { ...registry, names: [...registry.names, name] }, ok: true, name };
}

function removeProfile(registry, rawName) {
  const name = cleanName(rawName);
  if (name === DEFAULT_PROFILE) return { registry, ok: false, error: "The default profile cannot be removed." };
  if (!registry.names.includes(name)) return { registry, ok: false, error: "No such profile." };
  if (registry.active === name) return { registry, ok: false, error: "Switch to another profile before removing this one." };
  return { registry: { ...registry, names: registry.names.filter((n) => n !== name) }, ok: true, name };
}

function setActive(registry, rawName) {
  const name = cleanName(rawName);
  if (!isKnownName(registry, name)) return { registry, ok: false, error: "No such profile." };
  return { registry: { ...registry, active: name }, ok: true, name };
}

// What the console shows. Names and which one is active; never a path.
function listProfiles(registry) {
  return [DEFAULT_PROFILE, ...registry.names].map((name) => ({ name, active: name === registry.active, isDefault: name === DEFAULT_PROFILE }));
}

module.exports = {
  DEFAULT_PROFILE, DEFAULT_PARTITION, MAX_PROFILES, cleanName, isValidName, isKnownName, partitionFor, dirFor,
  sanitizeRegistry, addProfile, removeProfile, setActive, listProfiles,
};
