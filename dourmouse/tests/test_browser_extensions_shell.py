"""Phase B3: unpacked Chrome extensions in the Electron shell, driven for real.

electron/main.js is loaded under plain node with a fake ``electron`` (see b3_harness.py), so its
OWN add, enable, disable and remove flows, native dialogs, copy and fingerprint check, loading
and pane bridge run. The fake session records ``loadExtension`` and ``removeExtension``. What
this cannot prove is what Electron's real extension system does with the extension; that was
checked live in an isolated copy of the app with a small extension of our own and is recorded in
the B3 finding.
"""

from __future__ import annotations

import re

import pytest

from dourmouse.tests.b3_harness import ELECTRON, NODE, run_scenario

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

MAIN_CODE = (ELECTRON / "main.js").read_text(encoding="utf-8")

EXT = r'''
const fsx = require("fs");
const B = () => path.join(USERDATA, "browser");
const mode = (p) => { try { return fsx.statSync(p).mode & 0o777; } catch { return null; } };
const SRC = path.join(DATA, "src-ext");
const writeExt = (dir, manifest, extra) => {
  fsx.mkdirSync(dir, { recursive: true });
  fsx.writeFileSync(path.join(dir, "manifest.json"), typeof manifest === "string" ? manifest : JSON.stringify(manifest));
  for (const [f, c] of Object.entries(extra || {})) fsx.writeFileSync(path.join(dir, f), c);
};
const MANIFEST = { manifest_version: 3, name: "Page Tagger", version: "1.2", permissions: ["storage", "cookies"], content_scripts: [{ matches: ["http://127.0.0.1/*"], js: ["tag.js"] }] };
const extDirs = () => { try { return fsx.readdirSync(path.join(B(), "extensions")); } catch { return []; } };
const wait_ = (ms) => new Promise((r) => setTimeout(r, ms));
const noPaths = (v) => !JSON.stringify(v).includes(DATA) && !JSON.stringify(v).includes("/extensions/");

main(async () => {
  T.ensurePaneView();
  T.showPane();
  await handlers["pane:screen"](consoleEvt(), true);
  const wc = T.tabs.get(T.active()).view.webContents;

  // 0. nothing yet, and the honest note is delivered with the list
  const list0 = await handlers["ext:list"](consoleEvt());
  R.list0 = { ok: list0.ok, supported: list0.supported, count: list0.extensions.length, note: list0.note };

  // 1. only the console's top page may call any of it, and no dialog opens for anybody else
  const refused = {};
  for (const [name, args] of [["ext:list", []], ["ext:add", []], ["ext:enable", ["0123456789abcdef"]], ["ext:disable", ["0123456789abcdef"]], ["ext:remove", ["0123456789abcdef"]]]) {
    refused[name] = [await handlers[name](pageEvt(wc), ...args), await handlers[name](strangerEvt(), ...args), await handlers[name](consoleSubframeEvt(), ...args)];
  }
  R.refused = refused;
  R.refusedOpenedNothing = { dialogs: calls.dialogs.length, opens: calls.opens.length };
  R.badIds = [await handlers["ext:enable"](consoleEvt(), 5), await handlers["ext:remove"](consoleEvt(), null)];

  // 2. a cancelled folder picker
  R.cancelledPick = await handlers["ext:add"](consoleEvt());
  R.afterCancelledPick = { dialogs: calls.dialogs.length, dirs: extDirs() };

  // 3. folders that are not usable: no dialog is shown for them
  openPaths.push(path.join(DATA, "nothing-here"));
  R.noFolder = await handlers["ext:add"](consoleEvt());
  writeExt(path.join(DATA, "badjson"), "{ not json");
  openPaths.push(path.join(DATA, "badjson"));
  R.badJson = await handlers["ext:add"](consoleEvt());
  writeExt(path.join(DATA, "noname"), { manifest_version: 3, version: "1" });
  openPaths.push(path.join(DATA, "noname"));
  R.noName = await handlers["ext:add"](consoleEvt());
  R.noDialogForUnusable = calls.dialogs.length;

  // 4. the owner presses Cancel on the native confirmation: nothing is copied or stored
  writeExt(SRC, MANIFEST, { "tag.js": "document.documentElement.dataset.tagged = '1';" });
  openPaths.push(SRC);
  dialogAnswer = 1;
  R.cancelledConfirm = await handlers["ext:add"](consoleEvt());
  R.confirmDialog = calls.dialogs.map((d) => ({ title: d.title, message: d.message, detail: d.detail, buttons: d.buttons, defaultId: d.defaultId, cancelId: d.cancelId }));
  R.afterCancelledConfirm = { dirs: extDirs(), file: readJson("extensions.json"), loaded: extCalls.loaded.length };

  // 5. confirmed: copied into the app's own folder, stored, and the COPY is what is loaded
  calls.dialogs.length = 0;
  dialogAnswer = 0;
  openPaths.push(SRC);
  const added = await handlers["ext:add"](consoleEvt());
  R.added = added;
  R.addedNoPaths = noPaths(added);
  const id = added.extension && added.extension.id;
  R.copy = { dirs: extDirs() === undefined ? null : extDirs().length, hasManifest: fsx.existsSync(path.join(B(), "extensions", id || "x", "manifest.json")), dirMode: mode(path.join(B(), "extensions")), copyMode: mode(path.join(B(), "extensions", id || "x")) };
  R.loaded = extCalls.loaded.map((l) => ({ partition: l.partition, isCopy: l.dir === path.join(B(), "extensions", id), notSource: l.dir !== SRC, opts: l.opts }));
  R.file = { json: readJson("extensions.json"), mode: mode(path.join(B(), "extensions.json")) };
  R.fileHasNoPath = !JSON.stringify(R.file.json).includes(DATA);
  const list1 = await handlers["ext:list"](consoleEvt());
  R.list1 = list1.extensions;
  R.list1NoPaths = noPaths(list1);

  // 6. editing the folder the owner picked changes nothing that runs
  fsx.writeFileSync(path.join(SRC, "tag.js"), "EVIL");
  R.copyStillOriginal = fsx.readFileSync(path.join(B(), "extensions", id, "tag.js"), "utf8").includes("tagged");

  // 7. a copy that was edited after the approval is refused on the next load
  const sTamper = sessionFor("persist:dourmouse-browser-tamper");
  fsx.writeFileSync(path.join(B(), "extensions", id, "extra.js"), "x");
  const before = extCalls.loaded.length;
  T.startExtensions(sTamper);
  await wait_(30);
  const st = T.statusMap(sTamper).get(id) || {};
  R.tampered = { loadedAgain: extCalls.loaded.length - before, error: st.error, running: st.loaded === true, activeStillRunning: (await handlers["ext:list"](consoleEvt())).extensions[0].loaded };
  fsx.unlinkSync(path.join(B(), "extensions", id, "extra.js"));

  // 8. disable, and enabling again asks natively
  R.disabled = await handlers["ext:disable"](consoleEvt(), id);
  R.afterDisable = { removed: extCalls.removed.map((r) => r.id), enabled: readJson("extensions.json").entries[0].enabled, listLoaded: (await handlers["ext:list"](consoleEvt())).extensions[0].loaded };
  R.disableUnknown = await handlers["ext:disable"](consoleEvt(), "ffffffffffffffff");
  calls.dialogs.length = 0;
  dialogAnswer = 1;
  R.enableCancelled = await handlers["ext:enable"](consoleEvt(), id);
  R.enableDialog = calls.dialogs.map((d) => ({ message: d.message, detail: d.detail, buttons: d.buttons }));
  R.stillOff = readJson("extensions.json").entries[0].enabled === false;
  const loadsBefore = extCalls.loaded.length;
  dialogAnswer = 0;
  R.enabled = await handlers["ext:enable"](consoleEvt(), id);
  R.afterEnable = { loadsAdded: extCalls.loaded.length - loadsBefore, enabled: readJson("extensions.json").entries[0].enabled };
  R.enableUnknown = await handlers["ext:enable"](consoleEvt(), "ffffffffffffffff");

  // 9. a load error is shown, not hidden
  await handlers["ext:disable"](consoleEvt(), id);
  extCalls.failNext = "Manifest version 2 is not supported";
  dialogAnswer = 0;
  await handlers["ext:enable"](consoleEvt(), id);
  R.loadError = (await handlers["ext:list"](consoleEvt())).extensions[0];

  // 10. only one native dialog at a time
  await handlers["ext:disable"](consoleEvt(), id);
  let release; hold.box = new Promise((r) => { release = r; });
  const first = handlers["ext:enable"](consoleEvt(), id);
  await wait_(10);
  R.second = await handlers["ext:enable"](consoleEvt(), id);
  R.secondAdd = await handlers["ext:add"](consoleEvt());
  hold.box = null; release();
  R.firstFinished = (await first).ok;

  // 11. a manifest edited while the confirmation was open is not what gets installed
  const SRC2 = path.join(DATA, "src2");
  writeExt(SRC2, { manifest_version: 3, name: "Honest", version: "1", permissions: ["storage"] });
  openPaths.push(SRC2);
  let release2; hold.box = new Promise((r) => { release2 = r; });
  const pending = handlers["ext:add"](consoleEvt());
  await wait_(20);
  writeExt(SRC2, { manifest_version: 3, name: "Honest", version: "1", permissions: ["storage"], host_permissions: ["<all_urls>"] });
  hold.box = null; release2();
  R.swapped = await pending;
  R.afterSwap = { entries: readJson("extensions.json").entries.map((e) => e.name), dirs: extDirs().length };

  // 12. a folder with a symbolic link inside is refused after the confirmation, and leaves nothing behind
  const SRC3 = path.join(DATA, "src3");
  writeExt(SRC3, { manifest_version: 3, name: "Linky", version: "1" });
  fsx.symlinkSync("/etc/hosts", path.join(SRC3, "hosts.js"));
  openPaths.push(SRC3);
  const dirsBefore = extDirs().length;
  R.linky = await handlers["ext:add"](consoleEvt());
  R.afterLinky = { dirs: extDirs().length - dirsBefore, names: readJson("extensions.json").entries.map((e) => e.name) };

  // 13. remove deletes the copy and the entry
  const removedOk = await handlers["ext:remove"](consoleEvt(), id);
  R.removed = removedOk;
  R.afterRemove = { dirs: extDirs().includes(id), entries: readJson("extensions.json").entries.map((e) => e.name) };
  R.removeUnknown = await handlers["ext:remove"](consoleEvt(), id);

  // 14. the bridge can read a non-secret list and cannot add, enable, disable or remove
  openPaths.push(SRC); dialogAnswer = 0;
  const again = await handlers["ext:add"](consoleEvt());
  const bridge = {};
  bridge.get = (await call("GET", "/extensions")).body;
  bridge.status = (await call("GET", "/status")).body.extensions;
  for (const r of ["/extensions", "/extensions/add", "/extensions/enable", "/extensions/disable", "/extensions/remove", "/extensions/load", "/extension", "/extensions/install"]) {
    bridge["POST " + r] = (await call("POST", r, { id: again.extension.id, path: SRC, url: "https://x.example/e.crx" })).status;
  }
  bridge.noPaths = noPaths(bridge.get);
  bridge.fieldNames = Object.keys(bridge.get.extensions[0]).sort();
  R.bridge = bridge;
});
'''


@pytest.fixture(scope="module")
def ext(tmp_path_factory):
    return run_scenario(tmp_path_factory.mktemp("ext"), EXT)


class TestSupportAndWhoMayCall:
    def test_the_list_says_extensions_are_supported_and_delivers_the_honest_note(self, ext):
        l0 = ext["list0"]
        assert l0["ok"] is True and l0["supported"] is True and l0["count"] == 0
        assert "only part of Chrome" in l0["note"] and "Chrome Web Store is not available" in l0["note"]

    def test_a_page_a_stranger_and_a_console_subframe_are_refused_every_time(self, ext):
        for name, answers in ext["refused"].items():
            for a in answers:
                assert a == {"ok": False, "error": "only the console may do that"}, name
        assert ext["refusedOpenedNothing"] == {"dialogs": 0, "opens": 0}

    def test_an_id_that_is_not_a_string_is_refused(self, ext):
        assert all(a == {"ok": False, "error": "only the console may do that"} for a in ext["badIds"])


class TestAddingNeedsTheOwner:
    def test_a_cancelled_folder_picker_does_nothing(self, ext):
        assert ext["cancelledPick"]["cancelled"] is True
        assert ext["afterCancelledPick"] == {"dialogs": 0, "dirs": []}

    def test_a_folder_that_is_not_an_extension_is_refused_before_any_dialog(self, ext):
        assert "no manifest.json" in ext["noFolder"]["error"]
        assert "not valid JSON" in ext["badJson"]["error"]
        assert "no name" in ext["noName"]["error"]
        assert ext["noDialogForUnusable"] == 0

    def test_the_native_confirmation_names_the_extension_and_its_permissions(self, ext):
        (d,) = ext["confirmDialog"]
        assert d["buttons"] == ["Add extension", "Cancel"] and d["defaultId"] == 1 and d["cancelId"] == 1  # Cancel is the default
        assert 'Add "Page Tagger" (version 1.2)' in d["message"]
        assert "cookies" in d["detail"] and "127.0.0.1" in d["detail"] and "only that copy runs" in d["detail"]

    def test_cancelling_the_confirmation_copies_and_stores_nothing(self, ext):
        assert ext["cancelledConfirm"]["cancelled"] is True
        assert ext["afterCancelledConfirm"] == {"dirs": [], "file": None, "loaded": 0}


class TestWhatIsLoadedIsTheApprovedCopy:
    def test_a_confirmed_add_copies_stores_and_loads_the_copy_not_the_folder(self, ext):
        a = ext["added"]
        assert a["ok"] is True and a["extension"]["name"] == "Page Tagger" and a["extension"]["enabled"] is True and a["extension"]["risk"] == "high"
        assert a["extension"]["loaded"] is True and ext["addedNoPaths"] is True
        assert ext["copy"] == {"dirs": 1, "hasManifest": True, "dirMode": 0o700, "copyMode": 0o700}
        assert ext["loaded"] == [{"partition": "persist:dourmouse-browser", "isCopy": True, "notSource": True, "opts": {"allowFileAccess": False}}]

    def test_the_list_file_is_owner_only_and_holds_no_path(self, ext):
        assert ext["file"]["mode"] == 0o600 and ext["fileHasNoPath"] is True
        e = ext["file"]["json"]["entries"][0]
        assert set(e) == {"id", "name", "version", "enabled", "addedAt", "tree", "risk", "summary"}
        assert ext["list1NoPaths"] is True
        assert set(ext["list1"][0]) == {"id", "name", "version", "enabled", "addedAt", "risk", "summary", "loaded", "error"}

    def test_editing_the_picked_folder_afterwards_changes_nothing_that_runs(self, ext):
        assert ext["copyStillOriginal"] is True

    def test_a_copy_edited_after_the_approval_is_refused_not_run(self, ext):
        t = ext["tampered"]
        assert t["loadedAgain"] == 0 and t["running"] is False
        assert "changed after you approved it" in t["error"]
        assert t["activeStillRunning"] is True  # the other session's refusal does not make this one forget what it loaded

    def test_a_manifest_edited_while_the_confirmation_was_open_is_refused(self, ext):
        assert ext["swapped"]["ok"] is False and "changed while it was being copied" in ext["swapped"]["error"]
        assert ext["afterSwap"]["entries"] == ["Page Tagger"] and ext["afterSwap"]["dirs"] == 1

    def test_a_folder_with_a_symbolic_link_is_refused_and_leaves_nothing_behind(self, ext):
        assert ext["linky"]["ok"] is False and "symbolic link" in ext["linky"]["error"]
        assert ext["afterLinky"] == {"dirs": 0, "names": ["Page Tagger"]}


class TestEnableDisableRemove:
    def test_disabling_unloads_and_is_remembered(self, ext):
        assert ext["disabled"] == {"ok": True}
        assert ext["afterDisable"]["removed"] == ["fakeext1"]
        assert ext["afterDisable"]["enabled"] is False and ext["afterDisable"]["listLoaded"] is False
        assert ext["disableUnknown"]["ok"] is False

    def test_enabling_again_asks_natively_and_cancel_keeps_it_off(self, ext):
        assert ext["enableCancelled"]["cancelled"] is True and ext["stillOff"] is True
        (d,) = ext["enableDialog"]
        assert d["buttons"] == ["Enable", "Cancel"] and "Page Tagger" in d["message"] and "cookies" in d["detail"].lower()
        assert not d["detail"].startswith("Folder:")
        assert ext["enabled"]["ok"] is True and ext["afterEnable"] == {"loadsAdded": 2, "enabled": True}  # the active profile's session and the second one started in step 7: enabling reaches every started profile (FB A-6; this used to pin the single-session bug)
        assert ext["enableUnknown"]["ok"] is False

    def test_a_load_error_is_shown_with_the_engines_own_words(self, ext):
        e = ext["loadError"]
        assert e["enabled"] is True and e["loaded"] is False and "Manifest version 2 is not supported" in e["error"]

    def test_only_one_native_dialog_can_be_open_at_a_time(self, ext):
        assert ext["second"] == {"ok": False, "error": "another confirmation is already open"}
        assert ext["secondAdd"] == {"ok": False, "error": "another confirmation is already open"}
        assert ext["firstFinished"] is True

    def test_removing_deletes_the_copy_and_the_entry(self, ext):
        assert ext["removed"] == {"ok": True}
        assert ext["afterRemove"] == {"dirs": False, "entries": []} or ext["afterRemove"]["dirs"] is False
        assert ext["removeUnknown"]["ok"] is False


class TestTheBridgeCanOnlyRead:
    def test_get_lists_names_and_state_and_every_change_route_is_missing(self, ext):
        b = ext["bridge"]
        assert b["fieldNames"] == ["enabled", "loaded", "name", "risk", "version"]  # no id, no path, no summary
        assert b["noPaths"] is True and b["status"] == {"total": 1, "enabled": 1}
        for route, status in b.items():
            if route.startswith("POST "):
                assert status == 404, route


NOSUPPORT = r'''
main(async () => {
  T.ensurePaneView();
  delete sessionFor("persist:dourmouse-browser").extensions;
  R.list = await handlers["ext:list"](consoleEvt());
  openPaths.push(path.join(DATA, "x"));
  R.add = await handlers["ext:add"](consoleEvt());
  R.noDialog = [calls.dialogs.length, calls.opens.length];
});
'''


def test_a_build_without_extension_support_says_so_and_opens_no_dialog(tmp_path):
    out = run_scenario(tmp_path, NOSUPPORT)
    assert out["list"]["supported"] is False
    assert out["add"] == {"ok": False, "error": "This build of Electron has no extension support."} and out["noDialog"] == [0, 0]


STARTUP = r'''
const fsx = require("fs");
const wait_ = (ms) => new Promise((r) => setTimeout(r, ms));
main(async () => {
  const SRC = path.join(DATA, "src");
  fsx.mkdirSync(SRC, { recursive: true });
  fsx.writeFileSync(path.join(SRC, "manifest.json"), JSON.stringify({ manifest_version: 3, name: "Starter", version: "1", permissions: ["storage"] }));
  T.ensurePaneView();
  openPaths.push(SRC); dialogAnswer = 0;
  await handlers["ext:add"](consoleEvt());
  await handlers["profile:create"](consoleEvt(), "work");
  extCalls.delay = 80;
  await handlers["profile:switch"](consoleEvt(), "work");
  const blank = T.tabs.get(T.active()).view.webContents;
  R.blankLoadsAtOnce = blank.loads.slice();             // the agent's anchor page never waits
  const tab = T.openTab("https://x.example/p");
  R.realPageAtOnce = tab.view.webContents.loads.slice(); // waits for the extensions of this session
  await wait_(250);
  R.realPageLater = tab.view.webContents.loads.slice();
  R.loadedInWork = extCalls.loaded.filter((l) => l.partition === "persist:dourmouse-browser-work").length;
  // a second tab in the same session does not wait again
  const t2 = T.openTab("https://x.example/q");
  R.secondAtOnce = t2.view.webContents.loads.slice();
});
'''


def test_a_new_profile_session_loads_the_enabled_extensions_before_its_first_real_page(tmp_path):
    out = run_scenario(tmp_path, STARTUP)
    assert out["blankLoadsAtOnce"] == ["about:blank"]
    assert out["realPageAtOnce"] == [] and out["realPageLater"] == ["https://x.example/p"]
    assert out["loadedInWork"] == 1
    assert out["secondAtOnce"] == ["https://x.example/q"]


class TestTheWiring:
    def test_every_extension_ipc_handler_starts_with_the_console_check(self):
        for name in ("ext:list", "ext:add", "ext:enable", "ext:disable", "ext:remove"):
            m = re.search(r'ipcMain\.handle\("' + re.escape(name) + r'", (.*?)\n(?:ipcMain|\n)', MAIN_CODE, re.S)
            assert m and "consoleOnly(evt)" in m.group(1), name

    def test_the_two_calls_that_run_code_end_in_a_native_dialog(self):
        add = MAIN_CODE.split("async function addExtensionFlow")[1].split("\nasync function enableExtensionFlow")[0]
        assert "pickNatively(" in add and "confirmNatively(" in add and add.index("confirmNatively(") < add.index("copyTree(")
        en = MAIN_CODE.split("async function enableExtensionFlow")[1].split("\nipcMain.handle")[0]
        assert "confirmNatively(" in en and en.index("confirmNatively(") < en.index("setEnabled(")

    def test_the_pane_bridge_has_no_route_that_adds_enables_or_removes(self):
        bridge = MAIN_CODE.split("function startPaneBridge()")[1].split("// Native notifications")[0]
        for needle in ("addExtensionFlow", "enableExtensionFlow", "loadExtensionInto", "saveExtRegistry", "ext:add", "extLib.addEntry", "extLib.setEnabled", "extLib.removeEntry"):
            assert needle not in bridge, needle
        assert 'route === "/extensions"' in bridge and "isGet && route === \"/extensions\"" in bridge

    def test_a_web_page_cannot_reach_it(self):
        pre = (ELECTRON / "preload.js").read_text(encoding="utf-8")
        assert pre.count('"ext:add"') == 1 and "contextBridge" in pre
        assert "webPreferences" not in MAIN_CODE.split("function startPaneBridge()")[1].split("// Native notifications")[0].replace("webPreferences: TAB", "")

    def test_files_are_loaded_with_no_file_access(self):
        assert "allowFileAccess: false" in MAIN_CODE

    def test_no_em_dash_was_added(self):
        assert "\u2014" not in (ELECTRON / "extensions.js").read_text(encoding="utf-8")
