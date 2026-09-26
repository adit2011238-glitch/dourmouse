// Dourmouse Lockdown: installs the URL-path block rules Dourmouse publishes on
// this Mac. It fetches one local address, never a remote one, and only ever
// installs plain "block" rules whose filter starts with "||" (see validRule).

const DEFAULT_PORT = 8765;
const ALARM_NAME = "dourmouse-lockdown-sync";
const POLL_MINUTES = 0.25; // 15 seconds; Chrome may stretch this to 30 for a packed extension
const FETCH_TIMEOUT_MS = 5000;
const VERSION_KEY = "appliedVersion";

async function getPort() {
  const stored = await chrome.storage.local.get({ port: DEFAULT_PORT });
  const port = Number(stored.port);
  return Number.isInteger(port) && port >= 1 && port <= 65535 ? port : DEFAULT_PORT;
}

function validRule(rule) {
  return (
    rule !== null &&
    typeof rule === "object" &&
    Number.isInteger(rule.id) &&
    rule.id >= 1 &&
    rule.action !== null &&
    typeof rule.action === "object" &&
    rule.action.type === "block" &&
    rule.condition !== null &&
    typeof rule.condition === "object" &&
    typeof rule.condition.urlFilter === "string" &&
    rule.condition.urlFilter.startsWith("||")
  );
}

async function fetchRules() {
  const port = await getPort();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  try {
    const response = await fetch(`http://127.0.0.1:${port}/api/security/lockdown/rules`, {
      signal: controller.signal,
      cache: "no-store",
    });
    if (!response.ok) {
      throw new Error(`Dourmouse answered HTTP ${response.status}`);
    }
    const body = await response.json();
    if (typeof body.version !== "string" || !Array.isArray(body.rules) || !body.rules.every(validRule)) {
      throw new Error("Dourmouse sent rules in an unexpected shape");
    }
    return body;
  } finally {
    clearTimeout(timer);
  }
}

async function applyRules(body) {
  // One call removes every old id and adds the new rules, so there is never a
  // moment with half the rules installed.
  const existing = await chrome.declarativeNetRequest.getDynamicRules();
  await chrome.declarativeNetRequest.updateDynamicRules({
    removeRuleIds: existing.map((rule) => rule.id),
    addRules: body.rules,
  });
  await chrome.storage.local.set({ [VERSION_KEY]: body.version });
}

async function setBadge(text, title) {
  await chrome.action.setBadgeText({ text });
  if (text) {
    await chrome.action.setBadgeBackgroundColor({ color: "#b3261e" });
  }
  await chrome.action.setTitle({ title });
}

async function syncRules() {
  try {
    const body = await fetchRules();
    const stored = await chrome.storage.local.get(VERSION_KEY);
    if (stored[VERSION_KEY] !== body.version) {
      await applyRules(body);
    }
    await setBadge("", body.rules.length ? `Dourmouse Lockdown: ${body.rules.length} address(es) blocked` : "Dourmouse Lockdown: nothing blocked");
  } catch (error) {
    // Keep the last rules: Dourmouse being closed must not switch protection off.
    await setBadge("!", `Dourmouse Lockdown: cannot reach Dourmouse, keeping the last rules (${error.message})`);
  }
}

// One sync at a time, so a slow poll cannot interleave with the next.
let running = null;
function syncOnce() {
  if (!running) {
    running = syncRules().finally(() => {
      running = null;
    });
  }
  return running;
}

async function ensureAlarm() {
  const existing = await chrome.alarms.get(ALARM_NAME);
  if (!existing) {
    await chrome.alarms.create(ALARM_NAME, { periodInMinutes: POLL_MINUTES });
  }
}

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM_NAME) {
    syncOnce();
  }
});
chrome.runtime.onStartup.addListener(() => {
  ensureAlarm().then(syncOnce);
});
chrome.runtime.onInstalled.addListener(() => {
  ensureAlarm().then(syncOnce);
});
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.port) {
    syncOnce();
  }
});

// The worker also wakes for any of the events above; this covers a plain restart.
ensureAlarm().then(syncOnce);
