"""Phase B3: browser profiles in the Electron shell, driven for real.

electron/main.js is loaded under plain node with a fake ``electron`` (see b3_harness.py), so its
OWN profile switch, stores, IPC handlers and pane bridge run. What this cannot prove is what
Chromium does with a second persistent partition (separate cookies, separate logins); that was
checked live in an isolated copy of the app and is recorded in the B3 finding.
"""

from __future__ import annotations

import json
import re
import stat

import pytest

from dourmouse.tests.b3_harness import ELECTRON, NODE, run_scenario

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

MAIN_CODE = (ELECTRON / "main.js").read_text(encoding="utf-8")

PW1 = "Default-Pw-Zq7!"
PW2 = "Work-Pw-Lm4#"

PROFILES = r'''
const PW1 = "%s", PW2 = "%s";
const fsx = require("fs");
const B = () => path.join(USERDATA, "browser");
const mode = (p) => { try { return fsx.statSync(p).mode & 0o777; } catch { return null; } };
const files = (d) => { try { return fsx.readdirSync(d).filter((f) => !f.endsWith(".tmp")).sort(); } catch { return null; } };
const savePassword = async (wc, user, pw) => {
  listeners["dm:pw-submit"](pageEvt(wc), { username: user, password: pw });
  const offer = T.privacyState().save;
  return handlers["pw:save-answer"](consoleEvt(), offer.id, "save");
};
main(async () => {
  // a browser folder and a permissions file that already exist with loose permissions (the B2 state)
  fsx.mkdirSync(B(), { recursive: true, mode: 0o755 });
  fsx.chmodSync(B(), 0o755);
  fsx.writeFileSync(path.join(B(), "permissions.json"), JSON.stringify({ sites: {} }), { mode: 0o644 });
  fsx.chmodSync(path.join(B(), "permissions.json"), 0o644);

  T.ensurePaneView();
  T.showPane();
  await handlers["pane:screen"](consoleEvt(), true);
  const wc1 = T.tabs.get(T.active()).view.webContents;
  R.defaultPartition = [T.tabPrefs().partition, wc1.session.partition];
  R.startProfile = T.activeProfile();
  wc1.loadURL("https://a.example/one");
  await call("POST", "/bookmarks/add", { url: "https://a.example/", title: "A default" });
  R.savedDefault = (await savePassword(wc1, "alice", PW1)).ok;
  await handlers["site:perm-set"](consoleEvt(), "https://a.example", "camera", "allow");
  T.flushBrowserStores();
  R.defaultFiles = files(B());
  R.loosePermsFixed = { dir: mode(B()), permissions: mode(path.join(B(), "permissions.json")), passwords: mode(path.join(B(), "passwords.json")) };
  R.defaultHistory = JSON.parse(readRaw("history.json")).map((e) => e.url);

  // who may manage profiles: only the console's top page
  const refusedFrom = {};
  refusedFrom.page = await handlers["profile:create"](pageEvt(wc1), "evil");
  refusedFrom.stranger = await handlers["profile:create"](strangerEvt(), "evil");
  refusedFrom.subframe = await handlers["profile:create"](consoleSubframeEvt(), "evil");
  refusedFrom.listPage = await handlers["profile:list"](pageEvt(wc1));
  refusedFrom.switchPage = await handlers["profile:switch"](pageEvt(wc1), "default");
  refusedFrom.removePage = await handlers["profile:remove"](pageEvt(wc1), "x");
  R.refusedFrom = refusedFrom;
  R.noEvilProfile = T.profileRegistry().names.includes("evil");

  // create: validation, then the list
  R.badNames = [];
  for (const n of ["", "default", "../x", "a b", "x".repeat(30), 5, null]) R.badNames.push((await handlers["profile:create"](consoleEvt(), n)).ok === true);
  R.created = await handlers["profile:create"](consoleEvt(), "Work");
  R.dupe = await handlers["profile:create"](consoleEvt(), "work");
  R.listed = (await handlers["profile:list"](consoleEvt())).profiles;
  R.createDidNotSwitch = T.activeProfile();

  // something waiting in the default profile must not survive the switch
  const wait = ask(wc1, "geolocation", { requestingUrl: "https://a.example/one", isMainFrame: true });
  const waitState = watch(wait);
  await wait_(20);
  R.promptWaiting = T.promptQueue.size();
  listeners["dm:pw-submit"](pageEvt(wc1), { username: "bob", password: PW1 + "x" });
  R.saveWaiting = Boolean(T.privacyState().save);

  // switch
  const sw = await handlers["profile:switch"](consoleEvt(), "work");
  R.switch = sw;
  R.switchUnknown = await handlers["profile:switch"](consoleEvt(), "ghost");
  R.switchSame = await handlers["profile:switch"](consoleEvt(), "work");
  await wait_(20);
  R.promptAfter = { size: T.promptQueue.size(), answer: waitState.value, pending: T.pendingSaves.size, save: T.privacyState().save };
  R.oldTabClosed = wc1.destroyed;
  R.tabsAfter = T.tabs.size;
  const wc2 = T.tabs.get(T.active()).view.webContents;
  R.newTab = { partition: wc2.session.partition, prefs: T.tabPrefs().partition, firstLoad: wc2.loads[0], different: wc2 !== wc1 };
  R.workEmpty = {
    history: (await call("GET", "/history")).body.history.length,
    bookmarks: (await call("GET", "/bookmarks")).body.bookmarks.length,
    passwords: T.vault.list().length,
    sites: Object.keys(T.siteTable()).length,
    paneProfile: T.paneState().profile,
    status: (await call("GET", "/status")).body.profile,
  };
  wc2.loadURL("https://w.example/two");
  await call("POST", "/bookmarks/add", { url: "https://w.example/", title: "W work" });
  R.savedWork = (await savePassword(wc2, "carol", PW2)).ok;
  await handlers["site:perm-set"](consoleEvt(), "https://w.example", "microphone", "block");
  // a second tab opened now belongs to the work partition too
  const t2 = T.openTab("https://w.example/three");
  R.secondTabPartition = t2.view.webContents.session.partition;
  T.closeTab(t2.id);
  T.flushBrowserStores();
  R.workFiles = files(path.join(B(), "profiles", "work"));
  R.modes = { profilesDir: mode(path.join(B(), "profiles")), workDir: mode(path.join(B(), "profiles", "work")), pw: mode(path.join(B(), "profiles", "work", "passwords.json")), perm: mode(path.join(B(), "profiles", "work", "permissions.json")) };
  R.workHistory = JSON.parse(readRaw("profiles/work/history.json")).map((e) => e.url);
  R.defaultHistoryStill = JSON.parse(readRaw("history.json")).map((e) => e.url);
  const rawAll = [readRaw("passwords.json"), readRaw("profiles/work/passwords.json")].join("|");
  R.plaintextOnDisk = [PW1, PW2, "alice", "carol"].map((t) => rawAll.includes(t));
  R.registryFile = { json: readJson("profiles.json"), mode: mode(path.join(B(), "profiles.json")) };

  // the other profile's own data does not leak across
  const back = await handlers["profile:switch"](consoleEvt(), "default");
  const wc3 = T.tabs.get(T.active()).view.webContents;
  R.back = { ok: back.ok, partition: wc3.session.partition, history: (await call("GET", "/history")).body.history.map((e) => e.url), bookmarks: (await call("GET", "/bookmarks")).body.bookmarks.map((b) => b.url),
    passwords: T.vault.list().map((e) => e.username), sites: Object.keys(T.siteTable()) };
  R.fillOffersOnlyThisProfile = (() => { wc3.loadURL("https://w.example/two"); return T.vault.entriesFor("https://w.example").length; })();

  // removing: never the default, never the one in use, native confirmation first
  R.removeDefault = await handlers["profile:remove"](consoleEvt(), "default");
  await handlers["profile:switch"](consoleEvt(), "work");
  R.removeActive = await handlers["profile:remove"](consoleEvt(), "work");
  await handlers["profile:switch"](consoleEvt(), "default");
  calls.dialogs.length = 0;
  dialogAnswer = 1; // Cancel
  R.removeCancelled = await handlers["profile:remove"](consoleEvt(), "work");
  R.cancelledDialog = calls.dialogs.map((d) => ({ message: d.message, detail: d.detail, buttons: d.buttons }));
  R.stillThere = { dir: fsx.existsSync(path.join(B(), "profiles", "work")), registry: T.profileRegistry().names, cleared: sessions["persist:dourmouse-browser-work"].cleared };
  dialogAnswer = 0;
  R.removed = await handlers["profile:remove"](consoleEvt(), "work");
  R.afterRemove = { dir: fsx.existsSync(path.join(B(), "profiles", "work")), registry: T.profileRegistry().names, cleared: sessions["persist:dourmouse-browser-work"].cleared, cache: sessions["persist:dourmouse-browser-work"].cacheCleared,
    defaultStill: JSON.parse(readRaw("history.json")).length > 0, pwStill: JSON.parse(readRaw("passwords.json")).entries.length };

  // the bridge can read the list and change nothing
  const bridge = {};
  bridge.list = (await call("GET", "/profiles")).body;
  for (const r of ["/profiles", "/profiles/switch", "/profiles/create", "/profiles/remove", "/profile", "/profile/switch", "/import", "/import/chrome", "/import/passwords"]) {
    bridge["POST " + r] = (await call("POST", r, { name: "work", id: "x" })).status;
  }
  bridge.activeStill = T.activeProfile();
  R.bridge = bridge;
});
const wait_ = (ms) => new Promise((r) => setTimeout(r, ms));
''' % (PW1, PW2)


@pytest.fixture(scope="module")
def prof(tmp_path_factory):
    return run_scenario(tmp_path_factory.mktemp("profiles"), PROFILES)


class TestTheDefaultProfileKeepsTodaysPaths:
    def test_the_default_profile_uses_the_old_partition_and_the_old_files(self, prof):
        assert prof["startProfile"] == "default"
        assert prof["defaultPartition"] == ["persist:dourmouse-browser", "persist:dourmouse-browser"]
        for f in ("history.json", "bookmarks.json", "permissions.json", "passwords.json"):
            assert f in prof["defaultFiles"], f
        assert prof["defaultHistory"] == ["https://a.example/one"]

    def test_the_folder_and_the_permissions_file_are_tightened_on_every_write(self, prof):
        # the folder and permissions.json existed with 0755 and 0644 (the B2 state); a write fixes both
        assert prof["loosePermsFixed"] == {"dir": 0o700, "permissions": 0o600, "passwords": 0o600}


class TestOnlyTheConsoleManagesProfiles:
    def test_a_page_a_stranger_and_a_console_subframe_are_all_refused(self, prof):
        for who, answer in prof["refusedFrom"].items():
            assert answer == {"ok": False, "error": "only the console may do that"}, who
        assert prof["noEvilProfile"] is False


class TestCreatingAndSwitching:
    def test_names_are_validated_and_a_created_profile_is_not_switched_to(self, prof):
        assert not any(prof["badNames"])
        assert prof["created"] == {"ok": True, "name": "work"}  # lower-cased
        assert prof["dupe"]["ok"] is False and "exists" in prof["dupe"]["error"]
        assert [(p["name"], p["active"]) for p in prof["listed"]] == [("default", True), ("work", False)]
        assert prof["createDidNotSwitch"] == "default"

    def test_a_switch_closes_the_tabs_and_opens_a_blank_tab_in_the_new_partition(self, prof):
        assert prof["switch"] == {"ok": True, "active": "work"}
        assert prof["switchUnknown"]["ok"] is False
        assert prof["switchSame"] == {"ok": True, "active": "work", "unchanged": True}
        assert prof["oldTabClosed"] is True and prof["tabsAfter"] == 1
        nt = prof["newTab"]
        # the first tab of a profile is created at about:blank, which is how the browser agent finds the pane
        assert nt == {"partition": "persist:dourmouse-browser-work", "prefs": "persist:dourmouse-browser-work", "firstLoad": "about:blank", "different": True}
        assert prof["secondTabPartition"] == "persist:dourmouse-browser-work"

    def test_what_was_waiting_for_an_answer_in_the_old_profile_is_refused_and_forgotten(self, prof):
        assert prof["promptWaiting"] == 1 and prof["saveWaiting"] is True
        assert prof["promptAfter"] == {"size": 0, "answer": False, "pending": 0, "save": None}

    def test_the_new_profile_starts_empty_and_says_which_it_is(self, prof):
        assert prof["workEmpty"] == {"history": 0, "bookmarks": 0, "passwords": 0, "sites": 0, "paneProfile": "work", "status": "work"}


class TestEachProfileKeepsItsOwnRecords:
    def test_work_files_are_in_the_profile_folder_and_default_files_did_not_move(self, prof):
        for f in ("history.json", "bookmarks.json", "permissions.json", "passwords.json"):
            assert f in prof["workFiles"], f
        assert prof["workHistory"] == ["https://w.example/three", "https://w.example/two"]
        assert prof["defaultHistoryStill"] == ["https://a.example/one"]

    def test_files_and_folders_are_owner_only_and_nothing_is_plaintext(self, prof):
        assert prof["modes"] == {"profilesDir": 0o700, "workDir": 0o700, "pw": 0o600, "perm": 0o600}
        assert prof["plaintextOnDisk"] == [False, False, False, False]

    def test_switching_back_shows_the_first_profile_again_and_never_the_other(self, prof):
        b = prof["back"]
        assert b["ok"] is True and b["partition"] == "persist:dourmouse-browser"
        assert b["history"] == ["https://a.example/one"] and b["bookmarks"] == ["https://a.example/"]
        assert b["passwords"] == ["alice"] and b["sites"] == ["https://a.example"]
        assert prof["fillOffersOnlyThisProfile"] == 0  # work's login for w.example is not offered here

    def test_the_active_profile_is_remembered_in_a_small_owner_only_file(self, prof):
        assert prof["registryFile"]["json"] == {"active": "work", "names": ["work"]}
        assert prof["registryFile"]["mode"] == 0o600


class TestRemovingAProfile:
    def test_the_default_and_the_one_in_use_cannot_be_removed(self, prof):
        assert "default" in prof["removeDefault"]["error"]
        assert "Switch to another" in prof["removeActive"]["error"]

    def test_a_native_confirmation_with_counts_comes_first_and_cancel_deletes_nothing(self, prof):
        assert prof["removeCancelled"]["cancelled"] is True
        (d,) = prof["cancelledDialog"]
        assert d["buttons"] == ["Remove profile", "Cancel"] and 'profile "work"' in d["message"]
        assert "passwords (1)" in d["detail"] and "bookmarks (1)" in d["detail"] and "history (2)" in d["detail"]
        assert prof["stillThere"] == {"dir": True, "registry": ["work"], "cleared": 0}

    def test_confirming_deletes_the_folder_clears_its_cookie_jar_and_touches_nothing_else(self, prof):
        assert prof["removed"] == {"ok": True}
        a = prof["afterRemove"]
        assert a["dir"] is False and a["registry"] == [] and a["cleared"] == 1 and a["cache"] == 1
        assert a["defaultStill"] is True and a["pwStill"] == 1


class TestTheBridgeCanOnlyRead:
    def test_get_lists_names_and_every_change_route_is_missing(self, prof):
        b = prof["bridge"]
        assert b["list"]["ok"] is True and [p["name"] for p in b["list"]["profiles"]] == ["default"]
        assert b["list"]["active"] == "default"
        for route, status in b.items():
            if route.startswith("POST "):
                assert status == 404, route
        assert b["activeStill"] == "default"


RESTART = r'''
main(async () => {
  R.afterRestart = { profile: T.activeProfile(), partition: T.tabPrefs().partition };
  T.ensurePaneView();
  T.showPane();
  const wc = T.tabs.get(T.active()).view.webContents;
  R.firstTab = { partition: wc.session.partition, load: wc.loads[0] };
  R.history = (await call("GET", "/history")).body.history.map((e) => e.url);
  R.registry = T.profileRegistry();
});
'''

FIRST_RUN = r'''
main(async () => {
  T.ensurePaneView();
  T.showPane();
  const wc = T.tabs.get(T.active()).view.webContents;
  await handlers["profile:create"](consoleEvt(), "work");
  await handlers["profile:switch"](consoleEvt(), "work");
  T.tabs.get(T.active()).view.webContents.loadURL("https://w.example/kept");
});
'''


def test_the_app_reopens_in_the_profile_it_was_last_in(tmp_path):
    data = tmp_path / "shared"
    data.mkdir()
    run_scenario(tmp_path, FIRST_RUN, data=data)
    out = run_scenario(tmp_path, RESTART, data=data)
    assert out["afterRestart"] == {"profile": "work", "partition": "persist:dourmouse-browser-work"}
    assert out["firstTab"] == {"partition": "persist:dourmouse-browser-work", "load": "about:blank"}
    assert out["history"] == ["https://w.example/kept"]
    assert out["registry"] == {"active": "work", "names": ["work"]}


class TestTheWiring:
    def test_every_profile_ipc_handler_starts_with_the_console_check(self):
        for name in ("profile:list", "profile:switch", "profile:create", "profile:remove"):
            m = re.search(r'ipcMain\.handle\("' + re.escape(name) + r'", (.*?)\n(?:ipcMain|\n)', MAIN_CODE, re.S)
            assert m and "consoleOnly(evt)" in m.group(1), name

    def test_the_removal_of_a_profile_goes_through_the_one_native_dialog_guard(self):
        assert 'ipcMain.handle("profile:remove"' in MAIN_CODE
        body = MAIN_CODE.split('ipcMain.handle("profile:remove"')[1].split("});")[0]
        assert "nativeGuard(" in body
        assert "confirmNatively(" in MAIN_CODE.split("async function removeProfileFlow")[1].split("\n}\n")[0]

    def test_the_pane_web_preferences_still_carry_no_preload_and_no_node(self):
        prefs = re.search(r"const TAB_WEB_PREFERENCES = (\{[^}]*\});", MAIN_CODE).group(1)
        for forbidden in ("preload", "nodeIntegration", "sandbox: false", "webSecurity: false", "contextIsolation: false"):
            assert forbidden not in prefs

    def test_no_em_dash_was_added(self):
        assert "\u2014" not in (ELECTRON / "profiles.js").read_text(encoding="utf-8")
