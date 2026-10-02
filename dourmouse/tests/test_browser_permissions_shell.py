"""Phase B2: the Electron shell's per-site permission prompts, driven for real.

electron/main.js is loaded under plain node with a fake ``electron`` (see b2_harness.py) and
its OWN permission handlers, IPC handlers and pane bridge are exercised. What this cannot
prove is what Chromium does with a real page asking for a real camera; that was checked live
in an isolated copy of the app and is recorded in the B2 finding.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from dourmouse.tests.b2_harness import ELECTRON, NODE, run_scenario

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

MAIN_CODE = (ELECTRON / "main.js").read_text(encoding="utf-8")

PERMISSIONS = r'''
main(async () => {
  T.ensurePaneView();
  T.showPane();
  const tab = T.tabs.get(T.active());
  const wc = tab.view.webContents;
  wc.loadURL("https://meet.example/room");
  R.sessionSetup = {
    preloads: PANE_SESSION.preloads.map((p) => [p.type, path.basename(p.filePath), fs.existsSync(p.filePath)]),
    displayDenied: (() => { let got; PANE_SESSION.display({}, (s) => { got = s; }); return got; })(),
    devicesDenied: PANE_SESSION.device({}),
    appSessionIsNotThePane: APP_SESSION.preloads.length,
  };

  // A. nobody is looking at the BROWSER screen: refused at once, nothing queued, nothing stored
  R.noScreen = { value: await ask(wc, "geolocation", { requestingUrl: "https://meet.example/room", isMainFrame: true }), queued: T.promptQueue.size(), stored: Object.keys(T.siteTable()).length };

  // the console reports its BROWSER screen is showing, through the real handler
  await handlers["pane:screen"](consoleEvt(), true);

  // B. first request waits for the owner; only the console can answer
  const geo = ask(wc, "geolocation", { requestingUrl: "https://meet.example/room", isMainFrame: true });
  const geoState = watch(geo);
  await wait(20);
  const prompt = T.privacyState().perm;
  R.prompt = prompt;
  R.pendingBeforeAnswer = !geoState.done;
  R.fromPage = await handlers["pane:perm-answer"](pageEvt(wc), prompt.id, "allow");
  R.fromStranger = await handlers["pane:perm-answer"](strangerEvt(), prompt.id, "allow");
  R.fromConsoleSubframe = await handlers["pane:perm-answer"](consoleSubframeEvt(), prompt.id, "allow");
  await wait(20);
  R.stillPendingAfterRefusals = !geoState.done;
  R.badDecision = await handlers["pane:perm-answer"](consoleEvt(), prompt.id, "grant-everything");
  R.answer = await handlers["pane:perm-answer"](consoleEvt(), prompt.id, "allow");
  await wait(20);
  R.geoGranted = geoState.value;
  R.stored = T.siteTable();
  R.secondAsk = await ask(wc, "geolocation", { requestingUrl: "https://meet.example/room", isMainFrame: true });
  R.noSecondPrompt = T.promptQueue.size();
  R.answerTwice = await handlers["pane:perm-answer"](consoleEvt(), prompt.id, "allow");

  // C. Block is remembered and silent afterwards
  const notif = ask(wc, "notifications", { requestingUrl: "https://meet.example/room" });
  await wait(20);
  await handlers["pane:perm-answer"](consoleEvt(), T.privacyState().perm.id, "block");
  R.blocked = await notif;
  R.blockedAgain = await ask(wc, "notifications", { requestingUrl: "https://meet.example/room" });
  R.blockedAgainQueued = T.promptQueue.size();

  // D. Allow this time: granted for this tab and origin, nothing stored, gone when the tab leaves the origin
  const fsReq = ask(wc, "fullscreen", { requestingUrl: "https://meet.example/room" });
  await wait(20);
  await handlers["pane:perm-answer"](consoleEvt(), T.privacyState().perm.id, "once");
  R.once = await fsReq;
  R.onceAgain = await ask(wc, "fullscreen", { requestingUrl: "https://meet.example/room" });
  R.onceStored = T.siteTable()["https://meet.example"].fullscreen === undefined;
  R.onceCheck = check(wc, "fullscreen");
  wc.loadURL("https://other.example/");
  wc.loadURL("https://meet.example/room");
  R.onceAfterLeaving = check(wc, "fullscreen");
  const again = ask(wc, "fullscreen", { requestingUrl: "https://meet.example/room" });
  const againState = watch(again);
  await wait(20);
  R.promptsAgainAfterLeaving = !againState.done && Boolean(T.privacyState().perm);
  await handlers["pane:perm-answer"](consoleEvt(), T.privacyState().perm.id, "dismiss");
  R.dismissed = await again;

  // E. things that are never offered, and a frame from another origin
  const never = {};
  for (const name of ["display-capture", "midi", "midiSysex", "openExternal", "usb", "serial", "hid", "pointerLock", "unknown-thing"]) {
    never[name] = await ask(wc, name, { requestingUrl: "https://meet.example/room" });
  }
  R.never = never;
  R.neverQueued = T.promptQueue.size();
  R.screenShare = await ask(wc, "media", { requestingUrl: "https://meet.example/room", mediaTypes: ["screen"] });
  R.screenShareEmptyList = await ask(wc, "media", { requestingUrl: "https://meet.example/room", mediaTypes: [] });  // how getDisplayMedia really arrives
  R.clipboardWrite = await ask(wc, "clipboard-sanitized-write", { requestingUrl: "https://meet.example/room" });
  R.foreignFrame = await ask(wc, "geolocation", { requestingUrl: "https://evil.example/", isMainFrame: false });
  R.foreignFrameQueued = T.promptQueue.size();
  R.otherOriginThanThePage = await ask(wc, "geolocation", { requestingUrl: "http://meet.example/room" }); // same host, other scheme
  R.blankUrl = await ask(wc, "geolocation", { requestingUrl: "about:blank" });

  // F. checks never prompt, and answer only from what is stored
  R.check = {
    geoAllowed: check(wc, "geolocation"),
    notifBlocked: check(wc, "notifications"),
    clipboardRead: check(wc, "clipboard-read"),
    clipboardWrite: check(wc, "clipboard-sanitized-write"),
    midi: check(wc, "midi"),
    foreignOrigin: check(wc, "geolocation", "https://evil.example"),
    nullContents: PANE_SESSION.chk(null, "geolocation", "https://meet.example", {}),
    strangerContents: PANE_SESSION.chk(new FakeWC(PANE_SESSION), "geolocation", "https://meet.example", {}),
  };
  R.checkQueued = T.promptQueue.size();

  // G. a prompt for a page that moved on, or a tab that closed, is dismissed and remembers nothing
  const old = ask(wc, "clipboard-read", { requestingUrl: "https://meet.example/room" });
  await wait(20);
  wc.loadURL("https://elsewhere.example/");
  R.movedOn = await old;
  const tab2 = T.openTab("https://news.example/");
  const wc2 = tab2.view.webContents;
  const closing = ask(wc2, "geolocation", { requestingUrl: "https://news.example/" });
  await wait(20);
  T.closeTab(tab2.id);
  R.closedTab = await closing;
  R.afterDismissals = { queued: T.promptQueue.size(), stored: Object.keys(T.siteTable()).sort() };

  // H. the same question twice is one prompt with two waiters
  wc.loadURL("https://meet.example/room");
  const d1 = ask(wc, "clipboard-read", { requestingUrl: "https://meet.example/room" });
  const d2 = ask(wc, "clipboard-read", { requestingUrl: "https://meet.example/room" });
  await wait(20);
  R.dupQueued = T.promptQueue.size();
  await handlers["pane:perm-answer"](consoleEvt(), T.privacyState().perm.id, "block");
  R.dup = [await d1, await d2];

  // I. the app's own windows keep the old rule, a stranger in the pane's session is refused
  T.installPermissionPolicy(APP_SESSION); // what main.js does for the default session once the app is ready
  R.appWindow = {
    ownMedia: await new Promise((r) => APP_SESSION.req(win.webContents, "media", r, { requestingUrl: win.webContents.url })),
    foreignMedia: await new Promise((r) => APP_SESSION.req(win.webContents, "media", r, { requestingUrl: "https://evil.example/" })),
    ownCheck: APP_SESSION.chk(win.webContents, "media", win.webContents.url, {}),
  };
  R.strangerInPaneSession = await new Promise((r) => PANE_SESSION.req(new FakeWC(PANE_SESSION), "media", r, { requestingUrl: "http://127.0.0.1:" + UI_PORT + "/" }));

  // J. the console manages what is stored: change, reset, forget, reset all; the page cannot
  R.listFromPage = await handlers["site:perms"](pageEvt(wc));
  R.list = (await handlers["site:perms"](consoleEvt())).sites.map((s) => [s.origin, s.permission, s.decision]);
  R.setFromPage = await handlers["site:perm-set"](pageEvt(wc), "https://meet.example", "geolocation", "block");
  R.setBad = [
    await handlers["site:perm-set"](consoleEvt(), "https://meet.example", "midi", "allow"),
    await handlers["site:perm-set"](consoleEvt(), "javascript:alert(1)", "camera", "allow"),
    await handlers["site:perm-set"](consoleEvt(), "https://meet.example", "camera", "yes"),
  ].map((r) => r.ok);
  await handlers["site:perm-set"](consoleEvt(), "https://meet.example", "geolocation", "block");
  R.afterBlock = await ask(wc, "geolocation", { requestingUrl: "https://meet.example/room" });
  await handlers["site:perm-set"](consoleEvt(), "https://meet.example", "geolocation", "reset");
  const reAsk = ask(wc, "geolocation", { requestingUrl: "https://meet.example/room" });
  const reAskState = watch(reAsk);
  await wait(20);
  R.afterReset = !reAskState.done;
  await handlers["pane:perm-answer"](consoleEvt(), T.privacyState().perm.id, "dismiss");
  R.forgetFromPage = await handlers["site:perm-forget"](pageEvt(wc), "https://meet.example");
  R.forget = await handlers["site:perm-forget"](consoleEvt(), "https://meet.example");
  R.afterForget = Object.keys(T.siteTable());
  await handlers["site:perm-set"](consoleEvt(), "https://a.example", "camera", "allow");
  R.clearFromPage = await handlers["site:perms-clear"](pageEvt(wc));
  R.clear = await handlers["site:perms-clear"](consoleEvt());
  R.afterClear = Object.keys(T.siteTable());

  // K. the file on disk
  await handlers["site:perm-set"](consoleEvt(), "https://disk.example", "camera", "allow");
  T.flushBrowserStores();
  R.disk = readJson("permissions.json");
});
'''

MEDIA = r'''
main(async () => {
  T.ensurePaneView();
  T.showPane();
  const wc = T.tabs.get(T.active()).view.webContents;
  wc.loadURL("https://meet.example/room");
  await handlers["pane:screen"](consoleEvt(), true);
  const details = { requestingUrl: "https://meet.example/room", isMainFrame: true, mediaTypes: ["audio", "video"] };
  const answer = async (decision) => { await wait(30); const p = T.privacyState().perm; return handlers["pane:perm-answer"](consoleEvt(), p.id, decision); };
  const noticeNow = () => T.privacyState().notice;

  // the prompt names both devices and one answer covers both
  const both = ask(wc, "media", details);
  await wait(30);
  R.sentence = T.privacyState().perm.text;
  R.keys = T.privacyState().perm.keys;
  await answer("allow");
  R.allowed = await both;
  R.stored = Object.keys(T.siteTable()["https://meet.example"]).sort();
  R.killSwitchHits = kill.hits;

  // stored allow still obeys the kill switch, the request and the check alike
  kill.mic_enabled = false;
  R.micOff = { request: await ask(wc, "media", details), notice: noticeNow() };
  R.micOffVideoOnly = await ask(wc, "media", { ...details, mediaTypes: ["video"] });
  R.checkMicOff = check(wc, "media", "", { mediaType: "audio" });
  R.checkCamWhileMicOff = check(wc, "media", "", { mediaType: "video" });
  kill.mic_enabled = true; kill.camera_enabled = false;
  R.camOff = { request: await ask(wc, "media", { ...details, mediaTypes: ["video"] }), notice: noticeNow() };
  kill.camera_enabled = true;

  // fail closed: if the privacy switch cannot be read, the devices stay off
  kill.fail = true;
  R.switchUnreadable = { request: await ask(wc, "media", details), notice: noticeNow() };
  kill.fail = false;
  R.switchBack = await ask(wc, "media", details);
  R.checkAllOk = check(wc, "media", "", { mediaType: "audio" });

  // macOS's own permission
  tcc.microphone = "denied";
  R.tccDenied = { request: await ask(wc, "media", { ...details, mediaTypes: ["audio"] }), notice: noticeNow(), asked: calls.asked.slice() };
  tcc.microphone = "not-determined"; tcc.askResult = false;
  R.tccAskRefused = { request: await ask(wc, "media", { ...details, mediaTypes: ["audio"] }), asked: calls.asked.slice() };
  tcc.askResult = true;
  R.tccAskGranted = { request: await ask(wc, "media", { ...details, mediaTypes: ["audio"] }), asked: calls.asked.slice() };
  tcc.microphone = "restricted";
  R.tccRestricted = await ask(wc, "media", { ...details, mediaTypes: ["audio"] });
  tcc.microphone = "granted";

  // a camera for a site the owner blocked
  await handlers["site:perm-set"](consoleEvt(), "https://meet.example", "camera", "block");
  R.blockedCamera = await ask(wc, "media", { ...details, mediaTypes: ["video"] });
  R.blockedEither = await ask(wc, "media", details);

  // allow this time on a camera for another site goes through the same gate
  const w2 = T.openTab("https://other.example/").view.webContents;
  const once = ask(w2, "media", { requestingUrl: "https://other.example/", mediaTypes: ["video"] });
  await wait(30);
  await handlers["pane:perm-answer"](consoleEvt(), T.privacyState().perm.id, "once");
  R.onceCamera = await once;
  kill.camera_enabled = false;
  R.onceCameraSwitchOff = await ask(w2, "media", { requestingUrl: "https://other.example/", mediaTypes: ["video"] });
});
'''

BRIDGE = r'''
main(async () => {
  T.ensurePaneView();
  T.showPane();
  const wc = T.tabs.get(T.active()).view.webContents;
  wc.loadURL("https://meet.example/room");
  await handlers["pane:screen"](consoleEvt(), true);
  const geo = ask(wc, "geolocation", { requestingUrl: "https://meet.example/room" });
  await wait(30);
  R.statusWhilePending = (await call("GET", "/status")).body;
  await handlers["pane:perm-answer"](consoleEvt(), T.privacyState().perm.id, "allow");
  await geo;
  const status = await call("GET", "/status");
  R.status = status.body;
  R.statusText = status.text;
  R.permissions = (await call("GET", "/permissions")).body;
  // no route on the bridge can grant, change or revoke anything
  const guesses = [["POST", "/permissions"], ["POST", "/permissions/grant"], ["POST", "/permissions/allow"], ["POST", "/permissions/set"],
    ["POST", "/permissions/revoke"], ["POST", "/permissions/answer"], ["POST", "/permissions/clear"], ["PUT", "/permissions"], ["DELETE", "/permissions"],
    ["POST", "/perm/answer"], ["POST", "/prompt/answer"], ["POST", "/site/grant"], ["GET", "/prompts"], ["GET", "/privacy"]];
  R.guesses = {};
  for (const [m, route] of guesses) R.guesses[m + " " + route] = (await call(m, route, { origin: "https://meet.example", permission: "camera", decision: "allow", id: "p1" })).status;
  R.stillOnlyGeo = T.siteTable();
  R.browserOriginRefused = (await call("GET", "/permissions", undefined, { Origin: "https://evil.example" })).status;
  R.fetchSiteRefused = (await call("GET", "/permissions", undefined, { "Sec-Fetch-Site": "cross-site" })).status;
});
'''


@pytest.fixture(scope="module")
def perms(tmp_path_factory):
    return run_scenario(tmp_path_factory.mktemp("perms"), PERMISSIONS)


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    return run_scenario(tmp_path_factory.mktemp("media"), MEDIA)


@pytest.fixture(scope="module")
def bridge(tmp_path_factory):
    return run_scenario(tmp_path_factory.mktemp("bridge"), BRIDGE)


class TestThePanesSession:
    def test_screen_capture_and_device_pickers_are_refused_and_the_form_helper_is_registered_before_any_page(self, perms):
        s = perms["sessionSetup"]
        assert s["displayDenied"] == {}  # getDisplayMedia is answered with no stream at all
        assert s["devicesDenied"] is False
        assert s["preloads"] == [["frame", "frame-forms.js", True]]
        assert s["appSessionIsNotThePane"] == 0  # the app's own windows never get the form helper


class TestAPromptNeedsSomebodyToAnswerIt:
    def test_with_the_browser_screen_not_showing_a_request_is_refused_and_nothing_is_stored(self, perms):
        assert perms["noScreen"] == {"value": False, "queued": 0, "stored": 0}

    def test_the_first_request_waits_and_is_named_for_the_owner(self, perms):
        assert perms["prompt"]["origin"] == "https://meet.example"
        assert perms["prompt"]["host"] == "meet.example"
        assert perms["prompt"]["keys"] == ["geolocation"]
        assert perms["prompt"]["text"] == "meet.example wants to know your location"
        assert perms["pendingBeforeAnswer"] is True

    def test_only_the_console_top_page_can_answer(self, perms):
        assert perms["fromPage"] == {"ok": False, "error": "only the console may do that"}
        assert perms["fromStranger"]["ok"] is False
        assert perms["fromConsoleSubframe"]["ok"] is False
        assert perms["stillPendingAfterRefusals"] is True

    def test_a_made_up_decision_is_refused_and_a_real_one_is_delivered_once(self, perms):
        assert perms["badDecision"]["ok"] is False
        assert perms["answer"] == {"ok": True}
        assert perms["answerTwice"] == {"ok": False}
        assert perms["geoGranted"] is True


class TestDecisionsAreRememberedPerSite:
    def test_allow_is_stored_for_that_origin_and_then_silent(self, perms):
        assert perms["stored"]["https://meet.example"]["geolocation"]["d"] == "allow"
        assert perms["secondAsk"] is True and perms["noSecondPrompt"] == 0

    def test_block_is_stored_and_the_site_is_refused_without_asking_again(self, perms):
        assert perms["blocked"] is False and perms["blockedAgain"] is False and perms["blockedAgainQueued"] == 0

    def test_allow_this_time_grants_without_storing_and_ends_when_the_tab_leaves_the_origin(self, perms):
        assert perms["once"] is True and perms["onceAgain"] is True
        assert perms["onceStored"] is True and perms["onceCheck"] is True
        assert perms["onceAfterLeaving"] is False
        assert perms["promptsAgainAfterLeaving"] is True
        assert perms["dismissed"] is False

    def test_the_file_on_disk_holds_exactly_the_decisions(self, perms):
        assert set(perms["disk"]["sites"]) == {"https://disk.example"}
        assert perms["disk"]["sites"]["https://disk.example"]["camera"]["d"] == "allow"


class TestWhatIsNeverGranted:
    def test_screen_capture_midi_usb_serial_hid_openexternal_and_the_unknown_are_refused_unasked(self, perms):
        assert perms["never"] == {k: False for k in ("display-capture", "midi", "midiSysex", "openExternal", "usb", "serial", "hid", "pointerLock", "unknown-thing")}
        assert perms["neverQueued"] == 0
        assert perms["screenShare"] is False and perms["screenShareEmptyList"] is False

    def test_clipboard_write_is_allowed_as_chrome_does_without_a_prompt(self, perms):
        assert perms["clipboardWrite"] is True

    def test_a_frame_from_another_origin_does_not_borrow_the_pages_decisions(self, perms):
        assert perms["foreignFrame"] is False and perms["foreignFrameQueued"] == 0
        assert perms["otherOriginThanThePage"] is False  # http is not https
        assert perms["blankUrl"] is False

    def test_checks_answer_from_what_is_stored_and_never_prompt(self, perms):
        assert perms["check"] == {
            "geoAllowed": True, "notifBlocked": False, "clipboardRead": False, "clipboardWrite": True, "midi": False,
            "foreignOrigin": False, "nullContents": False, "strangerContents": False,
        }
        assert perms["checkQueued"] == 0


class TestPromptsDieWithThePageThatAsked:
    def test_moving_to_another_origin_or_closing_the_tab_dismisses_and_stores_nothing(self, perms):
        assert perms["movedOn"] is False and perms["closedTab"] is False
        assert perms["afterDismissals"]["queued"] == 0
        assert "https://news.example" not in perms["afterDismissals"]["stored"]

    def test_the_same_question_twice_is_one_prompt_answered_for_both(self, perms):
        assert perms["dupQueued"] == 1 and perms["dup"] == [False, False]


class TestTheAppsOwnWindowsAreUnchanged:
    def test_the_app_origin_keeps_media_and_a_stranger_in_the_pane_session_gets_nothing(self, perms):
        assert perms["appWindow"] == {"ownMedia": True, "foreignMedia": False, "ownCheck": True}
        assert perms["strangerInPaneSession"] is False


class TestSiteSettingsBelongToTheConsole:
    def test_a_page_and_a_stranger_cannot_list_change_forget_or_clear(self, perms):
        for key in ("listFromPage", "setFromPage", "forgetFromPage", "clearFromPage"):
            assert perms[key] == {"ok": False, "error": "only the console may do that"}, key

    def test_the_console_can_list_change_reset_forget_and_clear(self, perms):
        assert ["https://meet.example", "geolocation", "allow"] in perms["list"]
        assert perms["setBad"] == [False, False, False]  # midi is never grantable, nor a non-web origin, nor "yes"
        assert perms["afterBlock"] is False
        assert perms["afterReset"] is True  # reset means the site asks again
        assert perms["forget"]["ok"] is True and perms["afterForget"] == []
        assert perms["clear"]["ok"] is True and perms["afterClear"] == []


class TestCameraAndMicrophoneHaveTheirOwnGates:
    def test_one_prompt_names_both_devices_and_one_answer_covers_them(self, media):
        assert media["sentence"] == "meet.example wants to use your microphone and camera"
        assert media["keys"] == ["microphone", "camera"]
        assert media["allowed"] is True and media["stored"] == ["camera", "microphone"]

    def test_a_stored_allow_still_obeys_the_privacy_kill_switch(self, media):
        assert media["micOff"]["request"] is False and "microphone is switched off" in media["micOff"]["notice"]
        assert media["micOffVideoOnly"] is True  # the camera is a separate switch
        assert media["checkMicOff"] is False  # a synchronous check uses the last answer of the switch
        assert media["checkCamWhileMicOff"] is True
        assert media["camOff"]["request"] is False and "camera is switched off" in media["camOff"]["notice"]

    def test_if_the_switch_cannot_be_read_the_devices_stay_off(self, media):
        assert media["switchUnreadable"]["request"] is False and "could not be read" in media["switchUnreadable"]["notice"]
        assert media["switchBack"] is True and media["checkAllOk"] is True

    @pytest.mark.skipif(__import__("sys").platform != "darwin", reason="macOS permission is checked on macOS only")
    def test_macos_permission_is_honoured_and_asked_for_once_when_undecided(self, media):
        assert media["tccDenied"]["request"] is False and "macOS" in media["tccDenied"]["notice"] and media["tccDenied"]["asked"] == []
        assert media["tccAskRefused"] == {"request": False, "asked": ["microphone"]}
        assert media["tccAskGranted"]["request"] is True
        assert media["tccRestricted"] is False

    def test_a_blocked_device_blocks_the_whole_request_and_a_one_time_grant_passes_the_same_gate(self, media):
        assert media["blockedCamera"] is False and media["blockedEither"] is False
        assert media["onceCamera"] is True and media["onceCameraSwitchOff"] is False


class TestTheBridgeCannotGrantAnything:
    def test_status_has_counts_only_and_never_a_site_or_a_value(self, bridge):
        assert bridge["statusWhilePending"]["permissions"]["pendingPrompts"] == 1
        assert bridge["status"]["permissions"] == {"pendingPrompts": 0, "origins": 1, "decisions": 1}
        assert bridge["status"]["passwords"] == {"passwords": 0, "neverSaved": 0, "encryption": None}  # unknown: the secure storage was not asked
        assert bridge["status"]["addresses"] == {"count": 0}
        assert "meet.example" not in bridge["statusText"]

    def test_the_permissions_list_is_read_only_and_nothing_can_be_granted_over_http(self, bridge):
        assert bridge["permissions"]["sites"] == [{"origin": "https://meet.example", "permission": "geolocation", "label": "Location", "decision": "allow", "at": bridge["permissions"]["sites"][0]["at"]}]
        assert set(bridge["guesses"].values()) == {404}
        assert list(bridge["stillOnlyGeo"]) == ["https://meet.example"]

    def test_a_browser_page_cannot_read_even_the_list(self, bridge):
        assert bridge["browserOriginRefused"] == 403 and bridge["fetchSiteRefused"] == 403


class TestTheSourceKeepsTheGrantDoorShut:
    def _body(self, name):
        m = re.search(rf"^function {name}\(.*?^}}", MAIN_CODE, re.S | re.M)
        assert m, name
        return m.group(0)

    def test_no_bridge_route_stores_a_decision_answers_a_prompt_or_touches_a_password(self):
        body = self._body("startPaneBridge")
        for forbidden in ("setDecision", "storeSites", "promptQueue.answer", "tempGrants.add", "vault.", "addressBook.", "pendingSaves", "credentials(", "cipher."):
            assert forbidden not in body, forbidden
        assert "privacyCounts()" in body and "permLib.listSites" in body

    def test_every_grant_goes_through_one_waiter_that_only_the_console_handler_can_trigger(self):
        assert MAIN_CODE.count("promptQueue.answer(") == 1
        assert MAIN_CODE.count("permLib.setDecision(") == 3  # the allow waiter, the block waiter, the Site settings handler
        assert re.search(r'ipcMain\.handle\("pane:perm-answer", \(evt, id, decision\) => \{\s*if \(!consoleOnly\(evt\)\) return REFUSED;', MAIN_CODE)

    def test_the_sender_check_demands_the_console_window_and_its_top_page(self):
        body = self._body("consoleOnly")
        assert "fromConsole(evt)" in body and "frame.parent === null" in body

    def test_every_site_and_prompt_ipc_handler_starts_with_that_check(self):
        for name in ("pane:privacy", "pane:perm-answer", "site:perms", "site:perm-set", "site:perm-forget", "site:perms-clear"):
            m = re.search(r'ipcMain\.handle\("' + re.escape(name) + r'", (.*?)\n(?:ipcMain|\n)', MAIN_CODE, re.S)
            assert m and "consoleOnly(evt)" in m.group(1), name

    def test_the_pane_session_handlers_are_installed_before_the_first_page_exists(self):
        body = self._body("ensurePaneView")
        assert body.index("installPaneSession(session.fromPartition(PANE_PARTITION))") < body.index("new BrowserView(")

    def test_the_pane_preferences_still_carry_no_preload_and_no_node(self):
        prefs = re.search(r"const TAB_WEB_PREFERENCES = (\{[^}]*\});", MAIN_CODE).group(1)
        for forbidden in ("preload", "nodeIntegration", "sandbox: false", "webSecurity: false", "contextIsolation: false"):
            assert forbidden not in prefs

    def test_an_isolated_copy_can_have_its_own_keychain_name(self):
        assert "DOURMOUSE_ELECTRON_APP_NAME" in MAIN_CODE and "app.setName(process.env.DOURMOUSE_ELECTRON_APP_NAME)" in MAIN_CODE

    def test_no_em_dash_was_added(self):
        for f in ("permissions.js", "passwords.js", "content/frame-forms.js"):
            assert "—" not in (ELECTRON / f).read_text(encoding="utf-8"), f
