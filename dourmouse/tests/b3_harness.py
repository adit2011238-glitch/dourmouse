"""Shared harness for the Phase B3 shell tests (extensions, profiles, import, DRM).

It builds on the B2 harness (``b2_harness.PRELUDE``): electron/main.js is loaded under plain
node with a fake ``electron`` module, so its OWN IPC handlers, flows and pane bridge run. B3
needs a few more fakes, added by patching that prelude (and failing loudly if a patch no longer
applies, so a change to the B2 harness cannot silently weaken these tests):

* one fake session PER PARTITION (``sessionFor``), each with ``extensions`` recording
  ``loadExtension`` and ``removeExtension``, so profile partitions and extension loading are
  visible;
* ``dialog.showOpenDialog`` that returns the paths the test queued (or cancels), and a
  ``showMessageBox`` that records exactly what the owner would have been shown;
* ``T_DATA`` to reuse a user-data folder across two runs (to prove what a restart remembers);
* hooks into main.js for the profile and extension state.

What this cannot prove is what Chromium and the real macOS dialogs do. That was checked live in
an isolated copy of the app and is recorded in the B3 finding.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from dourmouse.tests import b2_harness
from dourmouse.tests.b2_harness import ELECTRON, NODE, free_port

__all__ = ["ELECTRON", "NODE", "run_scenario", "free_port"]


def _patch(src: str, old: str, new: str) -> str:
    assert old in src, f"b3_harness: the B2 harness no longer contains {old[:70]!r}"
    return src.replace(old, new, 1)


def _build_prelude() -> str:
    src = b2_harness.PRELUDE
    src = _patch(src, 'const DATA = fs.mkdtempSync(path.join(os.tmpdir(), "dm-b2-"));',
                 'const DATA = process.env.T_DATA || fs.mkdtempSync(path.join(os.tmpdir(), "dm-b3-"));')
    # one session per partition; the default partition keeps the B2 name
    src = _patch(src, "const PANE_SESSION = new FakeSession();", r'''
const extCalls = { loaded: [], removed: [], failNext: null, seq: 0, delay: 0 };
class ExtFakeSession extends FakeSession {
  constructor(partition) {
    super();
    this.partition = partition; this.cleared = 0; this.cacheCleared = 0;
    this.extensions = {
      loadExtension: async (dir, opts) => {
        if (extCalls.delay) await new Promise((r) => setTimeout(r, extCalls.delay));
        if (extCalls.failNext) { const m = extCalls.failNext; extCalls.failNext = null; throw new Error(m); }
        extCalls.seq += 1;
        const id = "fakeext" + extCalls.seq;
        extCalls.loaded.push({ partition, dir, opts, id });
        return { id, name: "x", version: "1", path: dir, manifest: {}, url: "chrome-extension://" + id + "/" };
      },
      removeExtension: (id) => { extCalls.removed.push({ partition, id }); },
      getAllExtensions: () => [],
    };
  }
  clearStorageData() { this.cleared += 1; return Promise.resolve(); }
  clearCache() { this.cacheCleared += 1; return Promise.resolve(); }
}
const sessions = {};
const sessionFor = (partition) => (sessions[partition] = sessions[partition] || new ExtFakeSession(partition));
const PANE_SESSION = sessionFor("persist:dourmouse-browser");''')
    src = _patch(src, "this.webContents = new FakeWC(PANE_SESSION); this.bounds = null; }",
                 "this.webContents = new FakeWC(sessionFor((opts && opts.webPreferences && opts.webPreferences.partition) || 'persist:dourmouse-browser')); this.bounds = null; }")
    src = _patch(src, "session: { defaultSession: APP_SESSION, fromPartition: () => PANE_SESSION },",
                 "session: { defaultSession: APP_SESSION, fromPartition: (p) => sessionFor(p) },")
    src = _patch(src, "const calls = { sent: [], menus: [], dialogs: [], asked: [], openExternal: [] };",
                 "const calls = { sent: [], menus: [], dialogs: [], asked: [], openExternal: [], opens: [] };\nlet openPaths = [];")
    src = _patch(src, "  dialog: { showErrorBox() {},",
                 "  dialog: { showErrorBox() {}, showOpenDialog: async (_w, opts) => { calls.opens.push(opts); const p = openPaths.shift(); return p ? { canceled: false, filePaths: [p] } : { canceled: true, filePaths: [] }; },")
    # a message box that can answer per call, and can be held open to test the one-dialog-at-a-time rule
    src = _patch(src, "showMessageBox: async (_w, opts) => { calls.dialogs.push(opts); return { response: dialogAnswer }; } },",
                 "showMessageBox: async (_w, opts) => { calls.dialogs.push(opts); if (hold.box) await hold.box; return { response: Array.isArray(dialogAnswer) ? (dialogAnswer.length ? dialogAnswer.shift() : 1) : dialogAnswer }; } },")
    src = _patch(src, "let dialogAnswer = 0;", "let dialogAnswer = 0;\nconst hold = { box: null };")
    src = _patch(src, "  setWindow: (w) => { mainWindow = w; },", r'''  setWindow: (w) => { mainWindow = w; },
  switchProfile, storesFor, activeProfile: () => activeProfileName, profileRegistry: () => profileRegistry, extRegistry,
  statusMap, paneSession, drmState: () => drmState, setDrm: (d) => { drmState = d; }, startDrmCheck, startExtensions, loadInTab,
  closeAllTabs, extensionsDir, browserDataDir, tabPrefs: () => TAB_WEB_PREFERENCES, drmReport,''')
    return src


PRELUDE = _build_prelude()


def run_scenario(tmp_path: Path, scenario: str, *, platform: str = "darwin", data: Path | None = None, extra_env: dict | None = None) -> dict:
    script = tmp_path / "scenario.js"
    script.write_text(PRELUDE + scenario, encoding="utf-8")
    env = {
        **os.environ,
        "T_MAIN": str(ELECTRON / "main.js"),
        "T_UI_PORT": str(free_port()),
        "T_PLATFORM": platform,
        "DOURMOUSE_ELECTRON_PANE_PORT": str(free_port()),
        **({"T_DATA": str(data)} if data else {}),
        **(extra_env or {}),
    }
    proc = subprocess.run([str(NODE), str(script)], capture_output=True, text=True, timeout=90, env=env, check=False)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:")]
    assert lines, f"the scenario printed no result\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
    out = json.loads(lines[-1][len("RESULT:"):])
    assert "__error" not in out, out["__error"]
    return out
