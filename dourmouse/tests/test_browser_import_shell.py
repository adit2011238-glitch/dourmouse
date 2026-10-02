"""Phase B3: import from Chrome in the Electron shell, driven for real.

electron/main.js is loaded under plain node with a fake ``electron`` (see b3_harness.py): its OWN
import flows, native dialogs, stores and vault run. The "Chrome profile" is a FAKE folder in a
temp dir and the "export" is a FAKE CSV with invented passwords; the real Chrome profile, its
Keychain item and every real password are never touched. What this cannot prove is what the real
macOS pickers and Keychain do; that was checked live in an isolated copy of the app with the same
fake files and is recorded in the B3 finding.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from dourmouse.tests.b3_harness import ELECTRON, NODE, run_scenario
from dourmouse.tests.test_browser_import_policy import make_chrome_profile

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

MAIN_CODE = (ELECTRON / "main.js").read_text(encoding="utf-8")

PW_SHOP = "Shop-Secret-Zq7!"
PW_MULTI = "Multi-Secret-Lm4#"
PW_DEV = "Dev-Secret-Rt9$"
PW_CHANGED = "Shop-Changed-Vb2%"

CSV = (
    "name,url,username,password,note\r\n"
    f'shop.example,https://shop.example/login,alice@example.test,"{PW_SHOP}",\r\n'
    f'multi,https://multi.example/,bob,"{PW_MULTI}","a note, with comma"\r\n'
    "plain,http://insecure.example/,carol,PlainPw1-Hh8,\r\n"
    f"dev,http://localhost:3000/,dave,{PW_DEV},\r\n"
    "android,android://abc@com.example.app/,erin,AndroidPw-Kk3,\r\n"
    "empty,https://empty.example/,frank,,\r\n"
)

SCENARIO = r'''
const fsx = require("fs");
const crypto = require("crypto");
const CHROME = process.env.T_CHROME, CSVFILE = process.env.T_CSV, CSV2 = process.env.T_CSV2;
const sha = (p) => crypto.createHash("sha256").update(fsx.readFileSync(p)).digest("hex");
const tree = (d) => fsx.readdirSync(d).sort().map((f) => f + ":" + sha(path.join(d, f)));
const tmpImport = () => fsx.readdirSync(os.tmpdir()).filter((f) => f.startsWith("dm-import-")).length;
const walk = (d, out = []) => { for (const e of fsx.readdirSync(d, { withFileTypes: true })) { const p = path.join(d, e.name); if (e.isDirectory()) walk(p, out); else out.push(p); } return out; };
const SECRETS = ["%s", "%s", "%s", "%s", "PlainPw1-Hh8", "AndroidPw-Kk3", "alice@example.test", "bob", "dave", "carol"];
const wait_ = (ms) => new Promise((r) => setTimeout(r, ms));
main(async () => {
  T.ensurePaneView();
  T.showPane();
  await handlers["pane:screen"](consoleEvt(), true);
  const wc = T.tabs.get(T.active()).view.webContents;
  const chromeBefore = tree(CHROME);
  const csvBefore = sha(CSVFILE);
  const tmpBefore = tmpImport();

  // 1. only the console's top page; nothing opens for anybody else
  R.refused = [];
  for (const name of ["import:chrome", "import:passwords"]) for (const evt of [pageEvt(wc), strangerEvt(), consoleSubframeEvt()]) R.refused.push(await handlers[name](evt, {}));
  R.refusedOpened = [calls.dialogs.length, calls.opens.length];

  // 2. bookmarks and history: a cancelled picker, then a cancelled confirmation, change nothing
  R.cancelPick = await handlers["import:chrome"](consoleEvt(), {});
  openPaths.push(CHROME); dialogAnswer = 1;
  R.cancelConfirm = await handlers["import:chrome"](consoleEvt(), {});
  R.chromeDialog = calls.dialogs.map((d) => ({ title: d.title, message: d.message, detail: d.detail, buttons: d.buttons, defaultId: d.defaultId }));
  R.pickerOptions = calls.opens.map((o) => ({ title: o.title, properties: o.properties, defaultPath: o.defaultPath }));
  R.afterCancel = { bookmarks: (await call("GET", "/bookmarks")).body.bookmarks.length, history: (await call("GET", "/history")).body.history.length };
  R.nothingChoosing = await handlers["import:chrome"](consoleEvt(), { bookmarks: false, history: false });

  // 3. confirmed: merged into the ACTIVE profile only
  calls.dialogs.length = 0; openPaths.push(CHROME); dialogAnswer = 0;
  R.chromeImport = await handlers["import:chrome"](consoleEvt(), {});
  T.flushBrowserStores();
  const b = (await call("GET", "/bookmarks")).body.bookmarks;
  const h = (await call("GET", "/history")).body.history;
  R.bookmarks = b.map((x) => [x.url, x.title]);
  R.history = h.map((x) => [x.url, x.title]);
  R.historyTimes = h.map((x) => x.at);
  R.files = { bookmarks: fsx.existsSync(path.join(USERDATA, "browser", "bookmarks.json")), history: fsx.existsSync(path.join(USERDATA, "browser", "history.json")) };
  R.chromeUntouched = JSON.stringify(tree(CHROME)) === JSON.stringify(chromeBefore);
  R.tempLeft = tmpImport() - tmpBefore;

  // again: idempotent
  openPaths.push(CHROME);
  R.again = await handlers["import:chrome"](consoleEvt(), {});

  // only bookmarks asked for into a named profile: lands in that profile, not in default
  await handlers["profile:create"](consoleEvt(), "work");
  await handlers["profile:switch"](consoleEvt(), "work");
  openPaths.push(CHROME);
  R.workImport = await handlers["import:chrome"](consoleEvt(), { bookmarks: true, history: false });
  T.flushBrowserStores();
  R.workHas = { bookmarks: (await call("GET", "/bookmarks")).body.bookmarks.length, history: (await call("GET", "/history")).body.history.length };
  R.workFile = { bookmarks: fsx.existsSync(path.join(USERDATA, "browser", "profiles", "work", "bookmarks.json")), history: fsx.existsSync(path.join(USERDATA, "browser", "profiles", "work", "history.json")) };
  await handlers["profile:switch"](consoleEvt(), "default");
  R.defaultStill = { bookmarks: (await call("GET", "/bookmarks")).body.bookmarks.length, history: (await call("GET", "/history")).body.history.length };

  // 4. a folder that is not a Chrome profile is refused before any confirmation
  const empty = path.join(DATA, "empty"); fsx.mkdirSync(empty);
  calls.dialogs.length = 0; openPaths.push(empty);
  R.emptyFolder = await handlers["import:chrome"](consoleEvt(), {});
  R.emptyFolderDialogs = calls.dialogs.length;

  // 5. passwords: with no encryption nothing is even offered
  safe.available = false; T.resetCipher();
  openPaths.push(CSVFILE); const opensBefore = calls.opens.length;
  R.noCipher = await handlers["import:passwords"](consoleEvt());
  R.noCipherOpened = calls.opens.length - opensBefore;
  safe.available = true; T.resetCipher();
  openPaths.length = 0;

  // a file that is not an export
  const junk = path.join(DATA, "junk.csv"); fsx.writeFileSync(junk, "a,b\n1,2\n");
  calls.dialogs.length = 0; openPaths.push(junk);
  R.junkCsv = await handlers["import:passwords"](consoleEvt());
  R.junkDialogs = calls.dialogs.length;

  // cancelled picker, cancelled confirmation: nothing stored
  R.pwCancelPick = await handlers["import:passwords"](consoleEvt());
  openPaths.push(CSVFILE); calls.dialogs.length = 0; dialogAnswer = 1;
  R.pwCancelConfirm = await handlers["import:passwords"](consoleEvt());
  R.pwDialog = calls.dialogs.map((d) => ({ title: d.title, message: d.message, detail: d.detail, buttons: d.buttons, defaultId: d.defaultId }));
  R.pwAfterCancel = T.vault.list().length;

  // confirmed
  openPaths.push(CSVFILE); dialogAnswer = 0;
  R.pwImport = await handlers["import:passwords"](consoleEvt());
  T.flushBrowserStores();
  R.vaultRows = T.vault.list().map((e) => [e.origin, e.username]);
  R.vaultCounts = T.vault.counts();
  const raw = readRaw("passwords.json");
  R.diskLeaks = SECRETS.filter((t) => raw.includes(t));
  R.diskHasSite = raw.includes("shop.example");
  R.diskMode = fsx.statSync(path.join(USERDATA, "browser", "passwords.json")).mode & 0o777;
  R.credentialRoundTrip = (() => { const e = T.vault.list().find((x) => x.origin === "https://shop.example"); const c = T.vault.credentials(e.id); return [c.username, c.password]; })();

  // a second run with one changed password: updated, the rest the same
  fsx.writeFileSync(CSV2, fsx.readFileSync(CSVFILE, "utf8").replace("%s", "%s"));
  openPaths.push(CSV2);
  R.pwAgain = await handlers["import:passwords"](consoleEvt());
  T.flushBrowserStores();
  R.afterUpdate = (() => { const e = T.vault.list().find((x) => x.origin === "https://shop.example"); return T.vault.credentials(e.id).password === "%s"; })();

  // nothing was copied, logged or sent anywhere: the CSV, the userData folder and the console channel
  R.csvUntouched = sha(CSVFILE) === csvBefore;
  const everything = walk(USERDATA).map((p) => fsx.readFileSync(p, "utf8")).join("\n");
  R.plaintextAnywhereInUserData = SECRETS.filter((t) => t.length > 6 && everything.includes(t));
  R.csvCopiedInto = walk(USERDATA).filter((p) => p.endsWith(".csv"));
  R.sentToConsole = SECRETS.filter((t) => JSON.stringify(calls.sent).includes(t));
  R.resultsLeak = SECRETS.filter((t) => JSON.stringify([R.pwImport, R.pwAgain, R.chromeImport, R.pwDialog, R.chromeDialog]).includes(t));
  R.chromeCanaries = "never-read";
  R.statusLeak = SECRETS.filter((t) => JSON.stringify([(await_status = null)]).includes(t));
});
''' % (PW_SHOP, PW_MULTI, PW_DEV, PW_CHANGED, PW_SHOP, PW_CHANGED, PW_CHANGED)


@pytest.fixture(scope="module")
def imp(tmp_path_factory):
    base = tmp_path_factory.mktemp("import")
    chrome = make_chrome_profile(base / "chrome")
    csv = base / "Chrome Passwords.csv"
    csv.write_text(CSV, encoding="utf-8")
    csv2 = base / "second.csv"
    out = run_scenario(base, SCENARIO.replace("R.statusLeak = SECRETS.filter((t) => JSON.stringify([(await_status = null)]).includes(t));", ""),
                       extra_env={"T_CHROME": str(chrome), "T_CSV": str(csv), "T_CSV2": str(csv2)})
    out["__chrome_hash_after"] = hashlib.sha256((chrome / "Login Data").read_bytes()).hexdigest()
    return out


class TestOnlyTheConsoleCanImport:
    def test_a_page_a_stranger_and_a_console_subframe_are_refused_and_nothing_opens(self, imp):
        assert all(r == {"ok": False, "error": "only the console may do that"} for r in imp["refused"]) and len(imp["refused"]) == 6
        assert imp["refusedOpened"] == [0, 0]


class TestBookmarksAndHistory:
    def test_a_cancelled_picker_and_a_cancelled_confirmation_change_nothing(self, imp):
        assert imp["cancelPick"]["cancelled"] is True and imp["cancelConfirm"]["cancelled"] is True
        assert imp["afterCancel"] == {"bookmarks": 0, "history": 0}
        assert "Choose bookmarks, history or both" in imp["nothingChoosing"]["error"]

    def test_the_native_confirmation_gives_counts_and_says_chrome_is_not_changed(self, imp):
        (d,) = imp["chromeDialog"]
        assert d["buttons"] == ["Import", "Cancel"] and d["defaultId"] == 1 and 'profile "default"' in d["message"]
        assert "bookmarks found" in d["detail"] or "bookmark found" in d["detail"]
        assert "history entries found" in d["detail"] and "never changed" in d["detail"] and "Passwords, cookies" in d["detail"]
        (p,) = imp["pickerOptions"][:1]
        assert p["properties"] == ["openDirectory"] and p["defaultPath"].endswith("Google/Chrome")

    def test_a_confirmed_import_adds_web_pages_only_with_their_real_times(self, imp):
        r = imp["chromeImport"]
        assert r["ok"] is True and r["profile"] == "default"
        assert r["bookmarks"] == {"found": 5, "added": 2, "existing": 1, "skipped": 2, "full": 0}
        assert r["history"] == {"found": 5, "added": 3, "existing": 0, "skipped": 2, "dropped": 0}
        assert imp["bookmarks"] == [["https://example.com/a", "Example"], ["https://deep.example/x", "Deep"]]
        assert [u for u, _ in imp["history"]] == ["https://news.example/story", "https://example.com/a", "https://old.example/"]
        assert imp["historyTimes"] == [1_700_000_500_000, 1_700_000_100_000, 1_600_000_000_000]
        assert imp["files"] == {"bookmarks": True, "history": True}

    def test_chrome_was_read_never_changed_and_no_temp_copy_was_left(self, imp):
        assert imp["chromeUntouched"] is True and imp["tempLeft"] == 0

    def test_importing_again_adds_nothing(self, imp):
        a = imp["again"]
        assert a["bookmarks"]["added"] == 0 and a["history"]["added"] == 0 and a["bookmarks"]["existing"] == 3 and a["history"]["existing"] == 3

    def test_an_import_goes_into_the_profile_in_use_and_no_other(self, imp):
        assert imp["workImport"]["profile"] == "work" and imp["workImport"]["history"] is None
        assert imp["workHas"] == {"bookmarks": 2, "history": 0}
        assert imp["workFile"] == {"bookmarks": True, "history": False}
        assert imp["defaultStill"] == {"bookmarks": 2, "history": 3}

    def test_a_folder_that_holds_neither_file_is_refused_before_any_confirmation(self, imp):
        assert "No Bookmarks" in imp["emptyFolder"]["error"] and imp["emptyFolderDialogs"] == 0


class TestPasswordsFromACsv:
    def test_with_no_encryption_nothing_is_offered_and_no_picker_opens(self, imp):
        assert "secure storage is not available" in imp["noCipher"]["error"] and imp["noCipherOpened"] == 0

    def test_a_file_that_is_not_a_password_export_is_refused_before_any_confirmation(self, imp):
        assert "does not look like a Chrome password export" in imp["junkCsv"]["error"] and imp["junkDialogs"] == 0

    def test_a_cancelled_picker_or_confirmation_stores_nothing(self, imp):
        assert imp["pwCancelPick"]["cancelled"] is True and imp["pwCancelConfirm"]["cancelled"] is True and imp["pwAfterCancel"] == 0

    def test_the_native_confirmation_names_the_file_and_the_count_but_no_secret(self, imp):
        (d,) = imp["pwDialog"]
        assert d["buttons"] == ["Import passwords", "Cancel"] and d["defaultId"] == 1
        assert "3 passwords from Chrome Passwords.csv" in d["message"] and 'profile "default"' in d["message"]
        assert "3 rows cannot be saved" in d["detail"] and "not copied or changed" in d["detail"] and "delete it" in d["detail"]

    def test_a_confirmed_import_saves_into_the_encrypted_vault_and_returns_counts_only(self, imp):
        r = imp["pwImport"]
        assert r["ok"] is True and r["profile"] == "default"
        assert {k: r[k] for k in ("rows", "usable", "skipped", "added", "updated", "same", "refused", "never", "full", "unavailable")} == {
            "rows": 6, "usable": 3, "skipped": 3, "added": 3, "updated": 0, "same": 0, "refused": 0, "never": 0, "full": 0, "unavailable": False}
        assert sorted(imp["vaultRows"]) == [["http://localhost:3000", "dave"], ["https://multi.example", "bob"], ["https://shop.example", "alice@example.test"]]
        assert imp["vaultCounts"]["passwords"] == 3

    def test_on_disk_it_is_ciphertext_owner_only_and_round_trips(self, imp):
        assert imp["diskLeaks"] == [] and imp["diskHasSite"] is True and imp["diskMode"] == 0o600
        assert imp["credentialRoundTrip"] == ["alice@example.test", PW_SHOP]

    def test_a_second_run_updates_a_changed_password_and_leaves_the_rest(self, imp):
        r = imp["pwAgain"]
        assert r["ok"] is True and (r["added"], r["updated"], r["same"]) == (0, 1, 2)
        assert imp["afterUpdate"] is True


class TestNothingWasCopiedLoggedOrSent:
    def test_the_csv_was_not_copied_or_changed_and_no_plaintext_is_anywhere_in_the_app_folder(self, imp):
        assert imp["csvUntouched"] is True and imp["csvCopiedInto"] == []
        assert imp["plaintextAnywhereInUserData"] == []

    def test_no_secret_reached_the_console_channel_or_any_result_or_dialog(self, imp):
        assert imp["sentToConsole"] == [] and imp["resultsLeak"] == []


class TestTheWiring:
    def test_both_import_handlers_start_with_the_console_check_and_the_dialog_guard(self):
        for name in ("import:chrome", "import:passwords"):
            m = re.search(r'ipcMain\.handle\("' + re.escape(name) + r'", (.*?)\n(?:ipcMain|\n)', MAIN_CODE, re.S)
            assert m and "consoleOnly(evt)" in m.group(1) and "nativeGuard(" in m.group(1), name

    def test_every_import_goes_native_picker_then_native_confirmation_then_write(self):
        chrome = MAIN_CODE.split("async function importChromeFlow")[1].split("\nasync function importPasswordsFlow")[0]
        assert chrome.index("pickNatively(") < chrome.index("readChromeProfile(") < chrome.index("confirmNatively(") < chrome.index("bookmarkStore.set(")
        pw = MAIN_CODE.split("async function importPasswordsFlow")[1].split("\nipcMain.handle")[0]
        assert pw.index("pickNatively(") < pw.index("passwordRowsFromCsv(") < pw.index("confirmNatively(") < pw.index("importPasswords(vault")

    def test_the_pane_bridge_has_no_import_route(self):
        bridge = MAIN_CODE.split("function startPaneBridge()")[1].split("// Native notifications")[0]
        for needle in ("importChromeFlow", "importPasswordsFlow", "impLib.", "pickNatively"):
            assert needle not in bridge, needle

    def test_main_js_never_names_chromes_secret_stores_or_its_keychain_item(self):
        code = "\n".join(line for line in MAIN_CODE.splitlines() if not line.lstrip().startswith("//"))
        for banned in ("Login Data", "Safe Storage", "Chrome Safe Storage", "security find-generic-password"):
            assert banned not in code, banned

    def test_the_picker_only_offers_a_folder_or_a_csv_and_the_page_supplies_no_path(self):
        pre = (ELECTRON / "preload.js").read_text(encoding="utf-8")
        block = pre[pre.index("importFrom: {"):pre.index("importFrom: {") + 300]
        assert "ipcRenderer.invoke(\"import:chrome\", want)" in block and "ipcRenderer.invoke(\"import:passwords\")" in block
        assert "path" not in block.lower().replace("passwords", "")
