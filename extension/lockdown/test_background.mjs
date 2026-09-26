// Plain-node harness for background.js: no dependencies. It runs the real
// worker script against stubs of chrome.* and fetch, and checks that rules are
// replaced on change, skipped when the version is unchanged, and kept when
// Dourmouse cannot be reached. Run: node test_background.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.join(here, "background.js"), "utf8");

function rule(id, filter) {
  return { id, priority: 1, action: { type: "block" }, condition: { urlFilter: filter, resourceTypes: ["main_frame", "sub_frame"] } };
}

function makeWorld(initialStorage = {}) {
  const world = {
    storage: { ...initialStorage },
    installed: [],
    updateCalls: [],
    fetchCalls: [],
    badge: { text: "", title: "" },
    alarms: new Map(),
    listeners: { alarm: [], startup: [], installed: [], changed: [] },
    answer: null, // a function returning the fetch Response stand-in
  };
  const chrome = {
    storage: {
      local: {
        async get(keys) {
          if (typeof keys === "string") return keys in world.storage ? { [keys]: world.storage[keys] } : {};
          const out = { ...keys };
          for (const k of Object.keys(keys)) if (k in world.storage) out[k] = world.storage[k];
          return out;
        },
        async set(values) {
          Object.assign(world.storage, values);
        },
      },
      onChanged: { addListener: (f) => world.listeners.changed.push(f) },
    },
    declarativeNetRequest: {
      async getDynamicRules() {
        return world.installed.map((r) => ({ ...r }));
      },
      async updateDynamicRules({ removeRuleIds, addRules }) {
        world.updateCalls.push({ removeRuleIds, addRules });
        world.installed = world.installed.filter((r) => !removeRuleIds.includes(r.id)).concat(addRules);
      },
    },
    alarms: {
      async get(name) {
        return world.alarms.get(name);
      },
      async create(name, info) {
        world.alarms.set(name, info);
      },
      onAlarm: { addListener: (f) => world.listeners.alarm.push(f) },
    },
    action: {
      async setBadgeText({ text }) {
        world.badge.text = text;
      },
      async setBadgeBackgroundColor() {},
      async setTitle({ title }) {
        world.badge.title = title;
      },
    },
    runtime: {
      onStartup: { addListener: (f) => world.listeners.startup.push(f) },
      onInstalled: { addListener: (f) => world.listeners.installed.push(f) },
    },
  };
  const fetchStub = async (url) => {
    world.fetchCalls.push(url);
    return world.answer();
  };
  const context = vm.createContext({
    chrome, fetch: fetchStub, console, setTimeout, clearTimeout, AbortController,
  });
  vm.runInContext(source, context);
  world.context = context;
  world.sync = () => vm.runInContext("syncOnce()", context);
  return world;
}

const ok = (body) => () => ({ ok: true, status: 200, json: async () => body });
const settle = () => new Promise((r) => setTimeout(r, 10));

let passed = 0;
async function test(name, fn) {
  await fn();
  passed += 1;
  console.log(`ok - ${name}`);
}

await test("startup registers a 15 second alarm and syncs once", async () => {
  const w = makeWorld();
  w.answer = ok({ active: true, version: "v1", rules: [rule(1, "||a.com/x")] });
  await settle();
  assert.equal(w.alarms.get("dourmouse-lockdown-sync").periodInMinutes, 0.25);
  assert.deepEqual(w.fetchCalls, ["http://127.0.0.1:8765/api/security/lockdown/rules"]);
  assert.equal(w.installed.length, 1);
});

await test("a changed version replaces every old rule in one update", async () => {
  const w = makeWorld();
  w.answer = ok({ active: true, version: "v1", rules: [rule(1, "||a.com/x"), rule(2, "||b.com/y")] });
  await w.sync();
  w.answer = ok({ active: true, version: "v2", rules: [rule(1, "||c.com/z")] });
  await w.sync();
  const last = w.updateCalls.at(-1);
  assert.deepEqual(last.removeRuleIds, [1, 2]);
  assert.deepEqual(last.addRules.map((r) => r.condition.urlFilter), ["||c.com/z"]);
  assert.deepEqual(w.installed.map((r) => r.condition.urlFilter), ["||c.com/z"]);
  assert.equal(w.storage.appliedVersion, "v2");
  assert.equal(w.badge.text, "");
});

await test("an unchanged version does not touch the rules", async () => {
  const w = makeWorld();
  w.answer = ok({ active: true, version: "v1", rules: [rule(1, "||a.com/x")] });
  await w.sync();
  const calls = w.updateCalls.length;
  await w.sync();
  await w.sync();
  assert.equal(w.updateCalls.length, calls);
});

await test("lockdown ending clears the rules", async () => {
  const w = makeWorld();
  w.answer = ok({ active: true, version: "v1", rules: [rule(1, "||a.com/x")] });
  await w.sync();
  w.answer = ok({ active: false, version: "v0", rules: [] });
  await w.sync();
  assert.deepEqual(w.installed, []);
});

await test("a failed fetch keeps the last rules and shows a badge", async () => {
  const w = makeWorld();
  w.answer = ok({ active: true, version: "v1", rules: [rule(1, "||a.com/x")] });
  await w.sync();
  const calls = w.updateCalls.length;
  w.answer = () => {
    throw new Error("connection refused");
  };
  await w.sync();
  assert.equal(w.updateCalls.length, calls);
  assert.equal(w.installed.length, 1);
  assert.equal(w.badge.text, "!");
  assert.match(w.badge.title, /keeping the last rules/);
  w.answer = ok({ active: true, version: "v1", rules: [rule(1, "||a.com/x")] });
  await w.sync();
  assert.equal(w.badge.text, "", "the badge clears once Dourmouse answers again");
});

await test("an HTTP error keeps the last rules", async () => {
  const w = makeWorld({ appliedVersion: "v1" });
  w.installed = [rule(1, "||a.com/x")];
  w.answer = () => ({ ok: false, status: 500, json: async () => ({}) });
  await w.sync();
  assert.equal(w.updateCalls.length, 0);
  assert.equal(w.installed.length, 1);
  assert.equal(w.badge.text, "!");
});

await test("rules that are not plain host-anchored blocks are refused and the old ones kept", async () => {
  const w = makeWorld({ appliedVersion: "v1" });
  w.installed = [rule(1, "||a.com/x")];
  const redirect = { id: 1, action: { type: "redirect" }, condition: { urlFilter: "||evil.com/" } };
  for (const bad of [[redirect], [rule(1, "*")], [{ id: "1" }], null]) {
    w.answer = ok({ active: true, version: "v9", rules: bad });
    await w.sync();
  }
  assert.equal(w.updateCalls.length, 0);
  assert.equal(w.installed[0].condition.urlFilter, "||a.com/x");
});

await test("the port comes from chrome.storage.local, falling back when invalid", async () => {
  const w = makeWorld({ port: 9100 });
  w.answer = ok({ active: false, version: "v0", rules: [] });
  await w.sync();
  assert.equal(w.fetchCalls.at(-1), "http://127.0.0.1:9100/api/security/lockdown/rules");
  w.storage.port = "not a port";
  await w.sync();
  assert.equal(w.fetchCalls.at(-1), "http://127.0.0.1:8765/api/security/lockdown/rules");
});

await test("the alarm fires a sync", async () => {
  const w = makeWorld();
  w.answer = ok({ active: true, version: "v1", rules: [rule(1, "||a.com/x")] });
  await settle();
  w.answer = ok({ active: true, version: "v2", rules: [rule(1, "||b.com/y")] });
  w.listeners.alarm[0]({ name: "dourmouse-lockdown-sync" });
  await settle();
  assert.equal(w.installed[0].condition.urlFilter, "||b.com/y");
});

console.log(`${passed} tests passed`);
