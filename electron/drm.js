// Dourmouse native shell -- Widevine DRM readiness (Phase B3).
//
// CommonJS with no Electron imports, like policy.js, so plain node can test every rule.
//
// Stock Electron has NO Widevine: video from Netflix, Disney+, Spotify's web player and the
// like cannot play in it. The castLabs build of Electron ("Electron for Content Security",
// ECS) adds a Widevine CDM and a `components` module, and is installed by the opt-in script
// scripts/install_drm_electron.sh (design: ~/Documents/DOURMOUSE/B3_DRM_PLAN.md). This file
// does not install anything. It only lets main.js ask, without ever throwing, "is this the
// build that has Widevine, and is the CDM ready?", and describes the answer honestly.

const DRM_PLAN = "B3_DRM_PLAN.md";

// Waits for castLabs's `components` (the CDM download and registration) when that module
// exists. On stock Electron it does not exist and the answer is simply "not available".
// Never throws and never waits longer than timeoutMs.
async function startDrm(electronModule, { timeoutMs = 20000, log = () => {} } = {}) {
  const state = { checked: true, hasComponents: false, ready: false, status: null, error: "" };
  let components = null;
  try {
    components = electronModule && electronModule.components;
  } catch {
    components = null;
  }
  if (!components || typeof components.whenReady !== "function") return state;
  state.hasComponents = true;
  let timer = null;
  try {
    await Promise.race([
      Promise.resolve(components.whenReady()),
      new Promise((_resolve, reject) => {
        timer = setTimeout(() => reject(new Error(`the content decryption module was not ready after ${Math.round(timeoutMs / 1000)} s`)), timeoutMs);
        if (timer && typeof timer.unref === "function") timer.unref();
      }),
    ]);
    state.ready = true;
  } catch (exc) {
    state.error = String((exc && exc.message) || exc).slice(0, 200);
    log("DRM components were not ready (non-fatal):", state.error);
  } finally {
    if (timer) clearTimeout(timer);
  }
  try {
    state.status = typeof components.status === "function" ? components.status() : null;
  } catch {
    state.status = null;
  }
  return state;
}

// What the engine itself says about Widevine, from a real call to the standard EME API made
// in a secure page. `probe` is { ok, why, robustness } or null when it could not be run.
function describeDrm(state, probe, versions = {}) {
  const st = state || { checked: false, hasComponents: false, ready: false, error: "" };
  const electron = String(versions.electron || "unknown");
  const out = {
    build: st.hasComponents ? "castlabs-ecs" : "stock-electron",
    electron,
    widevine: "not available",
    ready: false,
    line: "",
    plan: DRM_PLAN,
  };
  if (st.disabled) {
    out.widevine = "turned off";
    out.line = "DRM: turned off by DOURMOUSE_DRM=0. Protected video will not play.";
    return out;
  }
  const engineSays = probe && typeof probe === "object" ? probe : null;
  if (engineSays && engineSays.ok === true) {
    out.widevine = "available";
    out.ready = true;
    out.level = "software (L3)";
    out.line = `Widevine is available (${out.level}). Sites that need only plain Widevine can play protected video. Sites that demand a verified media path (Netflix, Disney+) also need a signed build: see ${DRM_PLAN}.`;
    return out;
  }
  if (!st.checked) {
    out.widevine = "not checked yet";
    out.line = "DRM status has not been checked yet.";
    return out;
  }
  if (!st.hasComponents) {
    out.line = `DRM: not available. This is stock Electron ${electron}, which has no Widevine, so protected video (Netflix, Disney+, Spotify web) will not play. An opt-in build that adds it is described in ${DRM_PLAN}.`;
    return out;
  }
  if (!st.ready) {
    out.widevine = "not ready";
    out.line = `DRM: this build has the Widevine component, but it is not ready${st.error ? ` (${st.error})` : ""}. It downloads on first start and needs a network connection.`;
    return out;
  }
  out.widevine = "not available";
  out.line = `DRM: the Widevine component reports ready, but the engine refused the key system${engineSays && engineSays.why ? ` (${engineSays.why})` : ""}. Protected video is not expected to play.`;
  return out;
}

// The code run (in an isolated world of the console page, a secure context) to ask the engine
// whether Widevine answers. It resolves to { ok, why, robustness }. It touches nothing else.
const EME_PROBE_SOURCE = `(async () => {
  try {
    if (!navigator.requestMediaKeySystemAccess) return { ok: false, why: "this page has no EME support" };
    const access = await navigator.requestMediaKeySystemAccess("com.widevine.alpha", [{
      initDataTypes: ["cenc"],
      audioCapabilities: [{ contentType: 'audio/mp4; codecs="mp4a.40.2"', robustness: "SW_SECURE_CRYPTO" }],
      videoCapabilities: [{ contentType: 'video/mp4; codecs="avc1.42E01E"', robustness: "SW_SECURE_DECODE" }],
    }]);
    const cfg = access.getConfiguration();
    const vc = (cfg.videoCapabilities || [])[0] || {};
    return { ok: true, robustness: String(vc.robustness || "") };
  } catch (e) {
    return { ok: false, why: String((e && e.message) || e).slice(0, 160) };
  }
})()`;

module.exports = { DRM_PLAN, startDrm, describeDrm, EME_PROBE_SOURCE };
