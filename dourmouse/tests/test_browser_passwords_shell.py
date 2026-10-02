"""Phase B2: the Electron shell's saved passwords and address autofill, driven for real.

electron/main.js is loaded under plain node with a fake ``electron`` (see b2_harness.py): its
OWN IPC handlers, its OWN message handlers for the form helper, its OWN fill, reveal and
bridge code run. The cipher is a visible stand-in for safeStorage, so a plaintext leak into a
file or a message is caught. What this cannot prove is what the real Keychain and a real page
do; that was checked live in an isolated copy of the app and is recorded in the B2 finding.
"""

from __future__ import annotations

import re
import stat

import pytest

from dourmouse.tests.b2_harness import ELECTRON, NODE, run_scenario

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

MAIN_CODE = (ELECTRON / "main.js").read_text(encoding="utf-8")
HELPER = (ELECTRON / "content" / "frame-forms.js").read_text(encoding="utf-8")

SECRET = "Hunter2-Zq7!pw"
SECRET2 = "Another-Pw-9x#"

LOGIN = r'''
const SECRET = "%s", SECRET2 = "%s";
main(async () => {
  T.ensurePaneView();
  T.showPane();
  const tab = T.tabs.get(T.active());
  const wc = tab.view.webContents;
  wc.loadURL("https://login.example/signin");
  await handlers["pane:screen"](consoleEvt(), true);
  const sentToConsole = () => JSON.stringify(calls.sent.filter((s) => s.wc === win.webContents.id));
  const submit = (evt, msg) => listeners["dm:pw-submit"](evt, msg);

  // the helper reports a login form, with no value in the report
  listeners["dm:forms"](pageEvt(wc), { pw: true, user: true, addr: false });
  R.noEntriesNoFill = T.privacyState().fill;

  // a submission from something that is not the top page of a pane tab is ignored
  submit(frameEvt(wc), { username: "alice", password: SECRET });
  submit(strangerEvt(), { username: "alice", password: SECRET });
  submit({ sender: win.webContents, senderFrame: { parent: null, url: win.webContents.url } }, { username: "alice", password: SECRET });
  submit(pageEvt(wc, "https://evil.example/"), { username: "alice", password: SECRET }); // the frame is not where the tab is
  submit(pageEvt(wc), { username: 5, password: SECRET });
  submit(pageEvt(wc), { username: "alice", password: "" });
  submit(pageEvt(wc), { username: "alice", password: "x".repeat(2000) });
  submit(pageEvt(wc), null);
  R.ignored = T.privacyState().save;

  // a real login: the owner is offered Save, and the password stays in the main process
  submit(pageEvt(wc), { username: "alice", password: SECRET });
  const offered = T.privacyState().save;
  R.offered = offered;
  R.consoleNeverSawIt = !sentToConsole().includes(SECRET) && !JSON.stringify(T.privacyState()).includes(SECRET);

  // only the console answers
  R.answerFromPage = await handlers["pw:save-answer"](pageEvt(wc), offered.id, "save");
  R.answerFromStranger = await handlers["pw:save-answer"](strangerEvt(), offered.id, "save");
  R.answerBad = await handlers["pw:save-answer"](consoleEvt(), offered.id, "save-and-show");
  R.stillOffered = Boolean(T.privacyState().save);
  R.saved = await handlers["pw:save-answer"](consoleEvt(), offered.id, "save");
  R.afterSave = T.privacyState().save;
  T.flushBrowserStores();
  const raw = readRaw("passwords.json");
  R.disk = { leaksPassword: raw.includes(SECRET), leaksUser: raw.includes("alice"), hasSite: raw.includes("login.example"), mode: require("fs").statSync(path.join(USERDATA, "browser", "passwords.json")).mode & 0o777 };
  R.entryCount = readJson("passwords.json").entries.length;

  // the same login again says nothing, a new password is an update, a refusal is remembered
  submit(pageEvt(wc), { username: "alice", password: SECRET });
  R.sameAgain = T.privacyState().save;
  wc.loadURL("https://login.example/signin");
  submit(pageEvt(wc), { username: "alice", password: SECRET2 });
  R.update = T.privacyState().save;
  const upId = T.privacyState().save.id;
  R.notNow = await handlers["pw:save-answer"](consoleEvt(), upId, "dismiss");
  R.afterNotNow = T.privacyState().save;
  R.stillTheOldOne = T.vault.credentials(T.vault.list()[0].id).password === SECRET;
  submit(pageEvt(wc), { username: "alice", password: SECRET2 });
  await handlers["pw:save-answer"](consoleEvt(), T.privacyState().save.id, "save");
  R.updated = T.vault.credentials(T.vault.list()[0].id).password === SECRET2 && T.vault.list().length === 1;

  // Fill: the bar offers usernames only, and the console fills it
  wc.loadURL("https://login.example/signin");
  listeners["dm:forms"](pageEvt(wc), { pw: true, user: true, addr: false });
  const entry = T.vault.list()[0];
  R.fillOffer = T.privacyState().fill;
  R.fillOfferHasNoPassword = !JSON.stringify(T.privacyState()).includes(SECRET2);
  wc.sentToThis.length = 0;
  R.fillFromPage = await handlers["pw:fill"](pageEvt(wc), entry.id);
  R.fillFromStranger = await handlers["pw:fill"](strangerEvt(), entry.id);
  R.nothingSentYet = wc.sentToThis.filter((m) => m.channel === "dm:fill-login").length;
  R.fill = await handlers["pw:fill"](consoleEvt(), entry.id);
  R.sentFill = wc.sentToThis.filter((m) => m.channel === "dm:fill-login").map((m) => m.payload);
  R.fillWentOnlyToThatTab = calls.sent.filter((m) => m.channel === "dm:fill-login").every((m) => m.wc === wc.id);
  R.fillNeverTouchedTheConsole = !sentToConsole().includes(SECRET2);
  R.fillUsedStamp = T.vault.list()[0].lastUsed > 0;

  // it never fills the wrong place
  wc.loadURL("https://evil.example/signin");
  wc.sentToThis.length = 0;
  R.otherSite = await handlers["pw:fill"](consoleEvt(), entry.id);
  wc.loadURL("http://login.example/signin");
  R.otherScheme = await handlers["pw:fill"](consoleEvt(), entry.id);
  wc.loadURL("https://login.example:8443/signin");
  R.otherPort = await handlers["pw:fill"](consoleEvt(), entry.id);
  wc.loadURL("https://login.example.evil.test/");
  R.lookalike = await handlers["pw:fill"](consoleEvt(), entry.id);
  wc.loadURL("about:blank");
  R.blank = await handlers["pw:fill"](consoleEvt(), entry.id);
  R.nothingSentToWrongPlaces = wc.sentToThis.filter((m) => m.channel === "dm:fill-login").length;
  R.fillOfferGoneElsewhere = T.privacyState().fill;
  R.missingEntry = await handlers["pw:fill"](consoleEvt(), "no-such-id");

  // a tab that is not in front is not filled
  wc.loadURL("https://login.example/signin");
  const back = T.openTab("https://news.example/");
  wc.sentToThis.length = 0;
  R.notInFront = await handlers["pw:fill"](consoleEvt(), entry.id);
  T.activateTab(tab.id);
  T.closeTab(back.id);

  // a click on a field opens a native menu at the pointer; nothing is filled until an item is chosen
  listeners["dm:forms"](pageEvt(wc), { pw: true, user: true, addr: false });
  wc.sentToThis.length = 0;
  listeners["dm:field-click"](pageEvt(wc), { kind: "password" });
  R.menu = calls.menus.map((m) => ({ labels: m.items.map((i) => i.label || i.type), inWindow: m.window }));
  R.filledByMenuOpening = wc.sentToThis.filter((m) => m.channel === "dm:fill-login").length;
  calls.menus[0].items.find((i) => i.label === "alice").click();
  R.filledByMenuItem = wc.sentToThis.filter((m) => m.channel === "dm:fill-login").length;
  const before = calls.menus.length;
  listeners["dm:field-click"](frameEvt(wc), { kind: "password" });
  listeners["dm:field-click"](strangerEvt(), { kind: "password" });
  listeners["dm:field-click"](pageEvt(wc), { kind: "nonsense" });
  const bg = T.openTab("https://login.example/other");
  T.activateTab(tab.id);
  listeners["dm:field-click"](pageEvt(bg.view.webContents), { kind: "password" });
  R.noMenuFromOthers = calls.menus.length === before;
  T.closeTab(bg.id);

  // Show: a console confirmation, then a native dialog only a person can press
  const dlg0 = calls.dialogs.length;
  R.revealFromPage = await handlers["pw:reveal"](pageEvt(wc), entry.id);
  R.dialogsAfterPageTried = calls.dialogs.length - dlg0;
  dialogAnswer = 1;
  R.revealCancelled = await handlers["pw:reveal"](consoleEvt(), entry.id);
  dialogAnswer = 0;
  R.reveal = await handlers["pw:reveal"](consoleEvt(), entry.id);
  R.dialog = { count: calls.dialogs.length - dlg0, message: calls.dialogs[dlg0].message, buttons: calls.dialogs[dlg0].buttons };
  R.revealMissing = await handlers["pw:reveal"](consoleEvt(), "no-such-id");

  // the list the console sees has no password, anywhere
  const listed = await handlers["pw:list"](consoleEvt());
  R.listShape = { ok: listed.ok, available: listed.available, rows: listed.entries.map((e) => [e.origin, e.username, e.readable]), keys: Object.keys(listed.entries[0]).sort() };
  R.listLeaks = JSON.stringify(listed).includes(SECRET) || JSON.stringify(listed).includes(SECRET2);
  R.listFromPage = await handlers["pw:list"](pageEvt(wc));

  // never for this site, and undoing it
  const shop = T.openTab("https://shop.example/login").view.webContents;
  submit(pageEvt(shop), { username: "bob", password: "Bob-Pw-1" });
  R.neverOffer = T.privacyState().save.host;
  R.never = await handlers["pw:save-answer"](consoleEvt(), T.privacyState().save.id, "never");
  submit(pageEvt(shop), { username: "bob", password: "Bob-Pw-2" });
  R.neverSilence = T.privacyState().save;
  R.neverList = (await handlers["pw:list"](consoleEvt())).never;
  R.neverRemoveFromPage = await handlers["pw:never-remove"](pageEvt(shop), "https://shop.example");
  R.neverRemove = await handlers["pw:never-remove"](consoleEvt(), "https://shop.example");
  submit(pageEvt(shop), { username: "bob", password: "Bob-Pw-2" });
  R.offeredAgain = Boolean(T.privacyState().save);
  await handlers["pw:save-answer"](consoleEvt(), T.privacyState().save.id, "dismiss");
  T.activateTab(tab.id);

  // plain http on a real host is never offered; this machine is
  wc.loadURL("http://plain.example/login");
  submit(pageEvt(wc), { username: "carol", password: "Carol-Pw-1" });
  R.plainHttp = T.privacyState().save;
  wc.loadURL("http://127.0.0.1:18851/login");
  submit(pageEvt(wc), { username: "dev", password: "Dev-Pw-1" });
  R.loopback = T.privacyState().save && T.privacyState().save.host;
  await handlers["pw:save-answer"](consoleEvt(), T.privacyState().save.id, "dismiss");

  // a flood of submissions from one tab is cut off after ten a minute
  const fl = T.openTab("https://flood.example/login");
  for (let i = 0; i < 14; i += 1) submit(pageEvt(fl.view.webContents), { username: "u" + i, password: "pw" + i });
  R.floodPrompts = T.pendingSaves.size;
  R.floodLastAccepted = T.pendingSaves.get(fl.id).username;
  T.closeTab(fl.id);
  T.activateTab(tab.id);

  // a prompt belongs to its tab and goes with it
  const t2 = T.openTab("https://two.example/");
  submit(pageEvt(t2.view.webContents), { username: "two", password: "Two-Pw-1" });
  R.pendingBeforeClose = T.pendingSaves.has(t2.id);
  T.closeTab(t2.id);
  R.pendingAfterClose = T.pendingSaves.has(t2.id);

  // delete
  R.deleteFromPage = await handlers["pw:delete"](pageEvt(wc), entry.id);
  R.delete = await handlers["pw:delete"](consoleEvt(), entry.id);
  R.afterDelete = T.vault.counts();

  // no secure storage: nothing is offered and nothing is written
  safe.available = false;
  T.resetCipher();
  const before2 = JSON.stringify(readJson("passwords.json"));
  wc.loadURL("https://secure.example/login");
  T.pendingSaves.clear();
  submit(pageEvt(wc), { username: "dave", password: "Dave-Pw-1" });
  R.unavailable = { offered: Boolean(T.privacyState().save), notice: T.privacyState().notice, vault: T.privacyState().vault.available };
  R.unavailableSave = T.vault.save({ origin: "https://secure.example", username: "dave", password: "Dave-Pw-1" });
  safe.available = true;
  R.noPlaintextAnywhere = ["passwords.json", "addresses.json", "permissions.json", "history.json"].every((f) => { const r = readRaw(f); return r === null || (!r.includes(SECRET) && !r.includes(SECRET2) && !r.includes("Dave-Pw-1")); });
});
''' % (SECRET, SECRET2)

BRIDGE = r'''
const SECRET = "%s";
main(async () => {
  T.ensurePaneView();
  T.showPane();
  const wc = T.tabs.get(T.active()).view.webContents;
  wc.loadURL("https://login.example/signin");
  await handlers["pane:screen"](consoleEvt(), true);
  listeners["dm:pw-submit"](pageEvt(wc), { username: "alice", password: SECRET });
  const offered = T.privacyState().save;
  // one saved, one waiting
  await handlers["pw:save-answer"](consoleEvt(), offered.id, "save");
  listeners["dm:pw-submit"](pageEvt(wc), { username: "alice2", password: SECRET + "2" });
  await handlers["addr:save"](consoleEvt(), { label: "Home", name: "Ada Lovelace", phone: "555-0100", line1: "12 Analytical Way" });

  const routes = [];
  for (const r of ["/status", "/tabs", "/history", "/bookmarks", "/downloads", "/permissions", "/passwords", "/passwords/list", "/passwords/reveal",
    "/vault", "/credentials", "/autofill", "/autofill/fill", "/addresses", "/pw", "/forms", "/save", "/fill", "/privacy", "/state"]) routes.push(["GET", r]);
  for (const r of ["/passwords", "/passwords/save", "/passwords/fill", "/passwords/reveal", "/autofill", "/fill", "/save", "/credentials", "/vault/unlock", "/privacy/answer"]) routes.push(["POST", r]);
  R.responses = {};
  let all = "";
  for (const [m, r] of routes) {
    const res = await call(m, r, { id: "x", entry: "x", origin: "https://login.example", url: "https://login.example/signin" });
    R.responses[m + " " + r] = res.status;
    all += res.text;
  }
  R.leaks = [SECRET, SECRET + "2", "alice", "alice2", "Ada Lovelace", "555-0100", "Analytical", "Hunter"].map((t) => all.includes(t));
  R.status = (await call("GET", "/status")).body;
  R.disk = ["passwords.json", "addresses.json"].map((f) => { T.flushBrowserStores(); const r = readRaw(f) || ""; return [SECRET, "alice", "Ada Lovelace", "555-0100"].some((t) => r.includes(t)); });
  R.cdpEndpointOnly = Object.keys(R.status).sort();
});
''' % SECRET

ADDRESS = r'''
main(async () => {
  T.ensurePaneView();
  T.showPane();
  const tab = T.tabs.get(T.active());
  const wc = tab.view.webContents;
  wc.loadURL("https://shop.example/checkout");
  await handlers["pane:screen"](consoleEvt(), true);
  listeners["dm:forms"](pageEvt(wc), { pw: false, user: false, addr: true });
  R.noProfileNoOffer = T.privacyState().fillAddress;
  R.saveFromPage = await handlers["addr:save"](pageEvt(wc), { name: "Ada Lovelace" });
  R.saveEmpty = await handlers["addr:save"](consoleEvt(), { label: "x" });
  const saved = await handlers["addr:save"](consoleEvt(), { label: "Home", name: "Ada Lovelace", email: "ada@example.test", phone: "555-0100", line1: "12 Analytical Way", city: "London", zip: "N1 1AA", country: "UK" });
  R.saved = saved.ok;
  R.offer = T.privacyState().fillAddress;
  T.flushBrowserStores();
  const raw = readRaw("addresses.json");
  R.disk = { leaks: ["Ada", "Lovelace", "555-0100", "Analytical", "London", "ada@example.test"].filter((t) => raw.includes(t)), mode: require("fs").statSync(path.join(USERDATA, "browser", "addresses.json")).mode & 0o777 };
  R.list = (await handlers["addr:list"](consoleEvt())).profiles.map((p) => [p.label, p.name, p.line1]);
  R.listFromPage = await handlers["addr:list"](pageEvt(wc));
  const id = saved.id;
  wc.sentToThis.length = 0;
  R.fillFromPage = await handlers["addr:fill"](pageEvt(wc), id);
  R.nothingYet = wc.sentToThis.length;
  R.fill = await handlers["addr:fill"](consoleEvt(), id);
  R.sent = wc.sentToThis.filter((m) => m.channel === "dm:fill-address").map((m) => ({ origin: m.payload.origin, name: m.payload.fields.name, line1: m.payload.fields.line1 }));
  wc.loadURL("about:blank");
  R.blankPage = await handlers["addr:fill"](consoleEvt(), id);
  wc.loadURL("https://shop.example/checkout");
  R.missing = await handlers["addr:fill"](consoleEvt(), "nope");
  // the fill goes to the tab in front and to no other
  const other = T.openTab("https://news.example/");
  wc.sentToThis.length = 0;
  R.toTheFrontTab = await handlers["addr:fill"](consoleEvt(), id);
  R.behindGotNothing = wc.sentToThis.length;
  R.frontGotIt = other.view.webContents.sentToThis.filter((m) => m.channel === "dm:fill-address").length;
  T.activateTab(tab.id);
  T.closeTab(other.id);
  wc.loadURL("https://shop.example/checkout");
  calls.menus.length = 0;
  listeners["dm:field-click"](pageEvt(wc), { kind: "address" });
  R.menuLabels = calls.menus.map((m) => m.items.map((i) => i.label || i.type));
  R.editFromPage = await handlers["addr:save"](pageEvt(wc), { name: "X" }, id);
  R.edit = (await handlers["addr:save"](consoleEvt(), { label: "Home", name: "Ada King", line1: "1 Road" }, id)).ok;
  R.afterEdit = (await handlers["addr:list"](consoleEvt())).profiles[0].name;
  R.deleteFromPage = await handlers["addr:delete"](pageEvt(wc), id);
  R.delete = await handlers["addr:delete"](consoleEvt(), id);
  R.count = T.addressBook.count();
  safe.available = false;
  T.resetCipher();
  R.unavailable = await handlers["addr:save"](consoleEvt(), { name: "No Keychain" });
});
'''


KEYCHAIN = r'''
main(async () => {
  T.ensurePaneView();
  T.showPane();
  const wc = T.tabs.get(T.active()).view.webContents;
  wc.loadURL("https://login.example/signin");
  await handlers["pane:screen"](consoleEvt(), true);
  // start-up, state pushes, the bridge status, permission prompts and Site settings never touch the secure storage
  T.privacyState();
  await call("GET", "/status");
  listeners["dm:forms"](pageEvt(wc), { pw: true, user: true, addr: true });
  const geo = ask(wc, "geolocation", { requestingUrl: "https://login.example/signin" });
  await wait(20);
  await handlers["pane:perm-answer"](consoleEvt(), T.privacyState().perm.id, "allow");
  await geo;
  await handlers["site:perms"](consoleEvt());
  R.afterStartup = { calls: safe.calls, status: (await call("GET", "/status")).body.passwords, state: T.privacyState().vault };
  // a login that can never be saved (plain http to a real host) does not ask either
  wc.loadURL("http://plain.example/login");
  listeners["dm:pw-submit"](pageEvt(wc), { username: "a", password: "b" });
  R.afterUnsavable = safe.calls;
  // the first login that can be saved is the first touch
  wc.loadURL("https://login.example/signin");
  listeners["dm:pw-submit"](pageEvt(wc), { username: "alice", password: "pw" });
  R.afterFirstLogin = { calls: safe.calls, offered: Boolean(T.privacyState().save), known: T.privacyState().vault.available };
  // opening the Passwords panel is a deliberate act and may ask
  await handlers["pw:list"](consoleEvt());
  R.afterPanel = safe.calls;
});
'''


@pytest.fixture(scope="module")
def keychain(tmp_path_factory):
    return run_scenario(tmp_path_factory.mktemp("keychain"), KEYCHAIN)


@pytest.fixture(scope="module")
def login(tmp_path_factory):
    return run_scenario(tmp_path_factory.mktemp("login"), LOGIN)


@pytest.fixture(scope="module")
def bridge(tmp_path_factory):
    return run_scenario(tmp_path_factory.mktemp("pwbridge"), BRIDGE)


@pytest.fixture(scope="module")
def address(tmp_path_factory):
    return run_scenario(tmp_path_factory.mktemp("addr"), ADDRESS)


class TestTheSecureStorageIsNotTouchedUntilItIsNeeded:
    """On macOS, asking Electron whether encryption is available creates or opens this app's
    Keychain item (seen live). So launching the app, pushing state, answering a permission prompt
    or asking the bridge for its status must never do it."""

    def test_nothing_before_a_login_is_actually_submitted(self, keychain):
        assert keychain["afterStartup"] == {"calls": 0, "status": {"passwords": 0, "neverSaved": 0, "encryption": None}, "state": {"available": None, "passwords": 0, "neverSaved": 0}}
        assert keychain["afterUnsavable"] == 0

    def test_the_first_savable_login_is_the_first_touch_and_it_is_then_known(self, keychain):
        assert keychain["afterFirstLogin"]["calls"] >= 1
        assert keychain["afterFirstLogin"]["offered"] is True and keychain["afterFirstLogin"]["known"] is True
        assert keychain["afterPanel"] >= keychain["afterFirstLogin"]["calls"]


class TestOnlyARealTopPageLoginIsOffered:
    def test_a_login_form_is_reported_without_any_value_and_no_login_means_no_fill_offer(self, login):
        assert login["noEntriesNoFill"] is None

    def test_a_submission_from_a_subframe_a_stranger_the_console_a_moved_frame_or_junk_is_ignored(self, login):
        assert login["ignored"] is None

    def test_a_real_one_is_offered_by_username_and_the_console_never_receives_the_password(self, login):
        assert login["offered"]["host"] == "login.example"
        assert login["offered"]["username"] == "alice" and login["offered"]["update"] is False
        assert "password" not in login["offered"]
        assert login["consoleNeverSawIt"] is True

    def test_plain_http_to_a_real_host_is_never_offered_but_this_machine_is(self, login):
        assert login["plainHttp"] is None
        assert login["loopback"] == "127.0.0.1:18851"

    def test_a_flood_from_one_tab_is_cut_off_and_a_prompt_goes_with_its_tab(self, login):
        assert login["floodPrompts"] >= 1  # one waiting prompt per tab, the latest
        assert login["floodLastAccepted"] == "u9"  # the eleventh and later never got in
        assert login["pendingBeforeClose"] is True and login["pendingAfterClose"] is False


class TestSavingIsTheOwnersChoiceAndIsEncrypted:
    def test_only_the_console_answers_and_only_with_a_real_answer(self, login):
        assert login["answerFromPage"] == {"ok": False, "error": "only the console may do that"}
        assert login["answerFromStranger"]["ok"] is False and login["answerBad"]["ok"] is False
        assert login["stillOffered"] is True
        assert login["saved"]["ok"] is True and login["saved"]["status"] == "added"
        assert login["afterSave"] is None

    def test_at_rest_the_file_has_the_site_but_neither_the_password_nor_the_username_and_is_owner_only(self, login):
        assert login["disk"] == {"leaksPassword": False, "leaksUser": False, "hasSite": True, "mode": 0o600}
        assert login["entryCount"] == 1

    def test_the_same_login_is_silent_a_new_password_is_an_update_and_not_now_keeps_the_old_one(self, login):
        assert login["sameAgain"] is None
        assert login["update"]["update"] is True
        assert login["notNow"]["ok"] is True and login["afterNotNow"] is None
        assert login["stillTheOldOne"] is True
        assert login["updated"] is True

    def test_never_for_this_site_is_remembered_listed_and_undoable_only_by_the_console(self, login):
        assert login["neverOffer"] == "shop.example"
        assert login["never"]["ok"] is True and login["neverSilence"] is None
        assert login["neverList"] == ["https://shop.example"]
        assert login["neverRemoveFromPage"]["ok"] is False
        assert login["neverRemove"]["ok"] is True and login["offeredAgain"] is True

    def test_without_secure_storage_nothing_is_offered_and_nothing_is_stored_in_plaintext(self, login):
        assert login["unavailable"]["offered"] is False and "secure storage" in login["unavailable"]["notice"]
        assert login["unavailable"]["vault"] is False
        assert login["unavailableSave"]["ok"] is False
        assert login["noPlaintextAnywhere"] is True


class TestFillingNeedsTheOwnerAndTheRightPlace:
    def test_the_bar_lists_usernames_only_and_a_page_or_stranger_cannot_press_fill(self, login):
        assert login["fillOffer"]["host"] == "login.example" and [e["username"] for e in login["fillOffer"]["entries"]] == ["alice"]
        assert set(login["fillOffer"]["entries"][0]) == {"id", "username"}
        assert login["fillOfferHasNoPassword"] is True
        assert login["fillFromPage"]["ok"] is False and login["fillFromStranger"]["ok"] is False
        assert login["nothingSentYet"] == 0

    def test_a_console_fill_sends_the_login_to_that_page_only_and_stamps_it_used(self, login):
        assert login["fill"] == {"ok": True}
        assert login["sentFill"] == [{"origin": "https://login.example", "username": "alice", "password": SECRET2}]
        assert login["fillWentOnlyToThatTab"] is True and login["fillNeverTouchedTheConsole"] is True
        assert login["fillUsedStamp"] is True

    def test_it_never_fills_another_origin_scheme_port_lookalike_blank_page_or_a_tab_behind(self, login):
        for key in ("otherSite", "otherScheme", "otherPort", "lookalike", "blank", "notInFront", "missingEntry"):
            assert login[key]["ok"] is False, key
        assert "another site" in login["otherSite"]["error"]
        assert login["nothingSentToWrongPlaces"] == 0
        assert login["fillOfferGoneElsewhere"] is None

    def test_a_click_on_a_field_opens_a_native_menu_and_fills_nothing_until_an_item_is_chosen(self, login):
        assert login["menu"] == [{"labels": ["Saved logins for login.example", "separator", "alice"], "inWindow": True}]
        assert login["filledByMenuOpening"] == 0 and login["filledByMenuItem"] == 1
        assert login["noMenuFromOthers"] is True


class TestShowingAPasswordNeedsAPerson:
    def test_a_page_cannot_even_open_the_dialog_a_cancel_returns_nothing_and_a_yes_returns_it(self, login):
        assert login["revealFromPage"]["ok"] is False and login["dialogsAfterPageTried"] == 0
        assert login["revealCancelled"] == {"ok": False, "error": "cancelled"}
        assert login["reveal"] == {"ok": True, "password": SECRET2}
        assert login["dialog"]["count"] == 2 and login["dialog"]["buttons"] == ["Show password", "Cancel"]
        assert "alice" in login["dialog"]["message"] and "login.example" in login["dialog"]["message"]
        assert login["revealMissing"]["ok"] is False

    def test_the_list_and_delete_are_the_consoles_and_the_list_never_holds_a_password(self, login):
        assert login["listShape"]["rows"] == [["https://login.example", "alice", True]]
        assert login["listShape"]["keys"] == ["created", "id", "lastUsed", "origin", "readable", "updated", "username"]
        assert login["listLeaks"] is False
        assert login["listFromPage"]["ok"] is False
        assert login["deleteFromPage"]["ok"] is False and login["delete"] == {"ok": True}
        assert login["afterDelete"]["passwords"] == 0


class TestNothingOnTheBridgeCanReachThem:
    def test_no_bridge_route_returns_a_password_a_username_an_address_or_a_site(self, bridge):
        assert bridge["leaks"] == [False] * 8
        assert bridge["disk"] == [False, False]

    def test_status_carries_counts_only(self, bridge):
        assert bridge["status"]["passwords"] == {"passwords": 1, "neverSaved": 0, "encryption": True}  # known once something was saved
        assert bridge["status"]["addresses"] == {"count": 1}
        assert bridge["cdpEndpointOnly"] == ["active", "activeTab", "addresses", "cdpEndpoint", "passwords", "permissions", "tabCount"]

    def test_every_guessed_password_route_is_a_404(self, bridge):
        known = {"GET /status", "GET /tabs", "GET /history", "GET /bookmarks", "GET /downloads", "GET /permissions"}
        for route, status in bridge["responses"].items():
            if route in known:
                assert status == 200, route
            else:
                assert status == 404, route


class TestAddressAutofill:
    def test_an_address_is_encrypted_at_rest_and_listed_only_for_the_console(self, address):
        assert address["saveFromPage"]["ok"] is False and address["saveEmpty"]["ok"] is False
        assert address["saved"] is True
        assert address["disk"] == {"leaks": [], "mode": 0o600}
        assert address["list"] == [["Home", "Ada Lovelace", "12 Analytical Way"]]
        assert address["listFromPage"]["ok"] is False

    def test_the_offer_appears_only_when_a_profile_exists_and_the_page_has_address_fields(self, address):
        assert address["noProfileNoOffer"] is None
        assert address["offer"]["profiles"] == [{"id": address["offer"]["profiles"][0]["id"], "label": "Home"}]

    def test_fill_goes_only_from_the_console_to_the_page_in_front_and_never_to_a_blank_page(self, address):
        assert address["fillFromPage"]["ok"] is False and address["nothingYet"] == 0
        assert address["fill"] == {"ok": True}
        assert address["sent"] == [{"origin": "https://shop.example", "name": "Ada Lovelace", "line1": "12 Analytical Way"}]
        assert address["blankPage"]["ok"] is False and address["missing"]["ok"] is False
        assert address["toTheFrontTab"] == {"ok": True} and address["behindGotNothing"] == 0 and address["frontGotIt"] == 1

    def test_a_click_on_an_address_field_lists_profiles_and_edit_and_delete_are_the_consoles(self, address):
        assert address["menuLabels"] == [["Saved addresses", "separator", "Home"]]
        assert address["editFromPage"]["ok"] is False and address["edit"] is True and address["afterEdit"] == "Ada King"
        assert address["deleteFromPage"]["ok"] is False and address["delete"] == {"ok": True} and address["count"] == 0

    def test_without_secure_storage_an_address_is_not_saved(self, address):
        assert address["unavailable"]["ok"] is False and "encryption" in address["unavailable"]["error"]


class TestTheFormHelperScript:
    """electron/content/frame-forms.js runs inside web pages, so its rules are pinned in text.
    What it does with a real page is checked live and recorded in the B2 finding."""

    def test_it_makes_no_network_request_and_writes_nothing(self):
        for forbidden in ("fetch(", "XMLHttpRequest", "WebSocket", "sendBeacon", "localStorage", "sessionStorage", "indexedDB", "document.cookie", "eval(", "new Function", "innerHTML", "console."):
            assert forbidden not in HELPER, forbidden

    def test_it_never_submits_or_clicks_for_the_owner(self):
        for forbidden in (".submit(", ".requestSubmit(", ".click(", "dispatchEvent(new MouseEvent", "dispatchEvent(new KeyboardEvent", "form.reset"):
            assert forbidden not in HELPER, forbidden

    def test_only_events_from_real_input_count(self):
        # the submit, click and keydown handlers all start by refusing an untrusted event
        for kind in ('"submit"', '"click"', '"keydown"'):
            for m in re.finditer(r"document\.addEventListener\(" + kind + r", \(e\) => \{\n\s*(.*?)\n", HELPER):
                assert "isTrusted" in m.group(1), kind

    def test_it_acts_in_the_top_page_only(self):
        assert "if (process.isMainFrame) {" in HELPER

    def test_a_fill_is_refused_unless_the_page_is_still_at_the_origin_named(self):
        assert HELPER.count("msg.origin !== location.origin") == 2

    def test_the_only_outbound_messages_are_the_four_the_shell_validates(self):
        sent = set(re.findall(r'ipcRenderer\.send\("([^"]+)"', HELPER))
        assert sent == {"dm:forms", "dm:pw-submit", "dm:field-click"}
        assert set(re.findall(r'ipcRenderer\.on\("([^"]+)"', HELPER)) == {"dm:fill-login", "dm:fill-address"}

    def test_it_exposes_nothing_to_the_page(self):
        for forbidden in ("contextBridge", "window.", "globalThis.", "exposeInMainWorld", "postMessage"):
            assert forbidden not in HELPER.replace("window.addEventListener", ""), forbidden


class TestTheShellValidatesWhatTheHelperSends:
    def test_messages_are_believed_only_from_the_top_page_of_a_pane_tab_at_its_real_address(self):
        m = re.search(r"^function trustedTabMessage\(.*?^}", MAIN_CODE, re.S | re.M).group(0)
        assert "tabForContents(evt.sender)" in m and "frame.parent !== null" in m
        assert "permLib.originOf(frame.url)" in m and "permLib.originOf(evt.sender.getURL())" in m

    def test_every_password_ipc_handler_is_console_only(self):
        for name in ("pw:list", "pw:delete", "pw:never-remove", "pw:save-answer", "pw:fill", "pw:reveal", "addr:list", "addr:save", "addr:delete", "addr:fill"):
            m = re.search(r'ipcMain\.handle\("' + re.escape(name) + r'", (.*?)\n(?:ipcMain|\n)', MAIN_CODE, re.S)
            assert m and "consoleOnly(evt)" in m.group(1), name

    def test_the_password_file_is_written_owner_only(self):
        assert 'makeStore("passwords.json", { entries: [], never: [] }, { mode: 0o600, lazy: true })' in MAIN_CODE
        assert 'makeStore("addresses.json", { profiles: [] }, { mode: 0o600, lazy: true })' in MAIN_CODE
        assert "mode: fileMode" in MAIN_CODE

    def test_a_password_is_cleared_from_memory_when_its_prompt_ends(self):
        assert MAIN_CODE.count('s.password = ""') >= 3

    def test_no_log_line_in_the_new_code_can_carry_a_password(self):
        block = MAIN_CODE[MAIN_CODE.index("// ------------------------------ logins and fills"):MAIN_CODE.index("// What the bridge may say")]
        for line in block.splitlines():
            if "log(" in line:
                assert "password" not in line and "credentials" not in line, line

    def test_the_sessions_form_helper_exists_and_is_listed_for_packaging_note(self):
        assert (ELECTRON / "content" / "frame-forms.js").is_file()
        assert stat.S_ISREG((ELECTRON / "permissions.js").stat().st_mode)


def test_an_ipc_fill_asks_a_native_dialog_first() -> None:
    """Finding #163: a CDP client can call the console IPC, so fill needs a native confirmation."""
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[2] / "electron" / "main.js").read_text()
    pw = src[src.index('ipcMain.handle("pw:fill"'):][:600]
    ad = src[src.index('ipcMain.handle("addr:fill"'):][:500]
    assert "confirmFillNatively" in pw and "confirmFillNatively" in ad
