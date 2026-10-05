"""Phase C1: the pane bridge names each tab so the browser agent can follow the active one.

``GET /tabs`` now carries, per tab, ``wcId`` (the webContents id) and ``targetId`` (the CDP target id
that Playwright's Page maps to). ``GET /status`` is deliberately unchanged: the B2 tests pin its exact key set.
electron/main.js is loaded under plain node with the same fake ``electron`` the B1 tab tests use, plus a
recording fake ``webContents.debugger``; the real ``tabTargetId`` and ``bridgeTabsView`` code runs. What
this cannot prove is what real Chromium answers to ``Target.getTargetInfo``: that was read from a live
isolated copy of the app and is recorded in finding #165.
"""

from __future__ import annotations

import shutil

import pytest

from dourmouse.tests.test_browser_tabs_shell import _run

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

DEBUGGER = r'''
// A recording fake of webContents.debugger, installed on the fake webContents prototype.
const open = new Set();
const DBG = { attaches: 0, detaches: 0, maxOpen: 0, commands: [], failNext: new Set(), preAttached: new Set() };
function installDebugger() {
  const proto = Object.getPrototypeOf(T.view().webContents);
  Object.defineProperty(proto, "debugger", {
    configurable: true,
    get() {
      if (!this.__dbg) {
        const wc = this;
        let attached = DBG.preAttached.has(wc.id);
        this.__dbg = {
          isAttached: () => attached,
          attach() { if (attached) throw new Error("already attached"); attached = true; DBG.attaches += 1; open.add(wc.id); DBG.maxOpen = Math.max(DBG.maxOpen, open.size); },
          detach() { attached = false; DBG.detaches += 1; open.delete(wc.id); },
          async sendCommand(name) {
            DBG.commands.push(name);
            await new Promise((r) => setTimeout(r, 15));
            if (DBG.failNext.has(wc.id)) throw new Error("target gone");
            return { targetInfo: { targetId: "TARGET-" + wc.id, type: "page" } };
          },
        };
      }
      return this.__dbg;
    },
  });
}
'''

TARGET_IDS = DEBUGGER + r'''
main(async () => {
  T.ensurePaneView();
  installDebugger();
  await call("POST", "/show", {});
  await call("POST", "/navigate", { url: "https://a.example/" });
  await call("POST", "/tabs/new", { url: "https://b.example/" });
  await call("POST", "/tabs/new", { url: "https://c.example/" });
  const first = (await call("GET", "/tabs")).body;
  R.first = first.tabs.map((t) => ({ id: t.id, wcId: t.wcId, targetId: t.targetId, active: t.active }));
  R.active = first.active;
  R.afterFirst = { attaches: DBG.attaches, detaches: DBG.detaches, stillOpen: [...open].length };
  // cached: a second read does not touch the debugger
  await call("GET", "/tabs");
  R.afterSecond = { attaches: DBG.attaches, detaches: DBG.detaches };
  // refresh re-reads every tab
  const refreshed = (await call("GET", "/tabs?refresh=1")).body;
  R.afterRefresh = { attaches: DBG.attaches, detaches: DBG.detaches, same: refreshed.tabs.map((t) => t.targetId) };
  // /status is unchanged (its key set is pinned by the B2 tests): it must NOT grow the ids
  R.statusKeys = Object.keys((await call("GET", "/status")).body).filter((k) => /target|wc/i.test(k));
  // five parallel refreshes: the sessions never overlap and always detach
  DBG.maxOpen = 0;
  const par = await Promise.all([1, 2, 3, 4, 5].map(() => call("GET", "/tabs?refresh=1")));
  R.parallel = { statuses: par.map((r) => r.status), maxOpen: DBG.maxOpen, balanced: DBG.attaches === DBG.detaches, stillOpen: [...open].length };
  // the owner switches tab: the active ids follow
  await call("POST", "/tabs/select", { id: 1 });
  const afterSwitch = (await call("GET", "/tabs")).body;
  R.afterSwitch = { active: afterSwitch.active, activeTargetId: afterSwitch.tabs.find((t) => t.active).targetId };
  // the command reads the target of THAT tab only
  R.commandsAreTargetInfo = [...new Set(DBG.commands)];
  // a closed tab is gone from the list
  await call("POST", "/tabs/close", { id: 2 });
  R.afterClose = (await call("GET", "/tabs")).body.tabs.map((t) => t.id);
});
'''

FAILURES = DEBUGGER + r'''
main(async () => {
  T.ensurePaneView();
  installDebugger();
  await call("POST", "/show", {});
  const t1 = T.tabs.get(T.active());
  // 1. the debugger command fails: the route still answers, the id is empty, the session is detached
  DBG.failNext.add(t1.view.webContents.id);
  const bad = await call("GET", "/tabs");
  R.failed = { status: bad.status, targetId: bad.body.tabs[0].targetId, detached: DBG.attaches === DBG.detaches, wcId: bad.body.tabs[0].wcId };
  // 2. recovery: the next read works and caches
  DBG.failNext.clear();
  const good = await call("GET", "/tabs");
  R.recovered = good.body.tabs[0].targetId;
  // 3. a debugger the app did not attach is left attached
  await call("POST", "/tabs/new", { url: "https://b.example/" });
  const t2 = T.tabs.get(T.active());
  DBG.preAttached.add(t2.view.webContents.id);
  const before = { attaches: DBG.attaches, detaches: DBG.detaches };
  const pre = await call("GET", "/tabs");
  R.preAttached = {
    targetId: pre.body.tabs.find((t) => t.id === t2.id).targetId,
    attachedByUs: DBG.attaches - before.attaches,
    detachedByUs: DBG.detaches - before.detaches,
    stillAttached: t2.view.webContents.debugger.isAttached(),
  };
  // 4. a webContents without a debugger at all: empty target id, no crash
  const t3 = (await call("POST", "/tabs/new", { url: "https://c.example/" })).body.id;
  const wc3 = T.tabs.get(t3).view.webContents;
  Object.defineProperty(wc3, "debugger", { value: undefined, configurable: true });
  const none = await call("GET", "/tabs");
  R.noDebugger = { status: none.status, targetId: none.body.tabs.find((t) => t.id === t3).targetId };
  // 5. a destroyed webContents reports wcId 0 and no target id, and is not asked
  const t4 = (await call("POST", "/tabs/new", { url: "https://d.example/" })).body.id;
  const tab4 = T.tabs.get(t4);
  const cmds = DBG.commands.length;
  tab4.view.webContents.destroyed = true;
  const gone = await call("GET", "/tabs");
  const row = gone.body.tabs.find((t) => t.id === t4);
  R.destroyed = { wcId: row.wcId, targetId: row.targetId, asked: DBG.commands.length - cmds };
  // 6. a page cannot reach it: a browser-originated request is refused
  R.origin = (await call("GET", "/tabs", undefined, { Origin: "https://evil.example" })).status;
});
'''


def test_tabs_report_wc_id_and_cdp_target_id(tmp_path):
    r = _run(tmp_path, TARGET_IDS)
    assert len(r["first"]) == 3
    for tab in r["first"]:
        assert tab["wcId"] > 0
        assert tab["targetId"] == f"TARGET-{tab['wcId']}"
    assert [t["active"] for t in r["first"]].count(True) == 1
    # every session is detached again, and the cache means a second read touches nothing
    assert r["afterFirst"] == {"attaches": 3, "detaches": 3, "stillOpen": 0}
    assert r["afterSecond"] == {"attaches": 3, "detaches": 3}
    assert r["afterRefresh"]["attaches"] == 6 and r["afterRefresh"]["detaches"] == 6
    assert r["afterRefresh"]["same"] == [t["targetId"] for t in r["first"]]


def test_status_is_unchanged(tmp_path):
    r = _run(tmp_path, TARGET_IDS)
    assert r["statusKeys"] == []


def test_parallel_requests_never_overlap_debugger_sessions(tmp_path):
    r = _run(tmp_path, TARGET_IDS)
    assert r["parallel"]["statuses"] == [200] * 5
    assert r["parallel"]["maxOpen"] == 1
    assert r["parallel"]["balanced"] is True and r["parallel"]["stillOpen"] == 0


def test_the_active_ids_follow_a_tab_switch_and_a_close(tmp_path):
    r = _run(tmp_path, TARGET_IDS)
    first_tab = r["first"][0]
    assert r["afterSwitch"]["active"] == first_tab["id"]
    assert r["afterSwitch"]["activeTargetId"] == first_tab["targetId"]
    assert r["commandsAreTargetInfo"] == ["Target.getTargetInfo"]
    assert 2 not in r["afterClose"] and len(r["afterClose"]) == 2


def test_failures_are_survivable_and_leave_no_session_open(tmp_path):
    r = _run(tmp_path, FAILURES)
    assert r["failed"]["status"] == 200 and r["failed"]["targetId"] == "" and r["failed"]["wcId"] > 0
    assert r["failed"]["detached"] is True
    assert r["recovered"].startswith("TARGET-")
    pre = r["preAttached"]
    assert pre["targetId"].startswith("TARGET-")
    assert pre["attachedByUs"] == 0 and pre["detachedByUs"] == 0 and pre["stillAttached"] is True
    assert r["noDebugger"] == {"status": 200, "targetId": ""}
    assert r["destroyed"] == {"wcId": 0, "targetId": "", "asked": 0}
    assert r["origin"] == 403
