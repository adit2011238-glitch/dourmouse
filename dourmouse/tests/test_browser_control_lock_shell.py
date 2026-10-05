"""Phase C2: the Electron shell's owner/model lock, driven for real under node.

electron/main.js runs under plain node with the same fake ``electron`` the B1 tab tests use (see
test_browser_tabs_shell.py); its OWN control code, its OWN pane bridge (``/control*``) and its OWN
console IPC handlers are exercised. The owner's input is simulated the way Electron delivers it to
the main process: ``before-input-event`` for a key press and ``input-event`` for a click, scroll or
touch, on the tab's webContents. What this cannot prove is which events real Chromium raises for
real input and for CDP input: that was measured on Electron 44.3 and is recorded in the C2 finding
and in ~/Documents/DOURMOUSE/C2_SHARED_CONTROL_DESIGN.md.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from dourmouse.tests.test_browser_tabs_shell import ELECTRON, NODE, PRELUDE, _free_port

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

HOOKED = PRELUDE.replace(
    "visible: () => paneVisible,",
    "visible: () => paneVisible, control, controlConsoleView, CONTROL_OWNER_HOLD_MS,",
)
assert HOOKED != PRELUDE, "the harness hook line moved; update this test"

COMMON = r'''
const CONSOLE = { sender: win.webContents, senderFrame: { parent: null, url: win.webContents.url } };
const PAGE = { sender: { getURL: () => "https://evil.example/" }, senderFrame: { parent: null } };
function wcOf(tabId) { return T.tabs.get(tabId).view.webContents; }
function installInsert() {
  const proto = Object.getPrototypeOf(T.view().webContents);
  proto.insertText = async function (t) { (this.inserted = this.inserted || []).push(t); };
  proto.stop = function () { this.stops = (this.stops || 0) + 1; };
}
function ownerKey(tabId, extra) {
  wcOf(tabId).emit("before-input-event", { preventDefault() {} }, { type: "keyDown", key: "x", meta: false, control: false, shift: false, alt: false, ...(extra || {}) });
}
function input(tabId, ev) { wcOf(tabId).emit("input-event", {}, ev); }
const begin = (tab, tool) => call("POST", "/control/begin", { tab, tool });
const check = (action) => call("POST", "/control/check", { action });
const type = (action, text) => call("POST", "/control/type", { action, text });
const pointer = (action, body) => call("POST", "/control/pointer", { action, ...body });
const end = (action, outcome) => call("POST", "/control/end", { action, outcome });
const pushes = () => calls.sent.filter((s) => s[0] === "pane:control").map((s) => s[1]);
'''

LOCK = COMMON + r'''
main(async () => {
  T.ensurePaneView();
  installInsert();
  await call("POST", "/show", {});
  await call("POST", "/navigate", { url: "https://a.example/" });
  const t1 = T.active();
  const t2 = (await call("POST", "/tabs/new", { url: "https://b.example/" })).body.id;
  await call("POST", "/tabs/select", { id: t1 });

  R.idle = (await call("GET", "/control")).body;
  // a claim, text through the main process, an end
  const b = await begin(t1, "type");
  R.begin = { status: b.status, ok: b.body.ok, hasAction: typeof b.body.action === "string" && b.body.action.length >= 12 };
  const A = b.body.action;
  R.acting = (await call("GET", "/control")).body;
  R.typed = [(await type(A, "Hello ")).body, (await type(A, "world")).body];
  R.inserted = wcOf(t1).inserted.slice();
  R.otherTabUntouched = wcOf(t2).inserted || [];
  R.checkOk = (await check(A)).body;
  // the agent's own CDP key events reach input-event but never before-input-event: not the owner
  input(t1, { type: "keyDown", key: "a" });
  input(t1, { type: "rawKeyDown", key: "a" });
  input(t1, { type: "char", key: "a" });
  input(t1, { type: "mouseMove", x: 5, y: 5 });
  input(t1, { type: "mouseUp", x: 5, y: 5 });
  R.checkAfterAgentKeysAndMoves = (await check(A)).body;
  await wait(60);
  R.pushedWhileActing = pushes().slice(-1)[0];
  await end(A, "done");
  R.afterEnd = (await call("GET", "/control")).body;
  R.endTwiceIsHarmless = (await end(A, "done")).status;

  // the owner presses a key while the agent is typing: the next chunk is refused
  const A2 = (await begin(t1, "type")).body.action;
  await type(A2, "abc");
  ownerKey(t1);
  const refused = await type(A2, "def");
  R.typeAfterOwnerKey = { status: refused.status, body: refused.body };
  R.insertedAfterOwnerKey = wcOf(t1).inserted.slice();
  R.checkAfterOwnerKey = (await check(A2)).body;
  await end(A2, "owner-input");
  R.lastAfterInterrupt = T.controlConsoleView().last;
  R.ownerActive = (await call("GET", "/control")).body;
  // a new claim on that tab is refused while the owner is active, with the time left
  const busy = await begin(t1, "click");
  R.beginWhileOwnerActive = { status: busy.status, body: busy.body };
  R.waitingShown = (await call("GET", "/control")).body;
  R.consoleWhileWaiting = T.controlConsoleView();
  // the other tab is free: the lock is per tab
  const other = await begin(t2, "type");
  R.otherTabFree = other.body.ok === true;
  // owner input on tab 1 does not interrupt an action on tab 2
  ownerKey(t1);
  R.otherTabUninterrupted = (await check(other.body.action)).body;
  await end(other.body.action, "done");
  // once the hold has lapsed, the agent may act again
  await wait(T.CONTROL_OWNER_HOLD_MS + 120);
  const later = await begin(t1, "click");
  R.beginAfterHold = later.body.ok === true;
  await end(later.body.action, "done");
  R.idleAgain = (await call("GET", "/control")).body.state;
});
'''

POINTER = COMMON + r'''
main(async () => {
  T.ensurePaneView();
  installInsert();
  await call("POST", "/show", {});
  await call("POST", "/navigate", { url: "https://a.example/" });
  const t1 = T.active();
  const results = {};
  async function fresh() {
    T.control.lastOwner.clear(); /* the hold lapsing is tested in LOCK; here only the classification matters */
    const r = await begin(t1, "click");
    return r.body.action;
  }
  // a click with no pointer window is the owner's
  let A = await fresh();
  input(t1, { type: "mouseDown", x: 100, y: 50, button: "left", clickCount: 1 });
  results.clickWithoutWindow = (await check(A)).body;
  await end(A, "owner-input");
  // inside a pointer window at the agent's point: the agent's own click
  A = await fresh();
  results.open = (await pointer(A, { on: true, x: 100, y: 50, ms: 2000 })).body;
  input(t1, { type: "mouseMove", x: 100, y: 50 });
  input(t1, { type: "mouseDown", x: 104, y: 47, button: "left", clickCount: 1 });
  input(t1, { type: "mouseUp", x: 104, y: 47 });
  results.agentClick = (await check(A)).body;
  // inside the window but far from the agent's point: the owner's
  input(t1, { type: "mouseDown", x: 400, y: 300, button: "left", clickCount: 1 });
  results.farClick = (await check(A)).body;
  await end(A, "owner-input");
  // a zoomed page: the event carries CSS pixels times the zoom
  A = await fresh();
  wcOf(t1).zoom = 1.5;
  await pointer(A, { on: true, x: 100, y: 50 });
  input(t1, { type: "mouseDown", x: 150, y: 75, button: "left", clickCount: 1 });
  results.zoomedAgentClick = (await check(A)).body;
  wcOf(t1).zoom = 1;
  // a window with no point covers any click and the wheel, until it is closed
  await pointer(A, { on: false });
  await pointer(A, { on: true });
  input(t1, { type: "mouseWheel", deltaY: 40 });
  input(t1, { type: "mouseDown", x: 1, y: 1, button: "left", clickCount: 1 });
  results.noPointWindow = (await check(A)).body;
  await pointer(A, { on: false });
  await wait(220);
  input(t1, { type: "mouseWheel", deltaY: 40 });
  results.wheelAfterClose = (await check(A)).body;
  await end(A, "owner-input");
  // a window the agent never closes closes itself
  A = await fresh();
  await pointer(A, { on: true, ms: 120 });
  await wait(200);
  input(t1, { type: "touchStart" });
  results.touchAfterExpiry = (await check(A)).body;
  await end(A, "owner-input");
  // gestures that are not a click or a scroll do not count, mouse moves do not count
  A = await fresh();
  for (const type of ["mouseMove", "mouseEnter", "mouseLeave", "mouseUp", "gestureScrollUpdate", "keyUp"]) input(t1, { type, x: 3, y: 3 });
  results.nonOwnerEvents = (await check(A)).body;
  await end(A, "done");
  R.p = results;
});
'''

OWNER_BUTTONS = COMMON + r'''
main(async () => {
  T.ensurePaneView();
  installInsert();
  await call("POST", "/show", {});
  await call("POST", "/navigate", { url: "https://a.example/" });
  const t1 = T.active();
  const t2 = (await call("POST", "/tabs/new", { url: "https://b.example/" })).body.id;
  // Stop, from a page: refused; from the console: every action in flight stops
  const A = (await begin(t1, "open")).body.action;
  const B = (await begin(t2, "type")).body.action;
  wcOf(t1).isLoading = () => true;
  R.stopFromPage = await handlers["control:stop"](PAGE);
  R.stateFromPage = await handlers["control:state"](PAGE);
  R.checkAfterPageStop = (await check(A)).body;
  R.stop = await handlers["control:stop"](CONSOLE);
  R.afterStop = [(await check(A)).body, (await check(B)).body, (await type(B, "x")).status];
  R.navigationStopped = wcOf(t1).stops || 0;
  R.typingTabNotStopped = wcOf(t2).stops || 0;
  await end(A, "stopped");
  await end(B, "stopped");
  R.lastAfterStop = T.controlConsoleView().last;
  // Stop holds nothing: the next action may start
  const C = await begin(t1, "click");
  R.beginAfterStop = C.body.ok === true;
  // Take control: what runs stops, and nothing starts on any tab until release
  R.takeFromPage = await handlers["control:take"](PAGE);
  R.take = await handlers["control:take"](CONSOLE);
  await wait(80);
  R.checkAfterTake = (await check(C.body.action)).body;
  await end(C.body.action, "owner-control");
  R.beginWhileHeld = [(await begin(t1, "type")).body, (await begin(t2, "type")).body];
  R.heldState = (await call("GET", "/control")).body;
  R.consoleHeld = await handlers["control:state"](CONSOLE);
  // the bridge cannot stop, take or release (no such routes)
  R.bridgeRoutes = [];
  for (const r of ["/control/stop", "/control/take", "/control/release", "/control/hold"]) R.bridgeRoutes.push((await call("POST", r, {})).status);
  R.stillHeld = (await call("GET", "/control")).body.held;
  R.releaseFromPage = await handlers["control:release"](PAGE);
  R.release = await handlers["control:release"](CONSOLE);
  const D = await begin(t1, "type");
  R.beginAfterRelease = D.body.ok === true;
  await wait(80);
  await end(D.body.action, "done");
  await wait(80);
  R.pushes = pushes().map((p) => p.state);
});
'''

EDGES = COMMON + r'''
main(async () => {
  T.ensurePaneView();
  installInsert();
  await call("POST", "/show", {});
  await call("POST", "/navigate", { url: "https://a.example/" });
  const t1 = T.active();
  const t2 = (await call("POST", "/tabs/new", { url: "https://b.example/" })).body.id;
  R.beginNoTab = await begin(999, "type");
  R.beginBadTool = (await begin(t1, "Robert'); DROP")).body.ok;
  R.consoleTool = T.controlConsoleView().acting.map((a) => a.tool);
  const A = T.controlConsoleView().acting.length ? [...T.control.actions.keys()][0] : "";
  R.unknownAction = [(await check("nope")).body, (await type("nope", "x")).status];
  R.badText = [(await type(A, "")).status, (await type(A, "y".repeat(513))).status, (await type(A, 7)).status, (await type(A, "y".repeat(512))).status];
  // the tab closes under the action
  const B = (await begin(t2, "type")).body.action;
  await call("POST", "/tabs/close", { id: t2 });
  R.afterClose = [(await check(B)).body, (await type(B, "z")).status];
  await end(B, "no-tab");
  // a lease that lapsed (the agent died): the bar goes back to idle by itself
  for (const a of T.control.actions.values()) a.seen = Date.now() - 61000;
  R.lapsed = (await call("GET", "/control")).body;
  R.lastLapsed = T.controlConsoleView().last;
  // a page in the browser cannot reach the routes (finding #135 rule)
  R.fromBrowser = (await call("POST", "/control/begin", { tab: t1, tool: "type" }, { Origin: "https://evil.example" })).status;
  R.fromBrowserGet = (await call("GET", "/control", undefined, { "Sec-Fetch-Site": "cross-site" })).status;
  // the bridge view says state and counts, never a title, an address or text
  ownerKey(t1);
  R.bridgeView = (await call("GET", "/control")).body;
  R.statusKeys = Object.keys((await call("GET", "/status")).body).sort();
});
'''


def _run(tmp_path: Path, scenario: str) -> dict:
    script = tmp_path / "scenario.js"
    script.write_text(HOOKED + scenario, encoding="utf-8")
    env = {**os.environ, "T_MAIN": str(ELECTRON / "main.js"), "DOURMOUSE_ELECTRON_PANE_PORT": str(_free_port())}
    assert NODE is not None
    proc = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=90, env=env, check=False)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:")]
    assert lines, f"the scenario printed no result\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
    data = json.loads(lines[-1][len("RESULT:") :])
    assert "__error" not in data, data["__error"]
    return data


@pytest.fixture(scope="module")
def lock(tmp_path_factory):
    return _run(tmp_path_factory.mktemp("lock"), LOCK)


@pytest.fixture(scope="module")
def ptr(tmp_path_factory):
    return _run(tmp_path_factory.mktemp("ptr"), POINTER)["p"]


@pytest.fixture(scope="module")
def buttons(tmp_path_factory):
    return _run(tmp_path_factory.mktemp("buttons"), OWNER_BUTTONS)


@pytest.fixture(scope="module")
def edges(tmp_path_factory):
    return _run(tmp_path_factory.mktemp("edges"), EDGES)


class TestClaimAndText:
    def test_idle_at_start_and_a_claim_makes_the_model_acting(self, lock):
        assert lock["idle"]["state"] == "idle" and lock["idle"]["acting"] == 0
        assert lock["begin"] == {"status": 200, "ok": True, "hasAction": True}
        assert lock["acting"]["state"] == "model-acting" and lock["acting"]["acting"] == 1

    def test_text_goes_in_through_the_main_process_into_that_tab_only(self, lock):
        assert lock["typed"] == [{"ok": True, "typed": 6}, {"ok": True, "typed": 5}]
        assert lock["inserted"] == ["Hello ", "world"]
        assert lock["otherTabUntouched"] == []

    def test_the_agents_own_cdp_key_events_and_mouse_moves_are_not_taken_for_the_owner(self, lock):
        assert lock["checkOk"] == {"ok": True}
        assert lock["checkAfterAgentKeysAndMoves"] == {"ok": True}

    def test_the_console_is_told_who_acts_and_where(self, lock):
        pushed = lock["pushedWhileActing"]
        assert pushed["state"] == "model-acting"
        assert pushed["acting"][0]["tool"] == "type" and isinstance(pushed["acting"][0]["tabId"], int)

    def test_ending_returns_to_idle_and_is_idempotent(self, lock):
        assert lock["afterEnd"]["state"] == "idle"
        assert lock["endTwiceIsHarmless"] == 200


class TestTheOwnerWins:
    def test_an_owner_key_refuses_the_next_chunk_so_the_streams_never_interleave(self, lock):
        assert lock["typeAfterOwnerKey"]["status"] == 409
        assert lock["typeAfterOwnerKey"]["body"]["reason"] == "owner-input"
        assert lock["typeAfterOwnerKey"]["body"]["kind"] == "key"
        # "def" never went in: the last agent text is the chunk before the owner's key
        assert lock["insertedAfterOwnerKey"][-1] == "abc"
        assert lock["checkAfterOwnerKey"]["reason"] == "owner-input"
        assert lock["lastAfterInterrupt"]["outcome"] == "owner-input"

    def test_no_new_action_starts_while_the_owner_is_active_and_the_wait_is_shown(self, lock):
        assert lock["ownerActive"]["state"] == "owner-active"
        busy = lock["beginWhileOwnerActive"]
        assert busy["status"] == 409 and busy["body"]["reason"] == "owner-active"
        assert 0 < busy["body"]["retryInMs"] <= 2500
        assert lock["waitingShown"]["state"] == "model-waiting" and lock["waitingShown"]["waiting"] is True
        assert lock["consoleWhileWaiting"]["waiting"]["tool"] == "click"

    def test_the_lock_is_per_tab(self, lock):
        assert lock["otherTabFree"] is True
        assert lock["otherTabUninterrupted"] == {"ok": True}

    def test_after_the_owner_pauses_the_model_may_act_again(self, lock):
        assert lock["beginAfterHold"] is True
        assert lock["idleAgain"] == "idle"


class TestPointer:
    def test_a_click_outside_any_window_is_the_owners(self, ptr):
        assert ptr["clickWithoutWindow"] == {"ok": False, "reason": "owner-input", "kind": "click"}

    def test_the_agents_declared_click_is_its_own_and_a_far_click_is_not(self, ptr):
        assert ptr["open"] == {"ok": True}
        assert ptr["agentClick"] == {"ok": True}
        assert ptr["farClick"]["reason"] == "owner-input"

    def test_zoom_is_allowed_for(self, ptr):
        assert ptr["zoomedAgentClick"] == {"ok": True}

    def test_an_unpointed_window_covers_clicks_and_wheel_until_closed(self, ptr):
        assert ptr["noPointWindow"] == {"ok": True}
        assert ptr["wheelAfterClose"] == {"ok": False, "reason": "owner-input", "kind": "scroll"}

    def test_a_window_closes_itself(self, ptr):
        assert ptr["touchAfterExpiry"] == {"ok": False, "reason": "owner-input", "kind": "touch"}

    def test_moves_releases_and_scroll_updates_are_not_owner_input(self, ptr):
        assert ptr["nonOwnerEvents"] == {"ok": True}


class TestStopAndTakeControl:
    def test_only_the_console_can_stop(self, buttons):
        assert buttons["stopFromPage"] == {"ok": False, "error": "only the console may do that"}
        assert buttons["stateFromPage"] is None
        assert buttons["checkAfterPageStop"] == {"ok": True}

    def test_stop_stops_every_action_and_a_navigation_the_agent_started(self, buttons):
        assert buttons["stop"] == {"ok": True, "stopped": 2}
        a, b, typed = buttons["afterStop"]
        assert a["reason"] == "stopped" and b["reason"] == "stopped" and typed == 409
        assert buttons["navigationStopped"] == 1
        assert buttons["typingTabNotStopped"] == 0
        assert buttons["lastAfterStop"]["outcome"] == "stopped"

    def test_stop_does_not_hold(self, buttons):
        assert buttons["beginAfterStop"] is True

    def test_take_control_stops_and_holds_every_tab_until_release(self, buttons):
        assert buttons["takeFromPage"]["ok"] is False
        assert buttons["take"] == {"ok": True, "stopped": 1}
        assert buttons["checkAfterTake"]["reason"] == "owner-control"
        assert [b["reason"] for b in buttons["beginWhileHeld"]] == ["owner-control", "owner-control"]
        assert buttons["heldState"]["state"] == "owner-control" and buttons["heldState"]["held"] is True
        assert buttons["consoleHeld"]["state"] == "owner-control"

    def test_the_bridge_cannot_stop_take_or_release(self, buttons):
        assert buttons["bridgeRoutes"] == [404, 404, 404, 404]
        assert buttons["stillHeld"] is True
        assert buttons["releaseFromPage"]["ok"] is False

    def test_release_lets_the_model_act_and_the_console_saw_every_state(self, buttons):
        assert buttons["release"] == {"ok": True}
        assert buttons["beginAfterRelease"] is True
        assert "owner-control" in buttons["pushes"] and "model-acting" in buttons["pushes"]


class TestEdges:
    def test_a_missing_tab_and_an_odd_tool_name(self, edges):
        assert edges["beginNoTab"]["status"] == 404 and edges["beginNoTab"]["body"]["reason"] == "no-tab"
        assert edges["beginBadTool"] is True
        assert edges["consoleTool"] == ["act"]

    def test_unknown_actions_and_bad_text(self, edges):
        assert edges["unknownAction"][0]["reason"] == "expired" and edges["unknownAction"][1] == 409
        assert edges["badText"] == [400, 400, 400, 200]

    def test_a_tab_closed_under_the_action(self, edges):
        assert edges["afterClose"][0]["reason"] == "no-tab" and edges["afterClose"][1] == 409

    def test_a_lapsed_lease_ends_the_claim(self, edges):
        assert edges["lapsed"]["acting"] == 0 and edges["lapsed"]["state"] == "idle"
        assert edges["lastLapsed"]["outcome"] == "expired"

    def test_pages_in_a_browser_cannot_reach_the_routes(self, edges):
        assert edges["fromBrowser"] == 403 and edges["fromBrowserGet"] == 403

    def test_the_bridge_view_is_state_and_counts_only(self, edges):
        view = edges["bridgeView"]
        assert set(view) == {"ok", "state", "held", "acting", "waiting", "ownerActiveTabs", "ownerHoldMs"}
        assert view["state"] == "owner-active" and view["ownerActiveTabs"] == 1
        assert all(isinstance(v, (bool, int, str)) for v in view.values())

    def test_status_is_unchanged(self, edges):
        assert not any("control" in k for k in edges["statusKeys"])


class TestWiring:
    MAIN = (ELECTRON / "main.js").read_text(encoding="utf-8")

    def test_owner_keys_come_only_from_before_input_event(self):
        assert 'if (input.type === "keyDown") noteOwnerInput(tab, "key");' in self.MAIN
        assert 'wc.on("input-event", (_evt, input) => onPaneInputEvent(tab, input));' in self.MAIN
        # the main process never injects input events of its own into a pane tab
        assert "sendInputEvent" not in self.MAIN

    def test_the_console_handlers_check_the_sender(self):
        for name in ("control:stop", "control:take", "control:release"):
            i = self.MAIN.index(f'ipcMain.handle("{name}"')
            assert "consoleOnly(evt)" in self.MAIN[i : i + 200], name

    def test_the_preload_exposes_the_four_calls_and_a_removable_listener(self):
        pre = (ELECTRON / "preload.js").read_text(encoding="utf-8")
        block = pre[pre.index("control: {") : pre.index("privacy: {")]
        assert set(__import__("re").findall(r'ipcRenderer\.invoke\("([^"]+)"', block)) == {"control:state", "control:stop", "control:take", "control:release"}
        assert 'ipcRenderer.on("pane:control", h)' in block and "removeListener" in block
