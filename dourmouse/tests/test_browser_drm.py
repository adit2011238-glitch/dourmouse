"""Phase B3: Widevine DRM readiness. Nothing is installed and nothing is downloaded; these tests
pin that main.js is READY for the opt-in castLabs build (it asks `components` only when the
module exists, never throws when it is absent, and reports the truth), that the opt-in script
and the plan exist and are safe by default, and that package.json was not changed."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from dourmouse.tests.b3_harness import ELECTRON, NODE, run_scenario

ROOT = Path(__file__).resolve().parents[2]
MAIN_CODE = (ELECTRON / "main.js").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def run(tmp_path, body):
    script = tmp_path / "d.js"
    script.write_text(f"const d = require({str(ELECTRON / 'drm.js')!r});\nconst R = {{}};\n(async () => {{\n{body}\nconsole.log(JSON.stringify(R));\n}})();\n", encoding="utf-8")
    proc = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestAskingWithoutEverThrowing:
    def test_stock_electron_has_no_components_so_nothing_is_awaited(self, tmp_path):
        out = run(tmp_path, """
R.a = await d.startDrm({});
R.b = await d.startDrm(null);
R.c = await d.startDrm({ components: undefined });
R.e = await d.startDrm({ get components() { throw new Error('boom'); } });
R.f = await d.startDrm({ components: {} });
R.g = await d.startDrm({ components: { whenReady: 'not a function' } });
""")
        for k in "abcefg":
            assert out[k] == {"checked": True, "hasComponents": False, "ready": False, "status": None, "error": ""}, k

    def test_a_build_with_components_is_awaited_and_its_status_is_kept(self, tmp_path):
        out = run(tmp_path, """
R.ready = await d.startDrm({ components: { whenReady: async () => 'x', status: () => ({ widevinecdm: { status: 'installed', version: '4.10' } }) } });
R.sync = await d.startDrm({ components: { whenReady: () => 1 } });
""")
        assert out["ready"]["hasComponents"] is True and out["ready"]["ready"] is True and out["ready"]["status"]["widevinecdm"]["version"] == "4.10"
        assert out["sync"]["ready"] is True and out["sync"]["status"] is None

    def test_a_rejection_a_throw_and_a_hang_are_all_survived(self, tmp_path):
        out = run(tmp_path, """
const logs = [];
R.reject = await d.startDrm({ components: { whenReady: () => Promise.reject(new Error('download failed')) } }, { log: (...a) => logs.push(a.join(' ')) });
R.throws = await d.startDrm({ components: { whenReady: () => { throw new Error('sync throw'); }, status: () => { throw new Error('status broke'); } } });
const keepAlive = setInterval(() => {}, 20); // the app's own event loop is always alive; a bare script's is not
const t0 = Date.now();
R.hang = await d.startDrm({ components: { whenReady: () => new Promise(() => {}) } }, { timeoutMs: 80 });
R.hangMs = Date.now() - t0;
clearInterval(keepAlive);
R.logged = logs.length;
""")
        assert out["reject"]["ready"] is False and "download failed" in out["reject"]["error"] and out["logged"] == 1
        assert out["throws"]["ready"] is False and "sync throw" in out["throws"]["error"] and out["throws"]["status"] is None
        assert out["hang"]["ready"] is False and "not ready after" in out["hang"]["error"] and out["hangMs"] < 2000


class TestTheStatusLineIsHonest:
    def test_not_checked_yet_stock_not_ready_ready_disabled(self, tmp_path):
        out = run(tmp_path, """
const v = { electron: '44.3.0' };
R.unchecked = d.describeDrm({ checked: false }, null, v);
R.stock = d.describeDrm({ checked: true, hasComponents: false, ready: false, error: '' }, { ok: false, why: 'no key system' }, v);
R.stockNoProbe = d.describeDrm({ checked: true, hasComponents: false }, null, v);
R.notReady = d.describeDrm({ checked: true, hasComponents: true, ready: false, error: 'timed out' }, null, v);
R.refused = d.describeDrm({ checked: true, hasComponents: true, ready: true }, { ok: false, why: 'Unsupported keySystem' }, v);
R.available = d.describeDrm({ checked: true, hasComponents: true, ready: true }, { ok: true, robustness: 'SW_SECURE_DECODE' }, v);
R.off = d.describeDrm({ checked: true, disabled: true }, { ok: true }, v);
R.nothing = d.describeDrm(null, null, {});
""")
        assert out["unchecked"]["widevine"] == "not checked yet" and out["unchecked"]["ready"] is False
        for k in ("stock", "stockNoProbe"):
            r = out[k]
            assert r["widevine"] == "not available" and r["ready"] is False and r["build"] == "stock-electron"
            assert "not available" in r["line"] and "stock Electron 44.3.0" in r["line"] and "B3_DRM_PLAN.md" in r["line"]
        assert out["notReady"]["widevine"] == "not ready" and "timed out" in out["notReady"]["line"] and out["notReady"]["build"] == "castlabs-ecs"
        assert out["refused"]["widevine"] == "not available" and "Unsupported keySystem" in out["refused"]["line"] and out["refused"]["ready"] is False
        a = out["available"]
        assert a["widevine"] == "available" and a["ready"] is True and "Widevine is available" in a["line"] and "Netflix" in a["line"]
        assert out["off"]["widevine"] == "turned off" and out["off"]["ready"] is False and "DOURMOUSE_DRM=0" in out["off"]["line"]
        assert out["nothing"]["widevine"] == "not checked yet"

    def test_the_probe_asks_the_standard_api_for_the_widevine_key_system_and_nothing_else(self):
        src = (ELECTRON / "drm.js").read_text(encoding="utf-8")
        probe = src.split("const EME_PROBE_SOURCE =")[1]
        assert "requestMediaKeySystemAccess(\"com.widevine.alpha\"" in probe
        for banned in ("fetch(", "XMLHttpRequest", "localStorage", "document.cookie", "ipcRenderer", "require("):
            assert banned not in probe


SHELL = r'''
main(async () => {
  T.ensurePaneView();
  T.showPane();
  await handlers["pane:screen"](consoleEvt(), true);
  const wc = T.tabs.get(T.active()).view.webContents;
  wc.loadURL("https://video.example/watch");
  const ask2 = (url, perm) => new Promise((resolve) => PANE_SESSION.req(wc, perm, resolve, { requestingUrl: url }));
  const chk = (origin) => PANE_SESSION.chk(wc, "mediaKeySystem", origin, {});

  // stock Electron: the fake has no `components`
  T.startDrmCheck();
  await new Promise((r) => setTimeout(r, 30));
  R.stock = { state: T.drmState(), request: await ask2("https://video.example/watch", "mediaKeySystem"), check: chk("https://video.example") };
  R.stockReport = await T.drmReport();
  R.stockIpc = await handlers["drm:status"](consoleEvt());
  R.stockIpcFromPage = await handlers["drm:status"](pageEvt(wc));
  R.stockBridge = (await call("GET", "/drm")).body;
  R.stockStatus = (await call("GET", "/status")).body.drm;

  // a build that has the component, and it is ready
  T.setDrm({ checked: true, hasComponents: true, ready: true, status: null, error: "" });
  R.ready = {
    top: await ask2("https://video.example/watch", "mediaKeySystem"),
    foreignFrame: await ask2("https://evil.example/", "mediaKeySystem"),
    check: chk("https://video.example"),
    checkForeign: chk("https://evil.example"),
    other: [await ask2("https://video.example/watch", "display-capture"), await ask2("https://video.example/watch", "midi")],
    queued: T.promptQueue.size(),
  };
  R.readyBridge = (await call("GET", "/status")).body.drm;

  // present but not ready
  T.setDrm({ checked: true, hasComponents: true, ready: false, status: null, error: "x" });
  R.notReady = await ask2("https://video.example/watch", "mediaKeySystem");
  R.sentToConsole = JSON.stringify(calls.sent).length >= 0;
});
'''


@pytest.fixture(scope="module")
def shell(tmp_path_factory):
    return run_scenario(tmp_path_factory.mktemp("drm"), SHELL)


class TestTheShellOnStockElectron:
    def test_nothing_throws_and_the_state_says_stock(self, shell):
        assert shell["stock"]["state"] == {"checked": True, "hasComponents": False, "ready": False, "status": None, "error": ""}

    def test_the_key_system_stays_refused_for_requests_and_checks(self, shell):
        assert shell["stock"]["request"] is False and shell["stock"]["check"] is False

    def test_the_report_says_not_available_over_ipc_and_the_bridge_and_a_page_cannot_ask(self, shell):
        assert shell["stockReport"]["widevine"] == "not available" and shell["stockReport"]["build"] == "stock-electron"
        assert shell["stockIpc"]["ok"] is True and shell["stockIpc"]["widevine"] == "not available" and "not available" in shell["stockIpc"]["line"]
        assert shell["stockIpcFromPage"] == {"ok": False, "error": "only the console may do that"}
        assert shell["stockBridge"]["ok"] is True and shell["stockBridge"]["widevine"] == "not available" and shell["stockBridge"]["ready"] is False
        assert shell["stockStatus"] == {"build": "stock-electron", "ready": False}


class TestWhenTheComponentIsReady:
    def test_only_the_top_page_gets_the_key_system_and_nothing_else_opens_up(self, shell):
        r = shell["ready"]
        assert r["top"] is True and r["foreignFrame"] is False and r["check"] is True and r["checkForeign"] is False
        assert r["other"] == [False, False] and r["queued"] == 0  # screen capture and MIDI are still refused, nothing was queued
        assert shell["readyBridge"] == {"build": "castlabs-ecs", "ready": True}

    def test_present_but_not_ready_is_still_refused(self, shell):
        assert shell["notReady"] is False


class TestTheOptInPieces:
    def test_the_script_is_valid_bash_and_does_nothing_without_yes(self):
        script = ROOT / "scripts" / "install_drm_electron.sh"
        assert script.exists()
        bash = shutil.which("bash")
        assert subprocess.run([bash, "-n", str(script)], capture_output=True, check=False).returncode == 0
        text = script.read_text(encoding="utf-8")
        # the plan is printed and the script exits before any step unless --yes was passed
        assert text.index('if [ "$YES" -ne 1 ]; then') < text.index("npm install")
        assert text.index('if [ "$YES" -ne 1 ]; then') < text.index("mkdir -p \"$BACKUP\"")
        assert "\u2014" not in text

    def test_the_script_never_creates_an_account_or_handles_a_password(self):
        text = (ROOT / "scripts" / "install_drm_electron.sh").read_text(encoding="utf-8")
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#") and "say " not in line.split("|")[0][:12])
        assert "account signup" not in code.replace('say "    $EVS_VENV/bin/python -m castlabs_evs.account signup', "")
        for banned in ("read -s", "read -p", "PASSWORD", "security add-generic-password", "rm -rf /", "sudo"):
            assert banned not in text, banned
        assert text.count("rm -rf") == 1 and 'rm -rf "$PROBE_DIR"' in text  # the one throwaway folder it made itself

    def test_the_script_keeps_package_json_untouched_unless_save_is_given(self):
        text = (ROOT / "scripts" / "install_drm_electron.sh").read_text(encoding="utf-8")
        assert 'SAVE_FLAG="--no-save"; [ "$SAVE" -eq 1 ] && SAVE_FLAG="--save-dev"' in text

    def test_package_json_was_not_changed_by_this_phase(self):
        pkg = json.loads((ELECTRON / "package.json").read_text(encoding="utf-8"))
        for section in ("dependencies", "devDependencies"):
            for name, spec in pkg.get(section, {}).items():
                assert "castlabs" not in spec.lower() and "wvcus" not in spec.lower(), (name, spec)
        assert pkg["devDependencies"]["electron"] == "^44.3.0"

    def test_the_plan_document_covers_what_was_asked(self):
        plan = Path.home() / "Documents" / "DOURMOUSE" / "B3_DRM_PLAN.md"
        if not plan.exists():
            pytest.skip("the owner's tracking folder is not on this machine")
        text = plan.read_text(encoding="utf-8")
        for needle in ("castLabs", "Electron for Content Security", "VMP", "Widevine L3", "Netflix", "Disney+", "YouTube", "Spotify",
                       "Rollback", "Risks", "castlabs_evs.vmp sign-pkg", "npm install --no-save", "components.whenReady", "install_drm_electron.sh", "Tests the owner should run"):
            assert needle in text, needle
        assert "\u2014" not in text

    def test_main_js_asks_components_only_through_the_guarded_helper(self):
        assert "components.whenReady" not in MAIN_CODE  # never called directly from main.js
        assert "drmLib.startDrm(electronApi" in MAIN_CODE
        assert re.search(r"if \(permission === \"mediaKeySystem\"\) return callback\(drmState\.ready === true\);", MAIN_CODE)
        assert "DOURMOUSE_DRM" in MAIN_CODE
